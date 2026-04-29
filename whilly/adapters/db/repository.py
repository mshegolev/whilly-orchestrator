"""Postgres-backed task repository for Whilly v4.0 (PRD FR-1.3, FR-1.4, FR-2.1, FR-2.3, FR-2.4).

This module owns the SQL that mutates the ``tasks`` table and writes audit
rows to ``events``. It is the single I/O-side counterpart to the pure
state-machine in :mod:`whilly.core.state_machine`: callers operating against
Postgres go through :class:`TaskRepository` instead of issuing SQL directly,
so the at-least-once / atomicity invariants live in one place.

Scope of TASK-009b / TASK-009c / TASK-009d
------------------------------------------
TASK-009b implemented :meth:`TaskRepository.claim_task` — atomic
``PENDING`` → ``CLAIMED`` transition via ``SELECT ... FOR UPDATE SKIP LOCKED``
plus a CLAIM event in one transaction.

TASK-009c added :meth:`TaskRepository.complete_task` and
:meth:`TaskRepository.fail_task` with optimistic locking on the
``tasks.version`` counter (PRD FR-2.4). Both methods filter the UPDATE by
``WHERE id = $1 AND version = $2 AND status IN (...)`` — no row locks are
taken, so two concurrent completers race purely through the version
counter: one wins, the other gets 0 rows affected and we surface a
:class:`VersionConflictError` after a follow-up SELECT to differentiate
"someone moved past me" from "task gone" (FK cascade) and "wrong status".

TASK-009d (this commit) adds :meth:`TaskRepository.release_stale_tasks` —
the visibility-timeout sweep (PRD FR-1.4). It scans for ``CLAIMED`` or
``IN_PROGRESS`` rows whose ``claimed_at`` predates ``NOW() - interval``,
flips them back to ``PENDING`` (clearing ``claimed_by`` / ``claimed_at``,
incrementing ``version``), and inserts a ``RELEASE`` event per row with
``payload = {"reason": "visibility_timeout", "version": <new>}``. All
mutations happen in a single ``WITH released AS (UPDATE ... RETURNING ...)
INSERT INTO events ...`` round-trip so the audit log can never disagree
with the tasks table — same atomicity contract as the per-row methods,
batched.

Why ``FOR UPDATE SKIP LOCKED`` for ``claim_task`` but **not** for
complete/fail?
    Claim is multi-row contention: many workers compete for the queue head,
    so we must atomically pick *one* row from the available pool. SKIP
    LOCKED is the right primitive there. Complete / fail target a single,
    already-owned task — there's no pool to scan, just one row to flip.
    Optimistic locking via ``version`` lets us detect lost updates (e.g.
    visibility-timeout sweep released the task to a second worker that
    already started running it) without taking row locks, which is cheaper
    and avoids holding lockers while we write the audit event.

Why a CTE + outer UPDATE for claim?
    The CTE materialises the lock decision (``SKIP LOCKED LIMIT 1``) and the
    outer ``UPDATE ... FROM picked`` re-uses that same row lock to flip
    status / claimed_by / claimed_at / version in one statement. We could
    SELECT first and UPDATE second from Python, but that opens a window
    between the lock and the write where the connection could be lost — the
    single SQL keeps the operation atomic at the wire level too.

asyncpg + JSONB
    asyncpg returns JSONB columns as raw ``str`` (JSON text) by default.
    Rather than monkey-patching codecs onto the pool (TASK-009a's territory)
    we ``json.loads`` the array columns inside :func:`_row_to_task`. The
    helper also accepts already-decoded ``list``/``dict`` so a future codec
    registration in pool.py won't break us.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import asyncpg

from whilly.core.models import PlanId, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.core.state_machine import Transition

__all__ = [
    "BUDGET_EXCEEDED_EVENT_TYPE",
    "BUDGET_EXCEEDED_REASON",
    "BUDGET_EXCEEDED_THRESHOLD_PCT",
    "TaskRepository",
    "VersionConflictError",
]


# Sentinel event type emitted exactly once when ``plans.spent_usd``
# transitions from ``< budget_usd`` to ``>= budget_usd`` (TASK-102 /
# VAL-BUDGET-040 / 041). The string is part of the audit-log contract:
# downstream queries match on it (``WHERE event_type = 'plan.budget_exceeded'``)
# and dashboards / post-mortems pivot off it. The dotted lower-case form
# follows the project convention for plan-scoped event types — distinct
# from the upper-case state-machine transitions
# (``CLAIM`` / ``COMPLETE`` / ``FAIL``) which live on
# :class:`whilly.core.state_machine.Transition`.
BUDGET_EXCEEDED_EVENT_TYPE: str = "plan.budget_exceeded"

# Contract pins for the ``plan.budget_exceeded`` payload (VAL-CROSS-013).
# The v4.1 design only emits the sentinel on the 100%-of-budget crossing,
# so ``reason`` is fixed to ``budget_threshold`` and ``threshold_pct`` to
# ``100`` — defined as module-level constants here so future thresholds
# (e.g. 50% / 90% pre-warnings) can extend the same path without a
# literal-search across the codebase.
BUDGET_EXCEEDED_REASON: str = "budget_threshold"
BUDGET_EXCEEDED_THRESHOLD_PCT: int = 100


# Quantum used to coerce a Python float-ish ``cost_usd`` into a stable
# Decimal with the same precision as the ``plans.spent_usd`` /
# ``plans.budget_usd`` columns. NUMERIC(10, 4) on the SQL side; we
# round to 4 decimal places on the Python side so a runaway runner that
# returns ``cost_usd = 0.123456789`` lands as ``0.1235`` in storage
# without surprising the operator.
_COST_USD_QUANTUM: Decimal = Decimal("0.0001")


def _coerce_cost_usd(value: Any) -> Decimal:
    """Normalise ``cost_usd`` (Decimal | float | int | None) → :class:`Decimal`.

    Accepts every input the ``AgentResult.usage.cost_usd`` chain can
    produce (the agent runner parses the value as ``float`` from
    Claude's JSON envelope; the HTTP transport may also forward a
    string-encoded Decimal in a future version). Rejects negative
    values loudly — :meth:`TaskRepository.complete_task`'s strict
    monotonic invariant (VAL-BUDGET-072) requires ``cost_usd >= 0``.

    ``None`` and missing values normalise to ``Decimal(0)`` so callers
    can pass through a missing usage envelope without a defensive
    branch (VAL-BUDGET-032 — cost=0 is a no-op spend).
    """
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        cost = value
    elif isinstance(value, (int, float)):
        # ``Decimal(float)`` would faithfully encode the float's binary
        # representation (``Decimal('0.10000000000000000555...')``);
        # going through ``str`` first gives the human-friendly
        # decimal form (``Decimal('0.1')``) which is what the operator
        # expects to see in the events / dashboard.
        cost = Decimal(str(value))
    else:
        raise TypeError(f"cost_usd must be Decimal | float | int | None, got {type(value).__name__}: {value!r}")
    if cost < 0:
        raise ValueError(f"cost_usd must be non-negative (strict-monotonic spend invariant); got {cost}")
    # Quantize to numeric(10,4) precision so the SQL UPDATE stores the
    # same value the Python caller intended without driver-side
    # rounding surprises.
    return cost.quantize(_COST_USD_QUANTUM)


logger = logging.getLogger(__name__)


# Priority → integer rank for SQL ORDER BY. Lower = higher priority. The
# CHECK constraint on tasks.priority guarantees one of these four values
# in production data; the trailing ``ELSE`` is defence-in-depth so a row
# corrupted past the constraint still sorts deterministically (last).
_PRIORITY_RANK_SQL: str = (
    "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
)


# Atomic claim. The CTE locks one PENDING row with SKIP LOCKED so concurrent
# claimers pick different rows; the outer UPDATE flips the row to CLAIMED in
# the same statement and RETURNING ships the post-update fields back so the
# caller doesn't need a follow-up SELECT.
#
# Ordering: priority bucket first (so 'critical' beats 'low'), then ``id`` as
# a deterministic tiebreaker — keeps tests reproducible without preempting
# the richer ordering logic that lives in core.scheduler.next_ready
# (TASK-013c). ``next_ready`` operates on Plan/in-progress in memory and
# composes *above* claim_task; the SQL order here is the fallback when
# callers don't pre-filter.
_CLAIM_SQL: str = f"""
WITH picked AS (
    SELECT t.id
    FROM tasks t
    JOIN plans p ON p.id = t.plan_id
    WHERE t.plan_id = $1
      AND t.status = 'PENDING'
      -- Budget guard (TASK-102, VAL-BUDGET-021..023). NULL budget is
      -- the documented "unlimited" sentinel (VAL-BUDGET-020); strict
      -- ``<`` makes the gate boundary-exclusive (VAL-BUDGET-021 /
      -- VAL-BUDGET-023) so any sub-cent over-spend still blocks.
      -- ``IS NULL OR ...`` short-circuits in Postgres so the strict
      -- comparison never runs against NULL.
      AND (p.budget_usd IS NULL OR p.spent_usd < p.budget_usd)
    ORDER BY {_PRIORITY_RANK_SQL}, t.id
    -- ``FOR UPDATE OF t`` locks the *tasks* row only — not the
    -- single ``plans`` row that every concurrent claimer joins
    -- against. Without ``OF t``, SKIP LOCKED would skip the
    -- plans row whenever any other claimer holds it, starving
    -- 99/100 callers in a 100-way contention test (SC-1) and
    -- defeating the whole point of SKIP LOCKED's row-level
    -- granularity. The plans row only needs a read-consistent
    -- view of ``budget_usd`` / ``spent_usd``; the actual
    -- spend mutation happens later in :data:`_INCREMENT_SPEND_SQL`
    -- under its own ``FOR UPDATE`` (which serialises legitimately
    -- because crossing-detection requires a consistent
    -- pre-update snapshot).
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
)
UPDATE tasks
SET status = 'CLAIMED',
    claimed_by = $2,
    claimed_at = NOW(),
    version = tasks.version + 1,
    updated_at = NOW()
FROM picked
WHERE tasks.id = picked.id
RETURNING
    tasks.id,
    tasks.status,
    tasks.dependencies,
    tasks.key_files,
    tasks.priority,
    tasks.description,
    tasks.acceptance_criteria,
    tasks.test_steps,
    tasks.prd_requirement,
    tasks.version
"""


# One row per state transition (PRD FR-2.4). Inserted in the same transaction
# as the corresponding tasks UPDATE so the audit log can never disagree with
# the tasks table.
_INSERT_EVENT_SQL: str = """
INSERT INTO events (task_id, event_type, payload)
VALUES ($1, $2, $3::jsonb)
"""


# Variant of :data:`_INSERT_EVENT_SQL` that also writes the ``detail`` JSONB
# column (TASK-104b, migration 003). Distinct statement rather than a
# four-arg superset because most call sites do not have a ``detail`` payload
# — passing ``NULL`` through every existing INSERT would inflate the wire
# round-trip with no upside, and the explicit two-statement split keeps
# each call site's intent legible.
#
# ``$4::jsonb`` accepts SQL ``NULL`` straight from Python ``None`` (asyncpg
# does not stringify None into the JSON literal ``"null"`` for jsonb
# columns); the column itself is nullable per the migration so a Python
# ``None`` round-trips to ``IS NULL`` on read (VAL-TRIZ-009).
_INSERT_EVENT_WITH_DETAIL_SQL: str = """
INSERT INTO events (task_id, event_type, payload, detail)
VALUES ($1, $2, $3::jsonb, $4::jsonb)
"""


# Optimistic-locking START: ``CLAIMED`` → ``IN_PROGRESS``. Bridges the
# claim-side and complete-side of the worker loop (TASK-019a): a worker that
# just won ``claim_task`` calls this immediately so the eventual
# ``complete_task`` passes its ``status = 'IN_PROGRESS'`` filter. Mirrors the
# ``Transition.START`` rule from :func:`whilly.core.state_machine.apply_transition`.
#
# Why a separate transition rather than collapsing CLAIMED/IN_PROGRESS?
#     The two states encode different operational facts: CLAIMED means
#     "ownership taken, agent not yet spawned" and IN_PROGRESS means "agent
#     running". Heartbeat/visibility-timeout policy (PRD FR-1.4) and the
#     dashboard (TASK-027) care about the distinction. Keeping them separate
#     also lets ``fail_task`` accept both — a worker that crashes between
#     claim and start still gets a clean FAILED audit row.
_START_SQL: str = """
UPDATE tasks
SET status = 'IN_PROGRESS',
    version = tasks.version + 1,
    updated_at = NOW()
WHERE id = $1
  AND version = $2
  AND status = 'CLAIMED'
RETURNING
    tasks.id,
    tasks.status,
    tasks.dependencies,
    tasks.key_files,
    tasks.priority,
    tasks.description,
    tasks.acceptance_criteria,
    tasks.test_steps,
    tasks.prd_requirement,
    tasks.version
"""


# Optimistic-locking COMPLETE: only flips ``IN_PROGRESS`` → ``DONE`` when the
# caller's expected version matches the row's current version. The status
# filter mirrors the state-machine rule from
# :func:`whilly.core.state_machine.apply_transition` so a buggy or stale
# caller cannot drag a DONE / FAILED / SKIPPED task back through the
# lifecycle. RETURNING ships the post-update row plus its parent plan_id
# so the caller can correlate the task transition with the plan-side
# spend update without a follow-up SELECT.
_COMPLETE_SQL: str = """
UPDATE tasks
SET status = 'DONE',
    version = tasks.version + 1,
    updated_at = NOW()
WHERE id = $1
  AND version = $2
  -- CLAIMED is allowed alongside IN_PROGRESS to support the remote-worker
  -- shape (TASK-024a / SC-3): the HTTP transport doesn't expose a /start
  -- endpoint today, so a remote worker's claim → run → complete sequence
  -- never visits IN_PROGRESS. Mirrors the
  -- (Transition.COMPLETE, TaskStatus.CLAIMED) edge in
  -- whilly.core.state_machine. The local worker still goes through
  -- IN_PROGRESS because its runner emits a START audit row.
  AND status IN ('CLAIMED', 'IN_PROGRESS')
RETURNING
    tasks.id,
    tasks.status,
    tasks.dependencies,
    tasks.key_files,
    tasks.priority,
    tasks.description,
    tasks.acceptance_criteria,
    tasks.test_steps,
    tasks.prd_requirement,
    tasks.version,
    tasks.plan_id
"""


# Atomic plan-spend accumulator (TASK-102). Increments
# ``plans.spent_usd`` by ``$2::numeric`` and RETURNS three values the
# caller needs to decide whether to emit the
# ``plan.budget_exceeded`` sentinel:
#
# * ``budget_usd`` — NULL = unlimited (no sentinel, ever);
# * ``new_spent`` — the post-update running total;
# * ``crossed`` — boolean: True iff this UPDATE moved spent_usd from
#   strictly below the budget to >= the budget. The ``crossed`` flag
#   is computed entirely SQL-side using the *pre-update* row value
#   (referenced via the plain ``plans.spent_usd`` term, which under
#   Postgres ``UPDATE ... RETURNING`` semantics resolves to the row's
#   pre-update value — *not* the new value — when used in the SET
#   list expression context). To avoid that subtlety we capture the
#   pre-update value via a CTE and compute the crossing transition
#   over the BEFORE / AFTER pair.
#
# The CTE form keeps the whole operation in a single round-trip and
# guarantees the BEFORE / AFTER pair come from the same MVCC snapshot,
# so two concurrent ``complete_task`` calls each carrying half of the
# remaining budget can race through the optimistic-lock cleanly: each
# UPDATE locks its own row, and the BEFORE values they observe are
# serialised by the row lock — exactly one will see the
# spent_usd-pre-update strictly below the budget; the other will see
# pre-update already at-or-above (because the first commit advanced
# the column). VAL-BUDGET-050 / 052 are pinned by this exact path.
_INCREMENT_SPEND_SQL: str = """
WITH plan_before AS (
    SELECT id, budget_usd, spent_usd AS spent_before
    FROM plans
    WHERE id = $1
    FOR UPDATE
)
UPDATE plans
SET spent_usd = plans.spent_usd + $2::numeric
FROM plan_before
WHERE plans.id = plan_before.id
RETURNING
    plans.budget_usd AS budget_usd,
    plans.spent_usd AS new_spent,
    plan_before.spent_before AS spent_before,
    (
        plan_before.budget_usd IS NOT NULL
        AND plan_before.spent_before < plan_before.budget_usd
        AND plans.spent_usd >= plan_before.budget_usd
    ) AS crossed
"""


# Plan-level sentinel insert (TASK-102). Distinct from
# :data:`_INSERT_EVENT_SQL` because it populates ``plan_id`` instead of
# ``task_id`` and writes the canonical
# ``plan.budget_exceeded`` event_type. ``task_id`` is left NULL —
# permitted since migration 005 relaxed the NOT NULL constraint.
_INSERT_PLAN_EVENT_SQL: str = """
INSERT INTO events (task_id, plan_id, event_type, payload)
VALUES (NULL, $1, $2, $3::jsonb)
"""


# Optimistic-locking FAIL: ``CLAIMED`` | ``IN_PROGRESS`` → ``FAILED``. FAIL
# is allowed from CLAIMED because a worker can crash before issuing START
# (claim → run → die before run_task even forks the subprocess); the
# state-machine reflects this and so must the SQL.
_FAIL_SQL: str = """
UPDATE tasks
SET status = 'FAILED',
    version = tasks.version + 1,
    updated_at = NOW()
WHERE id = $1
  AND version = $2
  AND status IN ('CLAIMED', 'IN_PROGRESS')
RETURNING
    tasks.id,
    tasks.status,
    tasks.dependencies,
    tasks.key_files,
    tasks.priority,
    tasks.description,
    tasks.acceptance_criteria,
    tasks.test_steps,
    tasks.prd_requirement,
    tasks.version
"""


# Optimistic-locking RELEASE for a single task (PRD FR-2.2, FR-2.4,
# TASK-019b2). Targeted release used by the worker on graceful shutdown to
# put its in-flight task back in the pool — distinct from the batch
# visibility-timeout sweep (``release_stale_tasks`` / ``_RELEASE_STALE_SQL``)
# which scans for *any* aged-out claim. Both end up writing a RELEASE event
# with the same shape; the difference is who fires (signal handler vs. sweep)
# and how many rows are touched (one vs. many).
#
# Status filter mirrors :func:`whilly.core.state_machine.apply_transition`'s
# RELEASE rule: allowed from both ``CLAIMED`` (worker received SIGTERM
# between claim and start) and ``IN_PROGRESS`` (signal arrived mid-runner).
# Version-filtered like ``complete_task`` so a sweep that already released
# the row surfaces as :class:`VersionConflictError` — the worker should
# treat that as "already released, nothing to do" and exit cleanly.
_RELEASE_SQL: str = """
UPDATE tasks
SET status = 'PENDING',
    claimed_by = NULL,
    claimed_at = NULL,
    version = tasks.version + 1,
    updated_at = NOW()
WHERE id = $1
  AND version = $2
  AND status IN ('CLAIMED', 'IN_PROGRESS')
RETURNING
    tasks.id,
    tasks.status,
    tasks.dependencies,
    tasks.key_files,
    tasks.priority,
    tasks.description,
    tasks.acceptance_criteria,
    tasks.test_steps,
    tasks.prd_requirement,
    tasks.version
"""


# Optimistic-locking SKIP: ``PENDING`` | ``CLAIMED`` | ``IN_PROGRESS`` →
# ``SKIPPED`` (TASK-104c). Mirrors the SKIP edges in
# :func:`whilly.core.state_machine.apply_transition`. The Decision Gate
# (``whilly.core.gates``) drives this primitive in ``--strict`` mode: a
# REJECT verdict on a freshly-imported plan transitions the offending
# row to SKIPPED without ever spawning a worker for it. The status
# filter intentionally excludes the terminal states ``DONE`` /
# ``FAILED`` / ``SKIPPED`` so a buggy caller cannot drag a finished
# task back through the lifecycle (idempotency on already-SKIPPED is
# handled at the Python layer — see :meth:`TaskRepository.skip_task`,
# which probes the row first and short-circuits to a no-op).
_SKIP_SQL: str = """
UPDATE tasks
SET status = 'SKIPPED',
    claimed_by = NULL,
    claimed_at = NULL,
    version = tasks.version + 1,
    updated_at = NOW()
WHERE id = $1
  AND version = $2
  AND status IN ('PENDING', 'CLAIMED', 'IN_PROGRESS')
RETURNING
    tasks.id,
    tasks.status,
    tasks.dependencies,
    tasks.key_files,
    tasks.priority,
    tasks.description,
    tasks.acceptance_criteria,
    tasks.test_steps,
    tasks.prd_requirement,
    tasks.version
"""


# Heartbeat update for the worker liveness signal (PRD FR-1.6, NFR-1,
# TASK-019b1). Stamps ``last_heartbeat = NOW()`` for the row keyed by
# ``worker_id``. Single-row UPDATE — no transaction wrapper, no audit event:
# heartbeats fire every ~30s for the lifetime of every worker, so writing an
# event row each tick would bloat ``events`` by orders of magnitude without
# adding any audit value beyond the timestamp on ``workers``. The visibility-
# timeout sweep (TASK-025) and the dashboard read ``workers.last_heartbeat``
# directly; that one column is the canonical liveness signal.
#
# Also flips ``status`` back to ``'online'`` (TASK-025b): a worker that the
# offline-worker sweep marked offline can recover transparently — its next
# heartbeat returns it to the active pool. The flip is unconditional rather
# than guarded on ``status = 'offline'`` because: (a) the noop case
# ``'online' → 'online'`` is a single-column overwrite with no side effects,
# (b) the canonical signal is still ``last_heartbeat``, so any future
# observer reading ``status`` knows it's a derived flag that follows
# heartbeat truth.
#
# A missing ``worker_id`` (admin revoked the worker, FK row was deleted) is a
# recoverable state for the caller — we surface "0 rows updated" via the
# return-value bool rather than raising, so the heartbeat loop can log and
# keep going without sprinkling try/except across the worker code.
_UPDATE_HEARTBEAT_SQL: str = """
UPDATE workers
SET last_heartbeat = NOW(),
    status = 'online'
WHERE worker_id = $1
"""


# Worker registration insert (PRD FR-1.1, TASK-021b). Stores the server-issued
# ``worker_id``, the worker-reported ``hostname`` and the *hash* of the
# per-worker bearer token (NEVER the plaintext — PRD NFR-3). ``last_heartbeat``
# and ``registered_at`` default to ``NOW()`` via the schema's server defaults
# so the first heartbeat the worker sends genuinely advances the timestamp.
#
# No ``ON CONFLICT`` clause: ``register_worker`` is the cluster-join entry
# point and a server-generated ``worker_id`` collision is astronomically
# unlikely — see ``register_worker``'s docstring for the entropy budget. If
# it ever does happen, the unique-violation surface lets the caller retry
# with a fresh id rather than silently overwriting another worker's row.
_INSERT_WORKER_SQL: str = """
INSERT INTO workers (worker_id, hostname, token_hash)
VALUES ($1, $2, $3)
"""


# Per-worker bearer validation lookup (TASK-101). Maps a token-hash to the
# owning worker_id. ``LIMIT 1`` is defence-in-depth: migration 004's partial
# UNIQUE index on ``workers (token_hash) WHERE token_hash IS NOT NULL``
# already guarantees at most one row, but the LIMIT keeps the SQL safe even
# in the unlikely scenario where the index has been dropped manually.
# ``token_hash IS NOT NULL`` is implicit (``WHERE token_hash = $1`` cannot
# match a NULL via ``=``) — a revoked worker's row is not selected because
# a NULL never equals anything in SQL.
_LOOKUP_WORKER_BY_TOKEN_HASH_SQL: str = """
SELECT worker_id
FROM workers
WHERE token_hash = $1
LIMIT 1
"""


# Probe used after an optimistic-lock UPDATE returns 0 rows: differentiates
# "row vanished" (FK cascade or test bug) from "version moved" / "wrong
# status". Cheaper than a second UPDATE attempt and gives us enough context
# to build a precise :class:`VersionConflictError`.
_PROBE_TASK_SQL: str = """
SELECT status, version
FROM tasks
WHERE id = $1
"""


# Visibility-timeout sweep (PRD FR-1.4, TASK-009d). One statement does the
# whole job: the CTE flips every CLAIMED / IN_PROGRESS row whose claim is
# older than ``NOW() - $1 seconds`` back to PENDING (clearing claimed_by /
# claimed_at, incrementing version), RETURNING the released ids+versions.
# The outer INSERT then writes one RELEASE event per released row — same
# transaction, same statement, so the audit log can never end up out of sync
# with the tasks table even under network failure between the two writes.
#
# Why not ``FOR UPDATE`` on the inner UPDATE? UPDATE in Postgres already
# acquires the row lock it needs, and the status filter naturally excludes
# rows a worker has just flipped to DONE / FAILED via the optimistic-locking
# path (TASK-009c). A worker's ``complete_task`` UPDATE and our sweep can't
# both succeed against the same row: whichever commits first wins, the other
# matches zero rows. This makes the sweep safe to run concurrently with
# active workers without serialising them behind a FOR UPDATE scan.
#
# ``$1::int`` is the visibility timeout in seconds; we cast inside SQL so
# asyncpg can pass a plain Python int without needing an interval converter.
# ``make_interval(secs => ...)`` is preferred over string concatenation here
# (no SQL-injection surface, no locale-dependent parsing).
_RELEASE_STALE_SQL: str = """
WITH released AS (
    UPDATE tasks
    SET status = 'PENDING',
        claimed_by = NULL,
        claimed_at = NULL,
        version = tasks.version + 1,
        updated_at = NOW()
    WHERE status IN ('CLAIMED', 'IN_PROGRESS')
      AND claimed_at IS NOT NULL
      AND claimed_at < NOW() - make_interval(secs => $1::int)
    RETURNING id, version
)
INSERT INTO events (task_id, event_type, payload)
SELECT id, $2, jsonb_build_object('reason', $3::text, 'version', version)
FROM released
RETURNING task_id
"""


# Offline-worker sweep (PRD FR-1.4, NFR-1, SC-2, TASK-025b). Two-step CTE in
# one round-trip: flip every still-``online`` worker whose last heartbeat
# predates ``NOW() - $1 seconds`` to ``offline`` (RETURNING worker_id), then
# join those ids back into ``tasks`` to release every CLAIMED / IN_PROGRESS
# row that worker owned. The final INSERT writes one RELEASE event per
# released task carrying ``payload = {"reason": "worker_offline", "version":
# <new>, "worker_id": <wid>}`` so dashboards (TASK-027) and post-mortems can
# surface *why* the bounce happened without joining ``events`` to ``workers``
# at read time.
#
# Why the worker UPDATE *first*, not after the task release?
#     If we released tasks first and then flipped workers, a heartbeat that
#     arrived between the two writes (new worker process re-using the old
#     ``worker_id``? unlikely but not impossible under cluster restart)
#     would put the row back to ``online`` while we're still mid-release —
#     and the audit log would carry ``worker_offline`` for a worker the
#     workers row says is healthy. Doing the worker UPDATE first means the
#     RETURNING set is the *committed-as-offline* cohort; the task release
#     and audit insert reference exactly that cohort, by design.
#
# Why ``status = 'online'`` in the WHERE clause?
#     Re-running the sweep against an already-offline worker would write a
#     second batch of RELEASE events for tasks that are already PENDING (or
#     have been re-claimed by another worker). Filtering on ``status =
#     'online'`` makes the sweep idempotent — a worker that comes back to
#     life will heartbeat itself back to online (TASK-019b1 / TASK-022b2)
#     and the next stale window starts the cycle fresh.
#
# Concurrency with active workers (PRD FR-2.4)
# --------------------------------------------
# Same lock-free contract as :data:`_RELEASE_STALE_SQL`: the sweep does not
# take row locks. A concurrent ``complete_task`` / ``fail_task`` from the
# (mid-die) worker process either commits before our UPDATE matches the
# row (status filter excludes it — we silently skip), or commits after
# (the worker's UPDATE matches zero rows because we advanced the version,
# and surfaces :class:`VersionConflictError`). Exactly one writer wins.
#
# ``$1::int`` is the heartbeat-staleness threshold in seconds (2 minutes by
# default — well under the 15 min visibility timeout, so this is the
# *primary* fault-tolerance signal and the visibility timeout is the
# fallback for cases where a worker's heartbeat is somehow live but its
# claim is genuinely stuck). ``$2`` is :data:`Transition.RELEASE.value`
# (string), ``$3`` is ``"worker_offline"`` (the audit reason).
_RELEASE_OFFLINE_WORKERS_SQL: str = """
WITH offline_workers AS (
    UPDATE workers
    SET status = 'offline'
    WHERE status = 'online'
      AND last_heartbeat < NOW() - make_interval(secs => $1::int)
    RETURNING worker_id
),
released AS (
    UPDATE tasks
    SET status = 'PENDING',
        claimed_by = NULL,
        claimed_at = NULL,
        version = tasks.version + 1,
        updated_at = NOW()
    FROM offline_workers
    WHERE tasks.claimed_by = offline_workers.worker_id
      AND tasks.status IN ('CLAIMED', 'IN_PROGRESS')
    RETURNING tasks.id, tasks.version, offline_workers.worker_id
)
INSERT INTO events (task_id, event_type, payload)
SELECT id, $2, jsonb_build_object('reason', $3::text, 'version', version, 'worker_id', worker_id)
FROM released
RETURNING task_id
"""


# Plan reset (TASK-103). Two modes:
#
# * ``keep-tasks`` — soft reset. Wipe the events table for every task in
#   the plan, flip every task back to ``PENDING`` (clearing claim
#   ownership, bumping ``version``), then write one ``RESET`` event per
#   task carrying ``payload = {"reason": "manual_reset", "mode":
#   "keep_tasks", "version": <new>}``. Useful for replaying a
#   debug-stuck plan from scratch without re-importing the JSON.
#
# * ``hard`` — DELETE the plan row; ON DELETE CASCADE on the FK chain
#   (plans → tasks → events) wipes everything in one statement. Useful
#   when the plan JSON itself changed and an idempotent re-import would
#   otherwise refuse to clobber existing rows (DO NOTHING semantics).
#   No audit row is written: the events table is gone with the rest of
#   the plan, so any RESET row would be deleted before commit anyway —
#   operators relying on durable audit trail should use ``keep-tasks``
#   or rely on the file-based mirror (TASK-106).
#
# All three statements (events DELETE, tasks UPDATE, RESET INSERT) run
# inside a single Python-level ``async with conn.transaction()`` rather
# than one combined CTE because per-statement diagnostics matter on the
# operator-facing reset path: a CHECK violation should surface with the
# offending statement's verb in the error, not as a generic CTE failure.
# The transaction wrapper provides the same atomicity guarantee as the
# combined CTE form would.
_RESET_DELETE_EVENTS_SQL: str = """
DELETE FROM events
WHERE task_id IN (SELECT id FROM tasks WHERE plan_id = $1)
"""

_RESET_UPDATE_TASKS_SQL: str = """
UPDATE tasks
SET status = 'PENDING',
    claimed_by = NULL,
    claimed_at = NULL,
    version = tasks.version + 1,
    updated_at = NOW()
WHERE plan_id = $1
RETURNING id, version
"""

# ``DELETE FROM plans WHERE id = $1`` cascades through the FK chain
# (tasks ON DELETE CASCADE, events ON DELETE CASCADE on tasks.id) so
# one statement clears the entire plan. The pre-DELETE COUNT lets us
# return a row count to the CLI for the operator-facing summary line.
_RESET_COUNT_TASKS_SQL: str = "SELECT COUNT(*)::int AS c FROM tasks WHERE plan_id = $1"
_RESET_DELETE_PLAN_SQL: str = "DELETE FROM plans WHERE id = $1"


class VersionConflictError(Exception):
    """Optimistic-locking mismatch on a :class:`TaskRepository` mutation.

    Raised by :meth:`TaskRepository.complete_task` and
    :meth:`TaskRepository.fail_task` when the ``WHERE id = $1 AND version = $2
    AND status IN (...)`` filter matches zero rows. We do a single follow-up
    SELECT to distinguish the cause:

    * ``actual_version is None`` → row is gone (likely FK cascade from a
      ``DELETE plans WHERE id = ...`` in a test, or a misconfigured caller).
    * ``actual_version != expected_version`` → another writer advanced the
      counter first; the canonical "lost update" case.
    * ``actual_version == expected_version`` → version is fine but ``status``
      disallows the requested transition (e.g. trying to COMPLETE on a row
      that's already ``DONE``).

    Carrying all three fields means the caller (FastAPI handler in TASK-021c,
    worker in TASK-019a) can decide whether to retry, surface a 409, or log
    and move on without re-running the SELECT itself.
    """

    def __init__(
        self,
        task_id: TaskId,
        expected_version: int,
        actual_version: int | None,
        actual_status: TaskStatus | None,
    ) -> None:
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.actual_status = actual_status
        if actual_version is None:
            detail = "task not found"
        elif actual_version != expected_version:
            detail = f"version moved past expected {expected_version}; current is {actual_version}"
        else:
            detail = f"status {actual_status.value if actual_status else '<unknown>'} disallows this transition"
        super().__init__(f"VersionConflict on task {task_id!r}: {detail}")


def _decode_jsonb(raw: Any) -> Any:
    """Decode an asyncpg JSONB column value to a native Python list/dict.

    asyncpg returns JSONB as ``str`` (the raw JSON text) unless a codec has
    been registered on the connection. We parse with stdlib :mod:`json` here
    so the repository works whether or not a codec is installed — matters
    because pool.py (TASK-009a) does not register one and we don't want to
    couple TASK-009b to that decision.

    ``None`` round-trips as ``None`` (column is NOT NULL in the schema, but
    defensive); already-decoded ``list``/``dict`` also pass through.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    return json.loads(raw)


def _row_to_task(row: asyncpg.Record) -> Task:
    """Map a ``tasks``-table row to the immutable :class:`Task` value object.

    Tuple conversions are deliberate: :class:`Task` defaults its collection
    fields to tuples so the frozen dataclass stays effectively immutable
    (``frozen=True`` only blocks attribute reassignment, not list mutation).
    Empty / missing JSONB arrays normalise to ``()``.
    """
    deps = _decode_jsonb(row["dependencies"]) or ()
    key_files = _decode_jsonb(row["key_files"]) or ()
    acceptance = _decode_jsonb(row["acceptance_criteria"]) or ()
    test_steps = _decode_jsonb(row["test_steps"]) or ()
    return Task(
        id=row["id"],
        status=TaskStatus(row["status"]),
        dependencies=tuple(deps),
        key_files=tuple(key_files),
        priority=Priority(row["priority"]),
        description=row["description"],
        acceptance_criteria=tuple(acceptance),
        test_steps=tuple(test_steps),
        prd_requirement=row["prd_requirement"],
        version=row["version"],
    )


class TaskRepository:
    """Postgres adapter for the Task aggregate root.

    Constructed once per process with the asyncpg pool from
    :func:`whilly.adapters.db.pool.create_pool`. Methods acquire connections
    from the pool on demand and release them automatically — callers never
    handle raw connections.

    Concurrency model
    -----------------
    Every mutating method runs inside ``async with conn.transaction()``. SQL
    queues are notoriously sensitive to "I read it, then it changed" races;
    using one transaction per method (rather than per call site) keeps the
    contract local: a method either commits an atomic state transition + its
    audit-event row, or rolls back both.

    The pool itself is left for the caller (the FastAPI lifespan in
    TASK-021a, or test fixtures) to close — the repository does not own the
    pool's lifecycle.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def claim_task(self, worker_id: WorkerId, plan_id: PlanId) -> Task | None:
        """Atomically claim one ``PENDING`` task from ``plan_id`` for ``worker_id``.

        Returns the post-update :class:`Task` (status ``CLAIMED``,
        ``version`` incremented by 1) on success, or ``None`` if no PENDING
        rows are available — either because the plan is exhausted or because
        every candidate is currently locked by another claimer.

        Side effects on success:

        * ``tasks`` row: ``status = CLAIMED``, ``claimed_by = worker_id``,
          ``claimed_at = NOW()``, ``version += 1``, ``updated_at = NOW()``.
        * ``events`` row: ``event_type = 'CLAIM'`` with payload
          ``{"worker_id": ..., "version": <new>}``.

        Both writes run in a single ``BEGIN`` / ``COMMIT`` block so an
        observer never sees a CLAIMED row without its corresponding CLAIM
        event, and a failed event INSERT rolls the row update back to
        PENDING with no half-state to clean up.

        ``worker_id`` must already exist in the ``workers`` table — that's a
        FK constraint (``ON DELETE SET NULL``) seeded by
        ``POST /workers/register`` in TASK-021b. Tests that exercise this
        method directly need to insert a workers row first; otherwise the
        INSERT-side FK fires and asyncpg surfaces
        :class:`asyncpg.exceptions.ForeignKeyViolationError`.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_CLAIM_SQL, plan_id, worker_id)
                if row is None:
                    logger.debug(
                        "claim_task: no PENDING rows in plan %s for worker %s",
                        plan_id,
                        worker_id,
                    )
                    return None

                payload = json.dumps({"worker_id": worker_id, "version": row["version"]})
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    row["id"],
                    Transition.CLAIM.value,
                    payload,
                )
                logger.info(
                    "claim_task: worker=%s claimed task=%s plan=%s version=%d",
                    worker_id,
                    row["id"],
                    plan_id,
                    row["version"],
                )
                return _row_to_task(row)

    async def start_task(self, task_id: TaskId, version: int) -> Task:
        """Atomically transition ``task_id`` from ``CLAIMED`` → ``IN_PROGRESS``.

        Called by the local worker (TASK-019a) immediately after a successful
        ``claim_task`` and before invoking the agent runner. Two reasons it's
        a separate round-trip rather than folded into ``claim_task``:

        * It marks the moment the worker actually starts running the agent,
          not the moment it took ownership. The visibility-timeout sweep
          (PRD FR-1.4) treats ``CLAIMED`` and ``IN_PROGRESS`` identically for
          aging, but heartbeats (TASK-019b1) and the dashboard (TASK-027)
          care about the distinction.
        * It fits the optimistic-locking lattice: ``complete_task`` requires
          ``status = 'IN_PROGRESS'``, so without this hop the happy path
          would have to relax that filter and lose its strong contract.

        Same lock-free contract as :meth:`complete_task`: the UPDATE filters
        on ``version`` and ``status``, RETURNING ships the post-update row,
        and a 0-row result triggers :class:`VersionConflictError` after a
        single follow-up SELECT to classify the cause (lost update vs. wrong
        status vs. row missing). A ``START`` event row is appended in the
        same transaction so the audit log can never disagree with the tasks
        table.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_START_SQL, task_id, version)
                if row is None:
                    await self._raise_version_conflict(conn, task_id, version)

                payload = json.dumps({"version": row["version"]})
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    row["id"],
                    Transition.START.value,
                    payload,
                )
                logger.info(
                    "start_task: task=%s version=%d → IN_PROGRESS",
                    row["id"],
                    row["version"],
                )
                return _row_to_task(row)

    async def complete_task(
        self,
        task_id: TaskId,
        version: int,
        cost_usd: Decimal | float | int | None = None,
    ) -> Task:
        """Atomically transition ``task_id`` from ``IN_PROGRESS`` → ``DONE``.

        Optimistic-locking contract: the UPDATE only fires when the row's
        current ``version`` matches the ``version`` argument *and* the row's
        status is ``IN_PROGRESS``. On success the row's version is
        incremented by 1, status is set to ``DONE``, and a ``COMPLETE`` event
        row is appended in the same transaction.

        Plan budget guard (TASK-102)
        ----------------------------
        When ``cost_usd > 0``, the same transaction *also* increments
        ``plans.spent_usd`` by the supplied cost (VAL-BUDGET-030 /
        VAL-BUDGET-031) — atomic with the task transition so a failed
        audit insert rolls *both* the task flip and the spend update
        back together. ``cost_usd = 0`` (the default for back-compat
        callers and the documented "missing usage envelope" case,
        VAL-BUDGET-032) skips the plan UPDATE entirely — no spurious
        wire round-trip, no locking on the parent plan, no spend-time
        delta.

        Sentinel emission (VAL-BUDGET-040 / 041)
        ----------------------------------------
        On the same call that crosses ``spent_usd`` from strictly below
        ``budget_usd`` to ``>= budget_usd`` (and only on that call), a
        single ``plan.budget_exceeded`` event row is written with
        ``task_id IS NULL`` and ``plan_id`` populated. Subsequent
        completes against the same plan see the pre-update spend
        already at-or-above budget and emit no further sentinels —
        the boolean ``crossed`` returned by :data:`_INCREMENT_SPEND_SQL`
        is the single source of truth here. Plans with
        ``budget_usd IS NULL`` (unlimited, VAL-BUDGET-042) never emit
        the sentinel.

        Concurrency (VAL-BUDGET-050 / 052)
        ----------------------------------
        Two concurrent completes for last-budget-cents serialise on
        ``plans.id`` via ``FOR UPDATE`` inside :data:`_INCREMENT_SPEND_SQL`.
        Each call observes a distinct pre-update ``spent_before`` —
        the first sees ``spent_before < budget_usd`` (and crosses);
        the second sees ``spent_before >= budget_usd`` already (no
        crossing). Both completes succeed (the per-task optimistic
        lock filter is independent of the plan-spend serialisation),
        ``spent_usd`` accumulates exactly the sum of both costs, and
        exactly one sentinel is written.

        Args
        ----
        task_id:
            Task to complete.
        version:
            Caller's last-seen version. Standard optimistic-locking
            guard.
        cost_usd:
            Spend amount to add to ``plans.spent_usd``. Accepts
            ``Decimal``, ``int``, ``float``, or ``None``. ``None`` /
            ``0`` is the no-op spend path (VAL-BUDGET-032). Negative
            values are rejected (strict-monotonic invariant,
            VAL-BUDGET-072). Quantised to the column's NUMERIC(10, 4)
            precision before storage.

        Raises
        ------
        VersionConflictError
            On 0-row UPDATE — see classification on
            :class:`VersionConflictError`.
        """
        cost = _coerce_cost_usd(cost_usd)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_COMPLETE_SQL, task_id, version)
                if row is None:
                    await self._raise_version_conflict(conn, task_id, version)

                payload = json.dumps({"version": row["version"]})
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    row["id"],
                    Transition.COMPLETE.value,
                    payload,
                )

                # Plan-spend update + sentinel emission (TASK-102).
                # ``cost == 0`` short-circuits to avoid a spurious
                # plan UPDATE — the AC for VAL-BUDGET-032 explicitly
                # requires that path to be a *no-op spend*. We still
                # complete the task and write the COMPLETE event;
                # only the budget-side write is skipped.
                if cost > 0:
                    plan_id: PlanId = row["plan_id"]
                    spend_row = await conn.fetchrow(
                        _INCREMENT_SPEND_SQL,
                        plan_id,
                        cost,
                    )
                    # ``spend_row`` is None only if the plan was
                    # deleted between the task UPDATE and our spend
                    # UPDATE — extremely narrow race (FK cascade
                    # would have wiped the task too); log loudly
                    # and skip the sentinel rather than corrupt
                    # the audit log with a sentinel referencing a
                    # gone plan.
                    if spend_row is None:
                        logger.warning(
                            "complete_task: plan %s vanished during spend update for task=%s — skipping sentinel",
                            plan_id,
                            row["id"],
                        )
                    elif spend_row["crossed"]:
                        sentinel_payload = json.dumps(
                            {
                                "plan_id": plan_id,
                                "budget_usd": str(spend_row["budget_usd"]),
                                "spent_usd": str(spend_row["new_spent"]),
                                "crossing_task_id": row["id"],
                                "reason": BUDGET_EXCEEDED_REASON,
                                "threshold_pct": BUDGET_EXCEEDED_THRESHOLD_PCT,
                            }
                        )
                        await conn.execute(
                            _INSERT_PLAN_EVENT_SQL,
                            plan_id,
                            BUDGET_EXCEEDED_EVENT_TYPE,
                            sentinel_payload,
                        )
                        logger.info(
                            "complete_task: plan=%s spent_usd=%s crossed budget_usd=%s — emitted %s sentinel",
                            plan_id,
                            spend_row["new_spent"],
                            spend_row["budget_usd"],
                            BUDGET_EXCEEDED_EVENT_TYPE,
                        )

                logger.info(
                    "complete_task: task=%s version=%d cost_usd=%s → DONE",
                    row["id"],
                    row["version"],
                    cost,
                )
                return _row_to_task(row)

    async def fail_task(
        self,
        task_id: TaskId,
        version: int,
        reason: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> Task:
        """Atomically transition ``task_id`` from ``CLAIMED`` | ``IN_PROGRESS`` → ``FAILED``.

        Mirrors :meth:`complete_task` but accepts both pre-START and
        post-START source states (the worker may crash before run_task even
        forks the agent — the state-machine in core/state_machine.py
        encodes this and the SQL filter mirrors the rule).

        ``reason`` is persisted as the FAIL event payload so the dashboard
        (TASK-027) and post-mortem queries can surface a human-readable
        cause without re-scanning logs. The audit row goes into the same
        transaction as the status flip — observers either see both or
        neither, never just the FAILED status with no event explaining why.

        Raises :class:`VersionConflictError` on optimistic-lock mismatch
        (same three-way classification as :meth:`complete_task`).

        TASK-104b extensions
        --------------------
        ``detail`` (keyword-only) accepts a free-form dict that is
        persisted into the new ``events.detail`` JSONB column (migration
        003). When ``detail`` is ``None`` (the default), the column is
        SQL ``NULL`` rather than the literal JSON ``null`` or the empty
        object ``{}`` (VAL-TRIZ-009). Reserved keys live on
        ``events.payload`` (``version`` / ``reason``); ``detail`` is
        free-form caller diagnostics.

        Per-task TRIZ hook
        ~~~~~~~~~~~~~~~~~~
        After the FAIL transition commits, the method optionally
        invokes :func:`whilly.core.triz.analyze_contradiction_with_outcome`
        on the post-update :class:`Task` and writes a follow-up
        ``triz.contradiction`` (positive verdict) or ``triz.error``
        (timeout) event row carrying the finding shape in ``detail``.
        Gated by ``WHILLY_TRIZ_ENABLED=1`` (off by default in tests, on
        in prod-like config). Fail-open contract (VAL-TRIZ-015): the hook
        never re-raises into the caller for any of the documented soft-
        fail modes (claude absent, timeout, malformed JSON, claude
        non-zero exit).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_FAIL_SQL, task_id, version)
                if row is None:
                    await self._raise_version_conflict(conn, task_id, version)

                payload = json.dumps({"version": row["version"], "reason": reason})
                detail_jsonb = json.dumps(detail) if detail is not None else None
                await conn.execute(
                    _INSERT_EVENT_WITH_DETAIL_SQL,
                    row["id"],
                    Transition.FAIL.value,
                    payload,
                    detail_jsonb,
                )
                logger.info(
                    "fail_task: task=%s version=%d reason=%r → FAILED",
                    row["id"],
                    row["version"],
                    reason,
                )
                updated = _row_to_task(row)

        # TRIZ hook runs *after* the FAIL commit (VAL-TRIZ-010 ordering
        # contract: FAIL row's ``created_at`` ≤ ``triz.contradiction``
        # row's ``created_at``). Running outside the transaction also
        # guarantees a TRIZ failure cannot roll the FAIL transition back
        # — VAL-TRIZ-003 requires the FAIL event row preserved when
        # claude is absent.
        if os.environ.get("WHILLY_TRIZ_ENABLED") == "1":
            await self._maybe_emit_triz_event(updated)
        return updated

    async def _maybe_emit_triz_event(self, task: Task) -> None:
        """Optional per-task TRIZ analyser hook (TASK-104b).

        Subprocesses the ``claude`` CLI via
        :func:`whilly.core.triz.analyze_contradiction_with_outcome` and
        writes a follow-up event row depending on the outcome:

        * positive verdict — one ``triz.contradiction`` event with
          ``detail = {"contradiction_type": ..., "reason": ...}``.
        * subprocess timeout — one ``triz.error`` event with
          ``detail = {"reason": "timeout"}``.
        * everything else (claude absent, malformed JSON, claude
          non-zero exit, no contradiction found) — no event row.

        Fail-open: every exception (analyzer-side AND DB-side) is
        swallowed with a WARNING log so the FAIL transition that
        already committed stays observable.
        """
        # Local import keeps cold-start cost off the import graph for
        # workloads that never invoke fail_task with the env flag set.
        from whilly.core.triz import (  # noqa: PLC0415 — see comment above
            ERROR_REASON_TIMEOUT,
            analyze_contradiction_with_outcome,
        )

        try:
            outcome = analyze_contradiction_with_outcome(task)
        except Exception as exc:  # noqa: BLE001 — fail-open contract
            logger.warning(
                "triz hook: analyzer raised unexpectedly: %r — skipping event",
                exc,
            )
            return

        if outcome.finding is not None:
            event_type = "triz.contradiction"
            detail_payload: dict[str, Any] = {
                "contradiction_type": outcome.finding.contradiction_type,
                "reason": outcome.finding.reason,
            }
        elif outcome.error_reason == ERROR_REASON_TIMEOUT:
            event_type = "triz.error"
            detail_payload = {"reason": "timeout"}
        else:
            # No contradiction OR a soft-fail mode (claude absent,
            # parse error, non-zero exit) that we deliberately do NOT
            # surface as an event row (VAL-TRIZ-003 / VAL-TRIZ-005).
            return

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    _INSERT_EVENT_WITH_DETAIL_SQL,
                    task.id,
                    event_type,
                    json.dumps({}),
                    json.dumps(detail_payload),
                )
        except Exception as exc:  # noqa: BLE001 — fail-open contract
            logger.warning(
                "triz hook: failed to write %s event for task=%s: %r",
                event_type,
                task.id,
                exc,
            )

    async def release_task(self, task_id: TaskId, version: int, reason: str) -> Task:
        """Atomically transition ``task_id`` from ``CLAIMED`` | ``IN_PROGRESS`` → ``PENDING``.

        Targeted single-task release used by the worker on graceful shutdown
        (TASK-019b2): when the local worker receives SIGTERM / SIGINT mid-
        runner, it cancels the agent and calls this method to put the task
        back in the pool so a peer worker (or this worker on restart) can
        pick it up cleanly. Distinct from :meth:`release_stale_tasks` —
        which is the *batch* visibility-timeout sweep — by being targeted at
        a single known row with the caller's expected ``version``.

        On success the row's ``status`` flips to ``PENDING``,
        ``claimed_by`` / ``claimed_at`` are cleared, ``version`` is
        incremented, and a ``RELEASE`` event is appended carrying
        ``payload = {"reason": <reason>, "version": <new>}`` — same shape
        as the sweep's audit row so dashboards / post-mortems don't have
        to special-case the source.

        Concurrency contract (PRD FR-2.4)
        ---------------------------------
        Mirrors :meth:`complete_task`: the UPDATE filters by both
        ``version`` and ``status``, RETURNING ships the post-update row,
        and a 0-row result triggers :class:`VersionConflictError` after a
        single follow-up SELECT to classify the cause:

        * **lost update** — the visibility-timeout sweep released the row
          first; ``actual_version`` is one ahead and ``actual_status`` is
          ``PENDING``. The worker should treat this as "already released,
          nothing to do" and exit.
        * **wrong status** — the worker already finished / failed the task
          before the signal handler reached this method (extremely
          narrow race; not impossible). The terminal status wins.
        * **task missing** — FK cascade in tests; same as the other
          methods.

        Args
        ----
        task_id:
            The task to release. Must currently be ``CLAIMED`` or
            ``IN_PROGRESS`` for the UPDATE to match.
        version:
            Caller's last-seen version (typically the value returned by
            ``start_task`` / ``claim_task``). Used for optimistic locking.
        reason:
            Human-readable cause for the release. Persisted into the
            ``RELEASE`` event payload — distinguishes
            ``"shutdown"`` (this method) from ``"visibility_timeout"``
            (the sweep) so the dashboard can show why a task bounced.

        Returns
        -------
        Task
            The post-update task with ``status = PENDING``,
            ``version`` incremented.

        Raises
        ------
        VersionConflictError
            On a 0-row UPDATE — see classification above.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_RELEASE_SQL, task_id, version)
                if row is None:
                    await self._raise_version_conflict(conn, task_id, version)

                payload = json.dumps({"version": row["version"], "reason": reason})
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    row["id"],
                    Transition.RELEASE.value,
                    payload,
                )
                logger.info(
                    "release_task: task=%s version=%d reason=%r → PENDING",
                    row["id"],
                    row["version"],
                    reason,
                )
                return _row_to_task(row)

    async def skip_task(
        self,
        task_id: TaskId,
        version: int,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> Task:
        """Atomically transition ``task_id`` to ``SKIPPED`` (TASK-104c).

        Drives the Decision Gate ``--strict`` mode (``whilly plan apply
        --strict``): a REJECT verdict from
        :func:`whilly.core.gates.evaluate_decision_gate` calls this
        method to mark the offending row SKIPPED without ever spawning
        a worker for it. The state machine
        (:func:`whilly.core.state_machine.apply_transition`) allows the
        SKIP edge from ``PENDING``, ``CLAIMED`` and ``IN_PROGRESS``;
        the SQL filter in :data:`_SKIP_SQL` mirrors that lattice.

        Side effects on success:

        * ``tasks`` row: ``status = 'SKIPPED'``, ``claimed_by`` /
          ``claimed_at`` cleared (so the SKIPPED row no longer
          appears under any worker's claim), ``version`` incremented,
          ``updated_at = NOW()``.
        * ``events`` row: ``event_type = 'SKIP'`` with payload
          ``{"version": <new>, "reason": <reason>, **detail}``. The
          ``detail`` dict (typically ``{"missing": [...]}`` from the
          gate verdict) is *merged* into the payload at the top
          level so audit-time queries can ``payload->'missing'`` /
          ``payload->>'reason'`` without a deeper jsonb walk. Reserved
          keys (``version`` / ``reason``) are *not* overwritten by
          ``detail`` — the canonical fields stay authoritative.

        Both writes happen inside one ``async with conn.transaction()``
        so an observer never sees a SKIPPED row without its
        corresponding SKIP event, and a failed event INSERT rolls the
        row update back with no half-state to clean up.

        Idempotency
        -----------
        Re-invoking ``skip_task`` on a row that is already ``SKIPPED``
        returns the existing :class:`Task` value (with whatever
        ``version`` it currently has) **without** writing a duplicate
        event row and **without** raising. This is required by the
        PRD AC ("idempotent on already-skipped") and matches the
        operational reality that the strict gate may run multiple
        times against the same plan during operator iteration. The
        idempotency check happens *before* the optimistic-lock
        UPDATE, so the caller's ``version`` argument can stale —
        once a row is terminal-SKIPPED, the version no longer
        matters.

        Errors
        ------
        * :class:`VersionConflictError` — raised in three cases (same
          three-way classification as :meth:`complete_task` /
          :meth:`fail_task`):

            * ``actual_version is None`` → the row was deleted (FK
              cascade in tests, or a misconfigured caller).
            * ``actual_version != expected_version`` → another writer
              advanced the counter first.
            * ``actual_version == expected_version`` → the row is in a
              terminal state (``DONE`` / ``FAILED``) that disallows
              SKIP. This satisfies the AC "raises on terminal".

        Args
        ----
        task_id:
            The task to skip. Must currently be ``PENDING``,
            ``CLAIMED``, ``IN_PROGRESS``, or already ``SKIPPED`` (the
            no-op idempotent path).
        version:
            Caller's last-seen version. Ignored when the row is
            already ``SKIPPED`` (idempotent short-circuit); used as
            the optimistic-lock guard otherwise.
        reason:
            Short string identifying the source of the skip; persisted
            into the ``payload->>'reason'`` field. Conventional
            values: ``"decision_gate_failed"`` (the strict gate
            uses this), ``"manual_skip"`` (operator-driven).
        detail:
            Optional extra payload keys to merge into the SKIP event
            (typically ``{"missing": [...]}`` from a
            :class:`whilly.core.gates.GateVerdict`). Reserved keys
            ``version`` / ``reason`` are not overridable from
            ``detail`` — they always carry the post-update values.

        Returns
        -------
        Task
            The post-update :class:`Task` with ``status = SKIPPED``;
            ``version`` is the row's current value (incremented on
            the first call, preserved as-is on idempotent replays).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Idempotency probe: a row already in SKIPPED returns
                # the canonical Task value without re-writing the
                # status or appending another event row. Probing
                # before the UPDATE means a stale ``version`` argument
                # on a re-run still hits the no-op path — operators
                # re-running the gate after editing the JSON file
                # don't need to worry about version drift.
                probe = await conn.fetchrow(
                    """
                    SELECT id, status, dependencies, key_files, priority,
                           description, acceptance_criteria, test_steps,
                           prd_requirement, version
                    FROM tasks WHERE id = $1
                    """,
                    task_id,
                )
                if probe is not None and probe["status"] == TaskStatus.SKIPPED.value:
                    logger.info(
                        "skip_task: task=%s already SKIPPED — no-op (idempotent)",
                        task_id,
                    )
                    return _row_to_task(probe)

                row = await conn.fetchrow(_SKIP_SQL, task_id, version)
                if row is None:
                    await self._raise_version_conflict(conn, task_id, version)

                # Build the SKIP event payload. Reserved fields go
                # last so they cannot be overwritten by a malicious /
                # buggy ``detail`` dict.
                payload_dict: dict[str, Any] = {}
                if detail:
                    payload_dict.update(detail)
                payload_dict["version"] = row["version"]
                payload_dict["reason"] = reason
                payload = json.dumps(payload_dict)
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    row["id"],
                    Transition.SKIP.value,
                    payload,
                )
                logger.info(
                    "skip_task: task=%s version=%d reason=%r → SKIPPED",
                    row["id"],
                    row["version"],
                    reason,
                )
                return _row_to_task(row)

    async def release_stale_tasks(self, visibility_timeout_seconds: int) -> int:
        """Return ``CLAIMED`` / ``IN_PROGRESS`` tasks whose claim has aged out.

        Implements the visibility-timeout sweep (PRD FR-1.4): any row whose
        ``claimed_at`` predates ``NOW() - visibility_timeout_seconds`` is
        flipped back to ``PENDING`` with ``claimed_by`` / ``claimed_at``
        cleared, ``version`` incremented, and a ``RELEASE`` event row
        appended carrying ``payload = {"reason": "visibility_timeout",
        "version": <new>}``. Returns the number of rows released so the
        background-task loop in TASK-025 can log / surface metrics.

        Single round-trip: the UPDATE and the audit-event INSERT run as one
        SQL statement (CTE + ``INSERT ... SELECT FROM released``). That's
        important because the sweep operates on a *batch* of rows — looping
        in Python would either need a transaction-wide lock (slow) or expose
        a window where some rows are PENDING again but their RELEASE event
        hasn't been written yet (audit drift).

        Concurrency with active workers (PRD FR-2.4)
        --------------------------------------------
        The sweep does *not* take row locks (no ``FOR UPDATE``). It races
        against worker mutations through the optimistic-locking lattice:

        * If a worker's ``complete_task`` / ``fail_task`` commits first, the
          row is no longer ``CLAIMED`` / ``IN_PROGRESS`` and our status
          filter excludes it — the sweep silently skips it. This is the
          desired outcome: the worker finished in time, no release needed.
        * If the sweep commits first, the worker's UPDATE matches zero rows
          (status flipped from ``IN_PROGRESS`` to ``PENDING``, version
          advanced) and surfaces :class:`VersionConflictError` —
          differentiated as "wrong status" via the probe, so the worker can
          drop the result and re-claim cleanly.

        Either way exactly one of the two writers wins; there is no path
        where both succeed and produce a duplicate / inconsistent state.

        Args:
            visibility_timeout_seconds: Age threshold in seconds. Rows with
                ``claimed_at < NOW() - this`` are released. Must be a
                non-negative integer; ``0`` releases every active claim
                (useful in tests with controlled clocks).

        Returns:
            Number of rows released (and corresponding RELEASE events
            written). ``0`` is the normal "nothing stale" outcome, not an
            error.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    _RELEASE_STALE_SQL,
                    visibility_timeout_seconds,
                    Transition.RELEASE.value,
                    "visibility_timeout",
                )
                released = len(rows)
                if released:
                    logger.info(
                        "release_stale_tasks: visibility_timeout=%ds released %d task(s): %s",
                        visibility_timeout_seconds,
                        released,
                        [row["task_id"] for row in rows],
                    )
                else:
                    logger.debug(
                        "release_stale_tasks: visibility_timeout=%ds — no stale claims",
                        visibility_timeout_seconds,
                    )
                return released

    async def release_offline_workers(self, heartbeat_timeout_seconds: int) -> int:
        """Mark stale-heartbeat workers ``offline`` and release their in-flight tasks.

        Implements the offline-worker recovery path (PRD FR-1.4, NFR-1,
        SC-2, TASK-025b): every worker whose ``last_heartbeat`` predates
        ``NOW() - heartbeat_timeout_seconds`` and is still ``online`` is
        flipped to ``offline``; every ``CLAIMED`` / ``IN_PROGRESS`` task
        owned by that cohort is flipped back to ``PENDING`` (clearing
        ``claimed_by`` / ``claimed_at``, incrementing ``version``); and a
        ``RELEASE`` event is appended per released task with ``payload =
        {"reason": "worker_offline", "version": <new>, "worker_id":
        <wid>}``. All three writes happen in a single SQL statement so
        the audit log can never disagree with either the workers row or
        the tasks row.

        Why a separate sweep from :meth:`release_stale_tasks`?
            Both eventually flip orphaned claims back to ``PENDING``, but
            they run on different timing budgets and observe different
            signals:

            * :meth:`release_stale_tasks` (visibility timeout) fires on
              ``claimed_at`` aging — 15 min by default. Catches the
              edge case where a heartbeat is live but a task is
              genuinely stuck (agent process hung mid-step but the
              heartbeat coroutine still ticking).
            * :meth:`release_offline_workers` (heartbeat staleness) fires
              on ``last_heartbeat`` aging — 2 min by default. Catches
              the *common* case: worker process killed (OOM, SIGKILL,
              host reboot). 7.5x faster recovery means SC-2's "kill a
              worker mid-task, peer worker picks up within seconds, not
              minutes" lands without forcing the visibility timeout
              down to 30s (which would risk releasing live work whose
              heartbeat just got delayed under event-loop pressure).

            Running both in the same lifespan TaskGroup keeps the
            supervision boundary explicit; the two sweeps don't conflict
            (the heartbeat sweep marks a worker offline first, so the
            visibility-timeout sweep that follows wouldn't see those
            tasks as still-CLAIMED — they're already PENDING).

        Idempotency
            The worker UPDATE filters on ``status = 'online'`` so a
            re-run against a worker already flipped to ``offline`` is a
            no-op — no double-RELEASE events, no spurious version
            bumps. A worker that comes back to life heartbeats itself
            back to online (TASK-019b1 for the local worker,
            TASK-022b2 for the remote one) and the next stale window
            starts cleanly.

        Concurrency contract
            Lock-free, same as :meth:`release_stale_tasks`: a worker
            mid-die racing to commit ``complete_task`` / ``fail_task``
            either wins (status filter excludes the row from our sweep
            — we silently skip it) or loses (its UPDATE matches zero
            rows because we advanced the version; the worker's
            error path surfaces a :class:`VersionConflictError`
            classified as "wrong status"). Exactly one writer wins;
            no duplicate state.

        Args:
            heartbeat_timeout_seconds: Age threshold in seconds. Workers
                with ``last_heartbeat < NOW() - this`` AND ``status =
                'online'`` are flipped to ``offline``. Must be a
                non-negative integer; ``0`` flips every still-online
                worker on every call (only useful in tests with
                controlled clocks).

        Returns:
            Number of *tasks* released (and corresponding RELEASE events
            written). ``0`` is the normal "no offline workers, or no
            offline workers had in-flight tasks" outcome — not an
            error. The number of *workers* flipped to offline is *not*
            returned because the sweep loop's primary signal is
            tasks-released-per-tick (that's what the dashboard and
            metrics will surface); callers that need worker counts can
            ``SELECT COUNT(*) FROM workers WHERE status = 'offline'``.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    _RELEASE_OFFLINE_WORKERS_SQL,
                    heartbeat_timeout_seconds,
                    Transition.RELEASE.value,
                    "worker_offline",
                )
                released = len(rows)
                if released:
                    logger.info(
                        "release_offline_workers: heartbeat_timeout=%ds released %d task(s): %s",
                        heartbeat_timeout_seconds,
                        released,
                        [row["task_id"] for row in rows],
                    )
                else:
                    logger.debug(
                        "release_offline_workers: heartbeat_timeout=%ds — no offline workers with in-flight tasks",
                        heartbeat_timeout_seconds,
                    )
                return released

    async def register_worker(
        self,
        worker_id: WorkerId,
        hostname: str,
        token_hash: str,
    ) -> None:
        """Insert a new row in ``workers`` for a freshly-registered worker (PRD FR-1.1).

        Called by the ``POST /workers/register`` handler in
        :mod:`whilly.adapters.transport.server` (TASK-021b) after the
        bootstrap-token check passes and the server has minted a fresh
        ``worker_id`` plus a per-worker bearer token. Only the *hash* of
        the token reaches Postgres (PRD NFR-3) — the plaintext is returned
        once in the HTTP response and then discarded by the server.

        ``worker_id`` is generated server-side from
        :func:`secrets.token_urlsafe`: 64+ bits of entropy is enough that
        a unique-violation collision is effectively impossible across any
        plausible cluster size, so we don't bother with ``ON CONFLICT``
        retry logic — a (vanishingly rare) collision surfaces as
        :class:`asyncpg.UniqueViolationError`, which the handler can
        translate to a 500 and log for follow-up rather than silently
        overwriting another worker's row.

        Why no transaction wrapper?
            One INSERT, no audit event, no follow-up read. The heartbeat /
            registered_at columns default to ``NOW()`` via the schema's
            server-side defaults, so the row is fully populated atomically
            by the single statement. A transaction here would only inflate
            the wire round-trips without adding any invariant.

        Why no return value?
            The caller already knows the ``worker_id`` (it generated it)
            and the plaintext token (it generated that too). Returning
            anything else would just leak the asyncpg row object into a
            layer that doesn't need it.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(_INSERT_WORKER_SQL, worker_id, hostname, token_hash)
        logger.info("register_worker: registered worker %s on %s", worker_id, hostname)

    async def get_worker_id_by_token_hash(self, token_hash: str) -> WorkerId | None:
        """Resolve a presented bearer's hash to the owning ``worker_id`` (TASK-101).

        Used by the per-worker bearer FastAPI dependency in
        :mod:`whilly.adapters.transport.auth` on every steady-state RPC.
        ``token_hash`` is the SHA-256 hex digest of the plaintext bearer
        the worker presented (computed by the dep — not by this method,
        so the repository surface remains hashing-scheme-agnostic).

        Returns the matching ``worker_id`` on hit, or ``None`` when no
        row carries that hash. The latter covers three operationally
        distinct cases — the auth layer treats them all as 401 because
        from the wire's perspective they are indistinguishable:

        * the token was never issued (random / forged bearer);
        * the worker was revoked via ``UPDATE workers SET token_hash =
          NULL`` (NULL ≠ anything under SQL three-valued logic, so the
          equality predicate fails);
        * the worker row was deleted (e.g. test cleanup, FK cascade).

        Returning ``None`` rather than raising keeps the dep simple —
        the 401 path is built once in :func:`_bearer_401` and the
        repository never raises for "didn't find anything".

        Why no audit event?
            Auth lookups fire on every RPC; logging each one would
            bloat ``events`` by orders of magnitude with no extra
            audit value (the per-RPC handlers already log success /
            failure with ``worker_id`` context). The 401 path is a
            transport concern, not a domain transition.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(_LOOKUP_WORKER_BY_TOKEN_HASH_SQL, token_hash)
        # ``WorkerId`` is a ``str`` type alias (``whilly.core.models``), so
        # the asyncpg-returned ``str`` is already the right shape — no
        # constructor wrapping needed.
        return row

    async def update_heartbeat(self, worker_id: WorkerId) -> bool:
        """Stamp ``workers.last_heartbeat = NOW()`` for ``worker_id``.

        Called periodically by the worker (TASK-019b1 for local,
        TASK-022b2 for remote) under :class:`asyncio.TaskGroup` so the
        control plane can distinguish "still working" from "crashed and
        the visibility-timeout sweep should reclaim its row" (PRD FR-1.4).

        Returns ``True`` when a row matched and was updated, ``False``
        when ``worker_id`` is not registered. The boolean lets the
        heartbeat loop log a warning and keep ticking without coupling
        the worker code to repository exception types — a missing worker
        row (admin revoked, ON DELETE SET NULL after a cascade) is
        recoverable, not fatal.

        No transaction wrapper, no audit event. Heartbeats fire every
        ~30s; logging each one would bloat ``events`` by orders of
        magnitude with no extra audit value beyond the timestamp on
        ``workers``. Concurrency-wise the UPDATE is a single-row
        last-writer-wins on a non-primary-key column — safe under
        contention without locking.

        asyncpg returns the SQL command tag (``"UPDATE 1"`` /
        ``"UPDATE 0"``) from ``Connection.execute``; we parse the row
        count from that rather than running a follow-up SELECT.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(_UPDATE_HEARTBEAT_SQL, worker_id)
        # ``result`` is the asyncpg command tag, e.g. "UPDATE 1". Defensive
        # parse: split on whitespace and take the trailing integer. A
        # malformed tag (would indicate a driver-level bug, not user
        # input) falls through to 0 → returns False.
        try:
            updated = int(result.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            updated = 0
        if not updated:
            logger.warning(
                "update_heartbeat: worker %s not registered (no row updated)",
                worker_id,
            )
            return False
        logger.debug("update_heartbeat: worker %s last_heartbeat refreshed", worker_id)
        return True

    async def reset_plan(self, plan_id: PlanId, *, keep_tasks: bool) -> int:
        """Reset every task in ``plan_id`` to ``PENDING`` (or wipe the plan).

        Implements the data-side half of the ``whilly plan reset`` CLI
        (TASK-103). Two modes, selected by the ``keep_tasks`` flag:

        * ``keep_tasks=True`` — soft reset. Deletes every event row whose
          task lives under ``plan_id``, then flips every such task back
          to ``status='PENDING'`` (clearing ``claimed_by`` /
          ``claimed_at``, incrementing ``version``), and writes one
          ``RESET`` event per task carrying ``payload = {"reason":
          "manual_reset", "mode": "keep_tasks", "version": <new>}``.
          Returns the number of tasks reset.

        * ``keep_tasks=False`` — hard reset. ``DELETE FROM plans WHERE
          id = $1``; the FK chain's ``ON DELETE CASCADE`` wipes the
          tasks rows and (transitively) the events rows in one
          statement. Returns the pre-delete count of tasks (so the CLI
          can show the same "N tasks affected" summary as in soft mode).

        Atomicity
        ---------
        Both modes run inside a single ``async with conn.transaction()``
        so an operator can't observe a partial reset. In soft mode the
        three statements (events DELETE, tasks UPDATE, audit INSERT
        per task) commit as a unit. In hard mode the COUNT and DELETE
        commit as a unit — the COUNT runs against the same MVCC
        snapshot the DELETE will operate on, so the returned value
        matches the rows that were actually affected.

        Why not a single combined CTE?
            Per-statement diagnostics matter on the operator-facing
            reset path: a CHECK constraint violation (e.g. someone
            extending ``events.event_type`` with a CHECK) should surface
            with the offending verb in the error message rather than
            being lost behind a CTE wrapper. The transaction is the
            atomicity contract; the per-statement granularity is
            developer ergonomics.

        Why no audit row in hard mode?
            ``DELETE FROM plans`` cascades to ``events`` — anything we
            insert before the delete would be wiped before commit, and
            anything inserted after the delete has no ``task_id`` to
            reference (FK NOT NULL). Operators relying on a durable
            audit trail should use ``keep_tasks=True`` or wait for the
            file-based audit mirror (TASK-106).

        Args
        ----
        plan_id:
            The plan to reset. A non-existent ``plan_id`` returns ``0``
            without error in either mode (idempotent — repeated resets
            on the same id are safe).
        keep_tasks:
            ``True`` for soft reset, ``False`` for hard reset.

        Returns
        -------
        int
            Number of tasks affected (reset to PENDING in soft mode, or
            scheduled for deletion in hard mode). Returns ``0`` when the
            plan is unknown — callers can use this to differentiate
            "plan exists, was empty" from "plan doesn't exist" by
            checking the plan row separately if they care.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if keep_tasks:
                    # Wipe events first so the post-reset audit rows are
                    # the only RESET-tagged rows left for this plan.
                    await conn.execute(_RESET_DELETE_EVENTS_SQL, plan_id)
                    rows = await conn.fetch(_RESET_UPDATE_TASKS_SQL, plan_id)
                    for row in rows:
                        payload = json.dumps(
                            {
                                "reason": "manual_reset",
                                "mode": "keep_tasks",
                                "version": row["version"],
                            }
                        )
                        await conn.execute(
                            _INSERT_EVENT_SQL,
                            row["id"],
                            "RESET",
                            payload,
                        )
                    logger.info(
                        "reset_plan(keep_tasks=True): plan=%s reset %d task(s)",
                        plan_id,
                        len(rows),
                    )
                    return len(rows)

                # Hard mode: count first (so we can report what we
                # nuked), then DELETE the plan row. CASCADE handles
                # tasks + events in the same statement.
                count_row = await conn.fetchrow(_RESET_COUNT_TASKS_SQL, plan_id)
                count = int(count_row["c"]) if count_row else 0
                await conn.execute(_RESET_DELETE_PLAN_SQL, plan_id)
                logger.info(
                    "reset_plan(keep_tasks=False): plan=%s deleted (%d task(s) cascaded)",
                    plan_id,
                    count,
                )
                return count

    async def _raise_version_conflict(
        self,
        conn: asyncpg.Connection,
        task_id: TaskId,
        expected_version: int,
    ) -> None:
        """Build and raise a :class:`VersionConflictError` for a 0-row UPDATE.

        Runs inside the same transaction as the failed UPDATE so the SELECT
        sees the same MVCC snapshot the UPDATE evaluated against — this
        guarantees the version / status we report is the value the UPDATE
        actually disagreed with, not a freshly-shifted value from a third
        writer that committed in between.

        Marked ``-> None`` (rather than ``NoReturn``) only because
        :pep:`484`'s ``NoReturn`` and async functions interact awkwardly in
        mypy < 1.6; ``raise`` from this method always exits via the
        exception path, never returns.
        """
        probe = await conn.fetchrow(_PROBE_TASK_SQL, task_id)
        actual_version: int | None
        actual_status: TaskStatus | None
        if probe is None:
            actual_version = None
            actual_status = None
        else:
            actual_version = probe["version"]
            actual_status = TaskStatus(probe["status"])
        logger.warning(
            "VersionConflict: task=%s expected_version=%d actual_version=%s actual_status=%s",
            task_id,
            expected_version,
            actual_version,
            actual_status.value if actual_status else None,
        )
        raise VersionConflictError(
            task_id=task_id,
            expected_version=expected_version,
            actual_version=actual_version,
            actual_status=actual_status,
        )
