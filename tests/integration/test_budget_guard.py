"""Integration tests for the plan-budget guard (TASK-102, PRD FR-2.4).

Pins the runtime half of TASK-102 — the data-layer migration is
covered by :mod:`tests.integration.test_alembic_005`; this module
exercises the *behaviour* the migration enables:

* VAL-BUDGET-010 / 011 — operator-supplied ``--budget`` round-trips
  through ``plans.budget_usd``; omitted ``--budget`` stores ``NULL``.
* VAL-BUDGET-012 — ``plans.spent_usd`` is initialised to ``0`` for a
  freshly-created plan.
* VAL-BUDGET-020 — ``budget_usd IS NULL`` (unlimited) keeps the claim
  path open even when ``spent_usd`` is large.
* VAL-BUDGET-021 — ``spent_usd < budget_usd`` permits claims.
* VAL-BUDGET-022 — ``spent_usd == budget_usd`` blocks the next claim
  (strict-``<`` boundary).
* VAL-BUDGET-023 — ``spent_usd > budget_usd`` blocks the next claim.
* VAL-BUDGET-030 — ``complete_task(cost_usd=...)`` accumulates the
  cost into ``plans.spent_usd`` atomically.
* VAL-BUDGET-031 — Decimal precision survives the round-trip
  (NUMERIC(10, 4)).
* VAL-BUDGET-032 — ``cost_usd=0`` / ``None`` is the no-op spend path
  (no plan UPDATE, no sentinel).
* VAL-BUDGET-033 — 100 increments of ``0.0123`` accumulate to exactly
  ``1.2300`` (NUMERIC arithmetic, no float drift).
* VAL-BUDGET-040 — exactly one ``plan.budget_exceeded`` sentinel is
  emitted when ``spent_usd`` first crosses ``budget_usd``.
* VAL-BUDGET-041 — sentinel is plan-level (``task_id IS NULL``,
  ``plan_id`` populated, payload includes ``budget_usd`` /
  ``spent_usd`` / ``crossing_task_id``).
* VAL-BUDGET-042 — plans with ``budget_usd IS NULL`` never emit a
  sentinel.
* VAL-BUDGET-043 — once crossed, subsequent completes do not emit
  additional sentinels.
* VAL-BUDGET-060 — complete_task rejects negative ``cost_usd``.
* VAL-BUDGET-071 — strict monotonic non-decrease of ``spent_usd``
  (sequence of completes never drops the column).

Companion :mod:`tests.integration.test_budget_concurrent` covers the
asyncio-gather contention contracts (VAL-BUDGET-050 / 051 / 052 /
072).

All tests use the session-scoped ``postgres_dsn`` testcontainer +
per-test ``db_pool`` truncation idiom from :mod:`tests.conftest`.
Direct INSERTs into ``plans`` / ``tasks`` / ``workers`` mirror the
seeding pattern in :mod:`tests.integration.test_concurrent_claims`
(repository deliberately does not expose plan-creation; that lives in
the CLI).
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.db.repository import (
    BUDGET_EXCEEDED_EVENT_TYPE,
    BUDGET_EXCEEDED_REASON,
    BUDGET_EXCEEDED_THRESHOLD_PCT,
    VersionConflictError,
)
from whilly.core.models import PlanId, TaskId

pytestmark = DOCKER_REQUIRED


# --------------------------------------------------------------------------- #
# Seeding helpers (mirror tests/integration/test_concurrent_claims.py)
# --------------------------------------------------------------------------- #


async def _seed_plan(
    pool: asyncpg.Pool,
    plan_id: PlanId,
    *,
    name: str | None = None,
    budget_usd: Decimal | None = None,
    spent_usd: Decimal | None = None,
) -> None:
    """Insert one plan with optional ``budget_usd`` / ``spent_usd`` overrides.

    ``budget_usd=None`` exercises the unlimited path (VAL-BUDGET-020 /
    042); ``spent_usd=None`` defers to the column default (``0``).
    """
    async with pool.acquire() as conn:
        if budget_usd is None and spent_usd is None:
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2)",
                plan_id,
                name or f"plan-{plan_id}",
            )
        elif spent_usd is None:
            await conn.execute(
                "INSERT INTO plans (id, name, budget_usd) VALUES ($1, $2, $3)",
                plan_id,
                name or f"plan-{plan_id}",
                budget_usd,
            )
        else:
            await conn.execute(
                "INSERT INTO plans (id, name, budget_usd, spent_usd) VALUES ($1, $2, $3, $4)",
                plan_id,
                name or f"plan-{plan_id}",
                budget_usd,
                spent_usd,
            )


async def _seed_task(
    pool: asyncpg.Pool,
    task_id: TaskId,
    plan_id: PlanId,
    *,
    priority: str = "medium",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', $3)",
            task_id,
            plan_id,
            priority,
        )


async def _seed_worker(pool: asyncpg.Pool, worker_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            f"host-{worker_id}",
            f"sha256:{worker_id}",
        )


async def _fetch_plan(pool: asyncpg.Pool, plan_id: PlanId) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, budget_usd, spent_usd FROM plans WHERE id = $1",
            plan_id,
        )
    assert row is not None, f"plan {plan_id!r} not found"
    return row


async def _fetch_sentinel_events(pool: asyncpg.Pool, plan_id: PlanId) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            (
                "SELECT id, task_id, plan_id, event_type, payload, created_at "
                "FROM events "
                "WHERE plan_id = $1 AND event_type = $2 "
                "ORDER BY id"
            ),
            plan_id,
            BUDGET_EXCEEDED_EVENT_TYPE,
        )


async def _claim_start_complete(
    repo: TaskRepository,
    plan_id: PlanId,
    worker_id: str,
    cost_usd: Decimal | float | None,
) -> TaskId:
    """Claim → start → complete cycle; returns the completed task id.

    Raises ``AssertionError`` if claim returns ``None`` (the test
    setup guarantees a PENDING task exists, so a None return is a
    real regression of the budget-filter — propagate as an
    AssertionError so the test author sees the failure as
    "claim returned None" rather than a downstream null-deref).
    """
    claimed = await repo.claim_task(worker_id, plan_id)
    assert claimed is not None, f"claim returned None for plan={plan_id!r} (budget filter regression?)"
    started = await repo.start_task(claimed.id, claimed.version)
    await repo.complete_task(started.id, started.version, cost_usd=cost_usd)
    return claimed.id


# --------------------------------------------------------------------------- #
# VAL-BUDGET-010 / 011 / 012 — plan budget columns round-trip
# --------------------------------------------------------------------------- #


async def test_plan_create_with_budget_round_trips(db_pool: asyncpg.Pool) -> None:
    """A plan inserted with ``budget_usd`` keeps that exact value (VAL-BUDGET-010).

    Initial ``spent_usd`` is the column default ``0`` (VAL-BUDGET-012).
    """
    await _seed_plan(db_pool, "plan-budget-1", budget_usd=Decimal("10.0000"))
    row = await _fetch_plan(db_pool, "plan-budget-1")
    assert row["budget_usd"] == Decimal("10.0000")
    assert row["spent_usd"] == Decimal("0")


async def test_plan_create_without_budget_stores_null_unlimited(db_pool: asyncpg.Pool) -> None:
    """A plan inserted with no ``budget_usd`` stores SQL NULL (VAL-BUDGET-011)."""
    await _seed_plan(db_pool, "plan-budget-null")
    row = await _fetch_plan(db_pool, "plan-budget-null")
    assert row["budget_usd"] is None
    assert row["spent_usd"] == Decimal("0")


# --------------------------------------------------------------------------- #
# VAL-BUDGET-020 — NULL budget never blocks claim
# --------------------------------------------------------------------------- #


async def test_null_budget_keeps_claim_path_open_even_with_high_spent(
    db_pool: asyncpg.Pool, task_repo: TaskRepository
) -> None:
    """``budget_usd IS NULL`` plus a large ``spent_usd`` still permits claims (VAL-BUDGET-020 / 042)."""
    # Seed an unlimited plan that already accumulated $9999 of spend
    # (no budget guard ever; the column is just bookkeeping).
    await _seed_plan(
        db_pool,
        "plan-unlimited",
        budget_usd=None,
        spent_usd=Decimal("9999.9999"),
    )
    await _seed_task(db_pool, "T-unl-1", "plan-unlimited")
    await _seed_worker(db_pool, "w-unl")

    claimed = await task_repo.claim_task("w-unl", "plan-unlimited")
    assert claimed is not None
    assert claimed.id == "T-unl-1"


# --------------------------------------------------------------------------- #
# VAL-BUDGET-021 / 022 / 023 — strict-< boundary
# --------------------------------------------------------------------------- #


async def test_spent_below_budget_permits_claim(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``spent_usd < budget_usd`` permits the next claim (VAL-BUDGET-021)."""
    await _seed_plan(
        db_pool,
        "plan-under",
        budget_usd=Decimal("1.0000"),
        spent_usd=Decimal("0.9999"),
    )
    await _seed_task(db_pool, "T-under", "plan-under")
    await _seed_worker(db_pool, "w-under")

    claimed = await task_repo.claim_task("w-under", "plan-under")
    assert claimed is not None
    assert claimed.id == "T-under"


async def test_spent_equal_budget_blocks_claim(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``spent_usd == budget_usd`` blocks the next claim — strict-``<`` (VAL-BUDGET-022).

    The boundary is exclusive: an exact-cents budget exhaustion blocks
    the *next* claim. The PENDING task is left untouched so a later
    operator-driven budget bump can let it run.
    """
    await _seed_plan(
        db_pool,
        "plan-equal",
        budget_usd=Decimal("5.0000"),
        spent_usd=Decimal("5.0000"),
    )
    await _seed_task(db_pool, "T-equal", "plan-equal")
    await _seed_worker(db_pool, "w-equal")

    claimed = await task_repo.claim_task("w-equal", "plan-equal")
    assert claimed is None

    # The PENDING task is untouched (visible to a later claim if the
    # operator bumps the budget).
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM tasks WHERE id = 'T-equal'")
    assert row is not None
    assert row["status"] == "PENDING"


async def test_spent_above_budget_blocks_claim(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``spent_usd > budget_usd`` blocks the next claim (VAL-BUDGET-023).

    Models the post-crossing steady state: a previous complete pushed
    spend past the cap; subsequent claims for the same plan are
    refused.
    """
    await _seed_plan(
        db_pool,
        "plan-over",
        budget_usd=Decimal("1.0000"),
        spent_usd=Decimal("1.5000"),
    )
    await _seed_task(db_pool, "T-over", "plan-over")
    await _seed_worker(db_pool, "w-over")

    claimed = await task_repo.claim_task("w-over", "plan-over")
    assert claimed is None


# --------------------------------------------------------------------------- #
# VAL-BUDGET-030 / 031 / 032 / 033 — complete_task spend accumulator
# --------------------------------------------------------------------------- #


async def test_complete_with_cost_increments_spent_usd(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``complete_task(cost_usd=X)`` adds X to ``plans.spent_usd`` atomically (VAL-BUDGET-030)."""
    await _seed_plan(db_pool, "plan-spend", budget_usd=Decimal("10.0000"))
    await _seed_task(db_pool, "T-spend", "plan-spend")
    await _seed_worker(db_pool, "w-spend")

    await _claim_start_complete(task_repo, "plan-spend", "w-spend", cost_usd=Decimal("0.4200"))

    plan_row = await _fetch_plan(db_pool, "plan-spend")
    assert plan_row["spent_usd"] == Decimal("0.4200")


async def test_complete_preserves_decimal_precision(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """NUMERIC(10, 4) round-trips a sub-cent ``cost_usd`` losslessly (VAL-BUDGET-031)."""
    await _seed_plan(db_pool, "plan-precision", budget_usd=Decimal("1.0000"))
    await _seed_task(db_pool, "T-precision", "plan-precision")
    await _seed_worker(db_pool, "w-precision")

    await _claim_start_complete(task_repo, "plan-precision", "w-precision", cost_usd=Decimal("0.1234"))

    plan_row = await _fetch_plan(db_pool, "plan-precision")
    assert plan_row["spent_usd"] == Decimal("0.1234")


async def test_complete_with_zero_cost_is_no_op_spend(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``cost_usd=0`` skips the plan UPDATE and emits no sentinel (VAL-BUDGET-032).

    A plan whose ``spent_usd`` is already at the cap stays at the cap;
    no sentinel is emitted because the spend never moved.
    """
    await _seed_plan(
        db_pool,
        "plan-zero",
        budget_usd=Decimal("1.0000"),
        spent_usd=Decimal("0.5000"),
    )
    await _seed_task(db_pool, "T-zero", "plan-zero")
    await _seed_worker(db_pool, "w-zero")

    await _claim_start_complete(task_repo, "plan-zero", "w-zero", cost_usd=Decimal("0"))

    plan_row = await _fetch_plan(db_pool, "plan-zero")
    assert plan_row["spent_usd"] == Decimal("0.5000")
    sentinels = await _fetch_sentinel_events(db_pool, "plan-zero")
    assert sentinels == []


async def test_complete_with_none_cost_is_no_op_spend(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``cost_usd=None`` is also the no-op spend path (VAL-BUDGET-032 alt)."""
    await _seed_plan(
        db_pool,
        "plan-none-cost",
        budget_usd=Decimal("1.0000"),
        spent_usd=Decimal("0.7000"),
    )
    await _seed_task(db_pool, "T-none", "plan-none-cost")
    await _seed_worker(db_pool, "w-none")

    await _claim_start_complete(task_repo, "plan-none-cost", "w-none", cost_usd=None)

    plan_row = await _fetch_plan(db_pool, "plan-none-cost")
    assert plan_row["spent_usd"] == Decimal("0.7000")


async def test_repeated_increments_accumulate_without_float_drift(
    db_pool: asyncpg.Pool, task_repo: TaskRepository
) -> None:
    """100 sequential completes of $0.0123 each accumulate to exactly $1.2300 (VAL-BUDGET-033).

    NUMERIC arithmetic gives exact decimals; float arithmetic would
    drift from $1.23 across 100 increments — pinning this here makes
    a regression to ``REAL`` / ``DOUBLE PRECISION`` immediately
    visible.
    """
    n = 100
    cost = Decimal("0.0123")
    expected_total = cost * n  # Decimal('1.2300') — exact

    # Budget high enough that we don't trip the gate mid-loop.
    await _seed_plan(db_pool, "plan-drift", budget_usd=Decimal("100.0000"))
    for i in range(n):
        await _seed_task(db_pool, f"T-drift-{i:03d}", "plan-drift")
    await _seed_worker(db_pool, "w-drift")

    for _ in range(n):
        await _claim_start_complete(task_repo, "plan-drift", "w-drift", cost_usd=cost)

    plan_row = await _fetch_plan(db_pool, "plan-drift")
    assert plan_row["spent_usd"] == expected_total


# --------------------------------------------------------------------------- #
# VAL-BUDGET-040 / 041 / 042 / 043 — sentinel emission
# --------------------------------------------------------------------------- #


async def test_crossing_budget_emits_sentinel_once(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """A complete that pushes ``spent_usd`` from ``< budget_usd`` to ``>= budget_usd`` emits exactly one sentinel (VAL-BUDGET-040)."""
    await _seed_plan(
        db_pool,
        "plan-cross",
        budget_usd=Decimal("1.0000"),
        spent_usd=Decimal("0.7000"),
    )
    await _seed_task(db_pool, "T-cross", "plan-cross")
    await _seed_worker(db_pool, "w-cross")

    completed_id = await _claim_start_complete(task_repo, "plan-cross", "w-cross", cost_usd=Decimal("0.4000"))

    plan_row = await _fetch_plan(db_pool, "plan-cross")
    assert plan_row["spent_usd"] == Decimal("1.1000")

    sentinels = await _fetch_sentinel_events(db_pool, "plan-cross")
    assert len(sentinels) == 1
    sentinel = sentinels[0]
    assert sentinel["task_id"] is None  # plan-level event
    assert sentinel["plan_id"] == "plan-cross"
    assert sentinel["event_type"] == BUDGET_EXCEEDED_EVENT_TYPE
    payload = json.loads(sentinel["payload"])
    assert payload["plan_id"] == "plan-cross"
    assert Decimal(payload["budget_usd"]) == Decimal("1.0000")
    assert Decimal(payload["spent_usd"]) == Decimal("1.1000")
    assert payload["crossing_task_id"] == completed_id
    # VAL-CROSS-013: crossing-condition pins.
    assert payload["reason"] == BUDGET_EXCEEDED_REASON == "budget_threshold"
    assert payload["threshold_pct"] == BUDGET_EXCEEDED_THRESHOLD_PCT == 100


async def test_sentinel_event_shape_matches_contract(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """The sentinel row has ``task_id IS NULL``, ``plan_id`` populated, and a structured payload (VAL-BUDGET-041)."""
    await _seed_plan(
        db_pool,
        "plan-shape",
        budget_usd=Decimal("0.5000"),
        spent_usd=Decimal("0"),
    )
    await _seed_task(db_pool, "T-shape", "plan-shape")
    await _seed_worker(db_pool, "w-shape")

    await _claim_start_complete(task_repo, "plan-shape", "w-shape", cost_usd=Decimal("0.5000"))

    sentinels = await _fetch_sentinel_events(db_pool, "plan-shape")
    assert len(sentinels) == 1
    sentinel = sentinels[0]
    assert sentinel["task_id"] is None
    assert sentinel["plan_id"] == "plan-shape"
    payload = json.loads(sentinel["payload"])
    assert set(payload.keys()) >= {
        "plan_id",
        "budget_usd",
        "spent_usd",
        "crossing_task_id",
        "reason",
        "threshold_pct",
    }


async def test_budget_exceeded_payload_includes_reason_and_threshold_pct(
    db_pool: asyncpg.Pool, task_repo: TaskRepository
) -> None:
    """The sentinel payload pins ``reason='budget_threshold'`` and ``threshold_pct=100`` (VAL-CROSS-013).

    Independent of the wider sentinel-shape test: this is the verbatim
    evidence query the user-testing validator runs —
    ``SELECT payload->>'reason', (payload->>'threshold_pct')::int FROM
    events WHERE plan_id=$1 AND event_type='plan.budget_exceeded'`` —
    must return ``('budget_threshold', 100)``.
    """
    await _seed_plan(
        db_pool,
        "plan-cross-013",
        budget_usd=Decimal("0.5000"),
        spent_usd=Decimal("0"),
    )
    await _seed_task(db_pool, "T-cross-013", "plan-cross-013")
    await _seed_worker(db_pool, "w-cross-013")

    await _claim_start_complete(task_repo, "plan-cross-013", "w-cross-013", cost_usd=Decimal("0.5000"))

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            (
                "SELECT payload->>'reason' AS reason, "
                "(payload->>'threshold_pct')::int AS threshold_pct "
                "FROM events "
                "WHERE plan_id = $1 AND event_type = $2"
            ),
            "plan-cross-013",
            BUDGET_EXCEEDED_EVENT_TYPE,
        )
    assert row is not None
    assert row["reason"] == "budget_threshold"
    assert row["threshold_pct"] == 100


async def test_unlimited_plan_never_emits_sentinel(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """A plan with ``budget_usd IS NULL`` never emits a sentinel, regardless of spend (VAL-BUDGET-042)."""
    await _seed_plan(db_pool, "plan-unl-evt", budget_usd=None)
    await _seed_task(db_pool, "T-unl-evt", "plan-unl-evt")
    await _seed_worker(db_pool, "w-unl-evt")

    await _claim_start_complete(task_repo, "plan-unl-evt", "w-unl-evt", cost_usd=Decimal("99.9999"))

    plan_row = await _fetch_plan(db_pool, "plan-unl-evt")
    assert plan_row["spent_usd"] == Decimal("99.9999")
    sentinels = await _fetch_sentinel_events(db_pool, "plan-unl-evt")
    assert sentinels == []


async def test_post_crossing_completes_do_not_emit_more_sentinels(
    db_pool: asyncpg.Pool, task_repo: TaskRepository
) -> None:
    """Once a plan has crossed, subsequent completes do not emit additional sentinels (VAL-BUDGET-043).

    This is the steady-state path: budget is already exceeded, claim
    is blocked for new work, but a previously-claimed in-flight task
    can still complete and bump ``spent_usd``. Only the *crossing*
    transition emits the sentinel; further bumps after the budget is
    already crossed do not.

    To exercise this without re-claiming through the budget guard, we
    seed two ``CLAIMED`` tasks directly and complete them in
    sequence: the first crosses, the second does not.
    """
    await _seed_plan(
        db_pool,
        "plan-post",
        budget_usd=Decimal("1.0000"),
        spent_usd=Decimal("0"),
    )
    await _seed_worker(db_pool, "w-post")
    # Two pre-CLAIMED tasks. We bypass the claim path because the
    # second claim would be refused by the budget gate (VAL-BUDGET-022)
    # — and what we're testing here is the *complete* path's sentinel
    # logic, not the claim path.
    async with db_pool.acquire() as conn:
        for tid in ("T-post-1", "T-post-2"):
            await conn.execute(
                (
                    "INSERT INTO tasks "
                    "(id, plan_id, status, claimed_by, claimed_at, priority, version) "
                    "VALUES ($1, 'plan-post', 'CLAIMED', 'w-post', NOW(), 'medium', 1)"
                ),
                tid,
            )

    # First complete crosses the budget (0 → 0.6 → still under).
    # Wait: we want the first to cross, not the second. Use 1.5 to cross.
    await task_repo.complete_task("T-post-1", 1, cost_usd=Decimal("1.5000"))
    await task_repo.complete_task("T-post-2", 1, cost_usd=Decimal("0.5000"))

    plan_row = await _fetch_plan(db_pool, "plan-post")
    assert plan_row["spent_usd"] == Decimal("2.0000")

    sentinels = await _fetch_sentinel_events(db_pool, "plan-post")
    assert len(sentinels) == 1, (
        f"Expected exactly one sentinel (only the crossing transition emits one); got {len(sentinels)}: "
        f"{[json.loads(r['payload']) for r in sentinels]}"
    )


# --------------------------------------------------------------------------- #
# VAL-BUDGET-060 — negative cost is rejected
# --------------------------------------------------------------------------- #


async def test_negative_cost_is_rejected(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``complete_task(cost_usd=-X)`` raises rather than corrupting ``spent_usd`` (VAL-BUDGET-060 / 072)."""
    await _seed_plan(db_pool, "plan-neg", budget_usd=Decimal("1.0000"))
    await _seed_task(db_pool, "T-neg", "plan-neg")
    await _seed_worker(db_pool, "w-neg")

    claimed = await task_repo.claim_task("w-neg", "plan-neg")
    assert claimed is not None
    started = await task_repo.start_task(claimed.id, claimed.version)

    with pytest.raises(ValueError, match=r"non-negative"):
        await task_repo.complete_task(started.id, started.version, cost_usd=Decimal("-0.0001"))

    # Task transition was *not* committed (the coercion runs before
    # any SQL), so ``spent_usd`` is still 0 and the task is still
    # IN_PROGRESS.
    plan_row = await _fetch_plan(db_pool, "plan-neg")
    assert plan_row["spent_usd"] == Decimal("0")
    async with db_pool.acquire() as conn:
        task_row = await conn.fetchrow("SELECT status FROM tasks WHERE id = 'T-neg'")
    assert task_row is not None
    assert task_row["status"] == "IN_PROGRESS"


# --------------------------------------------------------------------------- #
# VAL-BUDGET-071 — strict monotonic non-decrease across a sequence of completes
# --------------------------------------------------------------------------- #


async def test_spent_usd_strictly_monotonic_across_sequence(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """A sequence of completes with positive costs never sees ``spent_usd`` decrease (VAL-BUDGET-071)."""
    await _seed_plan(db_pool, "plan-mono", budget_usd=None)
    n = 5
    for i in range(n):
        await _seed_task(db_pool, f"T-mono-{i}", "plan-mono")
    await _seed_worker(db_pool, "w-mono")

    costs = [Decimal("0.1000"), Decimal("0.2500"), Decimal("0.0500"), Decimal("1.0000"), Decimal("0.0001")]
    observed: list[Decimal] = []
    for cost in costs:
        await _claim_start_complete(task_repo, "plan-mono", "w-mono", cost_usd=cost)
        plan_row = await _fetch_plan(db_pool, "plan-mono")
        observed.append(plan_row["spent_usd"])

    assert observed == sorted(observed), f"spent_usd decreased somewhere: {observed}"
    expected_running = []
    running = Decimal("0")
    for cost in costs:
        running += cost
        expected_running.append(running)
    assert observed == expected_running


# --------------------------------------------------------------------------- #
# Sanity: complete on a stale version still raises VersionConflictError
# (proves the budget logic doesn't break the optimistic-lock contract)
# --------------------------------------------------------------------------- #


async def test_complete_with_stale_version_raises_conflict(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """Optimistic-lock contract is preserved alongside the new budget logic."""
    await _seed_plan(db_pool, "plan-ver", budget_usd=None)
    await _seed_task(db_pool, "T-ver", "plan-ver")
    await _seed_worker(db_pool, "w-ver")

    claimed = await task_repo.claim_task("w-ver", "plan-ver")
    assert claimed is not None
    started = await task_repo.start_task(claimed.id, claimed.version)

    # Use a stale version (claimed.version is N-1 from the started.version).
    with pytest.raises(VersionConflictError):
        await task_repo.complete_task(started.id, claimed.version, cost_usd=Decimal("0.01"))
