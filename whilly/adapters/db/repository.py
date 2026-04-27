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
from typing import Any

import asyncpg

from whilly.core.models import PlanId, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.core.state_machine import Transition

__all__ = ["TaskRepository", "VersionConflictError"]

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
    SELECT id
    FROM tasks
    WHERE plan_id = $1
      AND status = 'PENDING'
    ORDER BY {_PRIORITY_RANK_SQL}, id
    FOR UPDATE SKIP LOCKED
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


# Optimistic-locking COMPLETE: only flips ``IN_PROGRESS`` → ``DONE`` when the
# caller's expected version matches the row's current version. The status
# filter mirrors the state-machine rule from
# :func:`whilly.core.state_machine.apply_transition` so a buggy or stale
# caller cannot drag a DONE / FAILED / SKIPPED task back through the
# lifecycle. RETURNING ships the post-update row so the caller doesn't need a
# separate SELECT on the happy path.
_COMPLETE_SQL: str = """
UPDATE tasks
SET status = 'DONE',
    version = tasks.version + 1,
    updated_at = NOW()
WHERE id = $1
  AND version = $2
  AND status = 'IN_PROGRESS'
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

    async def complete_task(self, task_id: TaskId, version: int) -> Task:
        """Atomically transition ``task_id`` from ``IN_PROGRESS`` → ``DONE``.

        Optimistic-locking contract: the UPDATE only fires when the row's
        current ``version`` matches the ``version`` argument *and* the row's
        status is ``IN_PROGRESS``. On success the row's version is
        incremented by 1, status is set to ``DONE``, and a ``COMPLETE`` event
        row is appended in the same transaction.

        On failure raises :class:`VersionConflictError`. The error carries
        the *expected* and *actual* (version, status) tuple so the caller
        can distinguish:

        * **lost update** — another writer (visibility-timeout sweep, second
          worker after a re-claim) advanced the version first;
        * **wrong status** — the task is already DONE / FAILED / SKIPPED
          (idempotent retry detection);
        * **task missing** — the row vanished (FK cascade in tests).

        See :class:`VersionConflictError` for the field semantics.

        Why no SELECT-then-UPDATE here?
            We deliberately skip ``FOR UPDATE`` and the read-modify-write
            ceremony: filtering by ``version`` in the UPDATE is the lock-free
            equivalent and avoids holding a row lock while we write the
            audit event. The follow-up ``_PROBE_TASK_SQL`` only runs on the
            cold "0 rows updated" path, so the happy path is one round-trip.
        """
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
                logger.info(
                    "complete_task: task=%s version=%d → DONE",
                    row["id"],
                    row["version"],
                )
                return _row_to_task(row)

    async def fail_task(self, task_id: TaskId, version: int, reason: str) -> Task:
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
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_FAIL_SQL, task_id, version)
                if row is None:
                    await self._raise_version_conflict(conn, task_id, version)

                payload = json.dumps({"version": row["version"], "reason": reason})
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    row["id"],
                    Transition.FAIL.value,
                    payload,
                )
                logger.info(
                    "fail_task: task=%s version=%d reason=%r → FAILED",
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
