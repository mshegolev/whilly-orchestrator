"""Integration tests for ``TaskRepository.skip_task`` (TASK-104c).

Mirrors VAL-GATES-009 through VAL-GATES-015 in
``validation-contract.md``: the SQL primitive's status transitions,
audit-log shape, idempotency, terminal-state rejection, and
optimistic-locking contract — all exercised against a real
testcontainers Postgres so the JSONB payload encoding and the FK
chain across ``tasks`` / ``events`` are part of the assertion
surface.

Why integration vs. unit?
-------------------------
``skip_task`` is a multi-statement transaction (probe → UPDATE →
event INSERT). A unit test would have to mock every asyncpg surface
involved and would lose all of the SQL invariants that actually
matter (CASCADE on ``events.task_id``, ``payload->>'reason'`` shape,
``status`` CHECK constraint). The price is requiring Docker — but
that's already paid by every other integration test in this suite.

We use the per-test ``db_pool`` and ``task_repo`` fixtures from
``tests/conftest.py`` (truncate at setup, fresh asyncpg pool with
the migrated schema).
"""

from __future__ import annotations

import json

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import TaskRepository, VersionConflictError
from whilly.core.models import TaskStatus

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-SKIP-001"
TASK_ID = "T-SKIP-001"


# ─── seeding helpers ─────────────────────────────────────────────────────


async def _seed_plan_and_task(
    pool: asyncpg.Pool,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    plan_id: str = PLAN_ID,
    task_id: str = TASK_ID,
    version: int = 0,
    claimed_by: str | None = None,
) -> None:
    """Insert one plan + one task in the requested status.

    For statuses that require a worker (CLAIMED / IN_PROGRESS the row
    keeps ``claimed_by`` populated only when explicitly requested),
    the caller may pass ``claimed_by`` — and a workers row is
    auto-inserted to satisfy the FK.
    """
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", plan_id, "skip-test")
        if claimed_by is not None:
            await conn.execute(
                "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
                claimed_by,
                "host",
                "hash",
            )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version,
                claimed_by, claimed_at
            )
            VALUES ($1, $2, $3, '[]'::jsonb, '[]'::jsonb,
                    'medium', $4, '[]'::jsonb, '[]'::jsonb, '', $5,
                    $6::text,
                    CASE WHEN $6::text IS NULL THEN NULL ELSE NOW() END)
            """,
            task_id,
            plan_id,
            status.value,
            f"task {task_id}",
            version,
            claimed_by,
        )


# ─── VAL-GATES-009: PENDING → SKIPPED + audit row ───────────────────────


async def test_skip_task_pending_to_skipped(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """skip_task on PENDING: status → SKIPPED, version+1, SKIP event row."""
    await _seed_plan_and_task(db_pool, status=TaskStatus.PENDING, version=0)

    skipped = await task_repo.skip_task(
        TASK_ID,
        version=0,
        reason="decision_gate_failed",
        detail={"missing": ["acceptance_criteria"]},
    )

    assert skipped.status == TaskStatus.SKIPPED
    assert skipped.version == 1

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", TASK_ID)
        assert status == "SKIPPED"

        rows = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"]) if isinstance(rows[0]["payload"], str) else rows[0]["payload"]
    assert payload["reason"] == "decision_gate_failed"
    assert payload["missing"] == ["acceptance_criteria"]
    assert payload["version"] == 1


# ─── VAL-GATES-010: CLAIMED → SKIPPED ───────────────────────────────────


async def test_skip_task_claimed_to_skipped(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """skip_task on CLAIMED: state machine permits the transition."""
    await _seed_plan_and_task(
        db_pool,
        status=TaskStatus.CLAIMED,
        version=1,
        claimed_by="w-1",
    )

    skipped = await task_repo.skip_task(TASK_ID, version=1, reason="manual_skip")
    assert skipped.status == TaskStatus.SKIPPED
    assert skipped.version == 2

    async with db_pool.acquire() as conn:
        # Claim ownership cleared on transition.
        row = await conn.fetchrow(
            "SELECT status, claimed_by, claimed_at FROM tasks WHERE id = $1",
            TASK_ID,
        )
        assert row["status"] == "SKIPPED"
        assert row["claimed_by"] is None
        assert row["claimed_at"] is None

        skip_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert skip_count == 1


# ─── VAL-GATES-011: IN_PROGRESS → SKIPPED ───────────────────────────────


async def test_skip_task_in_progress_to_skipped(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """skip_task on IN_PROGRESS: same observable side effects."""
    await _seed_plan_and_task(
        db_pool,
        status=TaskStatus.IN_PROGRESS,
        version=2,
        claimed_by="w-1",
    )

    skipped = await task_repo.skip_task(TASK_ID, version=2, reason="manual_skip")
    assert skipped.status == TaskStatus.SKIPPED

    async with db_pool.acquire() as conn:
        skip_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert skip_count == 1


# ─── VAL-GATES-012: idempotent on already-SKIPPED ───────────────────────


async def test_skip_task_idempotent_on_already_skipped(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """Re-invoking on SKIPPED returns the existing task without writing again."""
    await _seed_plan_and_task(db_pool, status=TaskStatus.PENDING, version=0)

    first = await task_repo.skip_task(TASK_ID, version=0, reason="decision_gate_failed")
    assert first.status == TaskStatus.SKIPPED
    first_version = first.version

    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert before == 1

    # Second call — passes a stale version on purpose to prove the
    # idempotency path doesn't depend on the optimistic-lock filter.
    second = await task_repo.skip_task(TASK_ID, version=0, reason="decision_gate_failed")
    assert second.status == TaskStatus.SKIPPED
    assert second.version == first_version, "idempotent path must not bump version"

    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert after == 1, "second skip must not write a duplicate audit row"


# ─── VAL-GATES-013: rejects from terminal DONE ──────────────────────────


async def test_skip_task_rejects_from_done(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """skip_task on DONE: raises VersionConflictError, no side effect."""
    await _seed_plan_and_task(db_pool, status=TaskStatus.DONE, version=3)

    with pytest.raises(VersionConflictError):
        await task_repo.skip_task(TASK_ID, version=3, reason="decision_gate_failed")

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", TASK_ID)
        skip_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert status == "DONE"
    assert skip_count == 0


# ─── VAL-GATES-014: rejects from terminal FAILED ────────────────────────


async def test_skip_task_rejects_from_failed(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """skip_task on FAILED: same documented invalid-transition exception."""
    await _seed_plan_and_task(db_pool, status=TaskStatus.FAILED, version=2)

    with pytest.raises(VersionConflictError):
        await task_repo.skip_task(TASK_ID, version=2, reason="decision_gate_failed")

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", TASK_ID)
        skip_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert status == "FAILED"
    assert skip_count == 0


# ─── VAL-GATES-015: respects optimistic-locking version ─────────────────


async def test_skip_task_respects_version_conflict(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """A stale ``version`` argument raises VersionConflictError without side effect."""
    await _seed_plan_and_task(db_pool, status=TaskStatus.PENDING, version=5)

    with pytest.raises(VersionConflictError):
        await task_repo.skip_task(TASK_ID, version=2, reason="decision_gate_failed")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            TASK_ID,
        )
        skip_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert row["status"] == "PENDING"
    assert row["version"] == 5
    assert skip_count == 0


# ─── Detail merging: reserved keys cannot be overridden ──────────────────


async def test_skip_task_detail_does_not_override_reason_or_version(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """The canonical ``reason`` / ``version`` fields beat ``detail`` overrides."""
    await _seed_plan_and_task(db_pool, status=TaskStatus.PENDING, version=0)

    await task_repo.skip_task(
        TASK_ID,
        version=0,
        reason="decision_gate_failed",
        detail={"reason": "tampered", "version": 999, "missing": ["test_steps"]},
    )

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
            TASK_ID,
        )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"]) if isinstance(rows[0]["payload"], str) else rows[0]["payload"]
    assert payload["reason"] == "decision_gate_failed"
    # version is the post-update value (1), not the tampered 999.
    assert payload["version"] == 1
    assert payload["missing"] == ["test_steps"]
