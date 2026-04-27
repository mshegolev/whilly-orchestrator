"""Postgres-backed task repository for Whilly v4.0 (PRD FR-1.3, FR-2.1, FR-2.4).

This module owns the SQL that mutates the ``tasks`` table and writes audit
rows to ``events``. It is the single I/O-side counterpart to the pure
state-machine in :mod:`whilly.core.state_machine`: callers operating against
Postgres go through :class:`TaskRepository` instead of issuing SQL directly,
so the at-least-once / atomicity invariants live in one place.

Scope of TASK-009b
------------------
This file currently implements only :meth:`TaskRepository.claim_task` —
atomic ``PENDING`` → ``CLAIMED`` transition via
``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` plus a CLAIM event row, all in
a single transaction. ``complete_task`` / ``fail_task`` (TASK-009c) and
``release_stale_tasks`` (TASK-009d) extend the same class and re-use the
helpers defined here.

Why ``FOR UPDATE SKIP LOCKED``?
    The standard Postgres queue idiom (since 9.5). Two concurrent claimers
    each acquire row-level locks on different candidate rows and proceed in
    parallel — no serialisation through a global mutex, no duplicate claims.
    Rows that another transaction has already locked are silently skipped,
    so a worker either takes a free task or returns ``None`` immediately
    instead of blocking. This is what SC-1 (100 concurrent claims, no
    duplicates / no losses) ultimately rests on; TASK-011 will exercise it
    end-to-end with testcontainers.

Why a CTE + outer UPDATE?
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

from whilly.core.models import PlanId, Priority, Task, TaskStatus, WorkerId
from whilly.core.state_machine import Transition

__all__ = ["TaskRepository"]

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
