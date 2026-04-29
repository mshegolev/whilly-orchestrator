"""Concurrent-completion contracts for the plan-budget guard (TASK-102).

This module pins the asyncio-gather contention contracts of
TASK-102's plan-spend accumulator, complementing the sequential
behaviour covered by :mod:`tests.integration.test_budget_guard`:

* VAL-BUDGET-050 — N concurrent completes against the same plan
  serialise on the ``plans.id`` row lock (``FOR UPDATE`` inside
  :data:`whilly.adapters.db.repository._INCREMENT_SPEND_SQL`); the
  final ``spent_usd`` equals the sum of all per-task costs.
* VAL-BUDGET-051 — exactly one ``plan.budget_exceeded`` sentinel is
  emitted across the whole burst, identifying the crossing task.
* VAL-BUDGET-052 — when the budget is large enough that no completer
  crosses, no sentinel is emitted (negative-control).
* VAL-BUDGET-072 — strict monotonic non-decrease of ``spent_usd``
  even under contention: the column never reads a value less than
  any previously-observed value, regardless of the completer order.

The contention design mirrors :mod:`tests.integration.test_concurrent_claims`:
seed N PENDING tasks → claim each into a distinct CLAIMED row →
``asyncio.gather`` the N ``complete_task`` calls. Claims are run
sequentially (the budget filter would block claims #2..N if we
gathered claims too) but the ``complete_task`` calls all race on the
plan's ``spent_usd`` row lock — the path SC-1 / VAL-BUDGET-050 calls
out as load-bearing.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.db.repository import BUDGET_EXCEEDED_EVENT_TYPE
from whilly.core.models import PlanId

pytestmark = DOCKER_REQUIRED


# Contention size. Big enough that the FOR UPDATE serialisation has
# real work to do; small enough to keep the test under a second on a
# laptop Postgres.
N_COMPLETERS: int = 16


async def _seed_plan(
    pool: asyncpg.Pool,
    plan_id: PlanId,
    *,
    budget_usd: Decimal | None,
) -> None:
    async with pool.acquire() as conn:
        if budget_usd is None:
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2)",
                plan_id,
                f"plan-{plan_id}",
            )
        else:
            await conn.execute(
                "INSERT INTO plans (id, name, budget_usd) VALUES ($1, $2, $3)",
                plan_id,
                f"plan-{plan_id}",
                budget_usd,
            )


async def _seed_pre_claimed_tasks(
    pool: asyncpg.Pool,
    plan_id: PlanId,
    n: int,
    worker_id: str,
) -> list[str]:
    """Seed ``n`` tasks already in ``CLAIMED`` state owned by ``worker_id``.

    We bypass the claim path because the budget filter would refuse
    later claims as ``spent_usd`` rises, defeating the purpose of the
    contention test: the contract under exercise is the *complete*
    path's plan-spend accumulator, not the claim gate.

    Tasks are inserted with ``version=1`` so the caller's
    ``complete_task(task_id, version=1)`` matches the optimistic-lock
    filter.
    """
    task_ids = [f"T-bcc-{i:03d}" for i in range(n)]
    async with pool.acquire() as conn:
        await conn.executemany(
            (
                "INSERT INTO tasks "
                "(id, plan_id, status, claimed_by, claimed_at, priority, version) "
                "VALUES ($1, $2, 'CLAIMED', $3, NOW(), 'medium', 1)"
            ),
            [(tid, plan_id, worker_id) for tid in task_ids],
        )
    return task_ids


async def _seed_worker(pool: asyncpg.Pool, worker_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            f"host-{worker_id}",
            f"sha256:{worker_id}",
        )


async def _fetch_plan_spent(pool: asyncpg.Pool, plan_id: PlanId) -> tuple[Decimal | None, Decimal]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT budget_usd, spent_usd FROM plans WHERE id = $1", plan_id)
    assert row is not None
    return row["budget_usd"], row["spent_usd"]


async def _fetch_sentinels(pool: asyncpg.Pool, plan_id: PlanId) -> list[asyncpg.Record]:
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


# --------------------------------------------------------------------------- #
# VAL-BUDGET-050 — N concurrent completes accumulate exact total
# --------------------------------------------------------------------------- #


async def test_concurrent_completes_accumulate_exact_total(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """``asyncio.gather`` of N completes lands ``spent_usd`` at the exact sum (VAL-BUDGET-050).

    The plan budget is intentionally far above the total so no
    completer crosses — this test isolates the *accumulation*
    invariant from the *crossing-detection* invariant (covered by the
    next test).
    """
    cost = Decimal("0.0500")
    expected_total = cost * N_COMPLETERS  # Decimal('0.8000') for N=16

    await _seed_plan(db_pool, "plan-bcc-acc", budget_usd=Decimal("100.0000"))
    await _seed_worker(db_pool, "w-bcc")
    task_ids = await _seed_pre_claimed_tasks(db_pool, "plan-bcc-acc", N_COMPLETERS, "w-bcc")

    # Gather N concurrent completes. Each task is at version=1 so the
    # optimistic-lock filter matches exactly once per task; the plan's
    # spent_usd row is the contention point.
    await asyncio.gather(*(task_repo.complete_task(tid, 1, cost_usd=cost) for tid in task_ids))

    budget, spent = await _fetch_plan_spent(db_pool, "plan-bcc-acc")
    assert budget == Decimal("100.0000")
    assert spent == expected_total


# --------------------------------------------------------------------------- #
# VAL-BUDGET-051 — exactly one sentinel under contention
# --------------------------------------------------------------------------- #


async def test_concurrent_crossing_emits_exactly_one_sentinel(db_pool: asyncpg.Pool, task_repo: TaskRepository) -> None:
    """Among N concurrent completes that collectively cross the budget, exactly one sentinel is emitted (VAL-BUDGET-051).

    Budget = 0.4000, N=16 completers each at 0.0500 → cumulative
    crosses at completer #8 (spent = 0.4000). Whoever observes the
    pre-update value strictly below 0.4000 *and* lands the post-update
    value at-or-above 0.4000 is the crossing completer; FOR UPDATE on
    the plan row guarantees exactly one such observer.
    """
    cost = Decimal("0.0500")
    budget = Decimal("0.4000")
    # Sanity: total spend should land above the budget so a crossing
    # is forced regardless of interleaving order.
    expected_total = cost * N_COMPLETERS
    assert expected_total > budget, "test setup: total must exceed budget to force a crossing"

    await _seed_plan(db_pool, "plan-bcc-cross", budget_usd=budget)
    await _seed_worker(db_pool, "w-bcc-cross")
    task_ids = await _seed_pre_claimed_tasks(db_pool, "plan-bcc-cross", N_COMPLETERS, "w-bcc-cross")

    await asyncio.gather(*(task_repo.complete_task(tid, 1, cost_usd=cost) for tid in task_ids))

    _, spent = await _fetch_plan_spent(db_pool, "plan-bcc-cross")
    assert spent == expected_total

    sentinels = await _fetch_sentinels(db_pool, "plan-bcc-cross")
    assert len(sentinels) == 1, (
        f"expected exactly one sentinel under contention; got {len(sentinels)}: "
        f"{[json.loads(r['payload']) for r in sentinels]}"
    )

    # Crossing task id must be one of the seeded task ids — proves the
    # sentinel payload identifies the actual crossing completer rather
    # than carrying a placeholder.
    payload = json.loads(sentinels[0]["payload"])
    assert payload["plan_id"] == "plan-bcc-cross"
    assert payload["crossing_task_id"] in set(task_ids)
    assert Decimal(payload["budget_usd"]) == budget


# --------------------------------------------------------------------------- #
# VAL-BUDGET-052 — no crossing means no sentinel even under contention
# --------------------------------------------------------------------------- #


async def test_concurrent_completes_under_budget_emit_no_sentinel(
    db_pool: asyncpg.Pool, task_repo: TaskRepository
) -> None:
    """When N concurrent completes collectively stay under the budget, no sentinel is emitted (VAL-BUDGET-052).

    Negative-control for the crossing-detection logic: the row lock
    must not produce a spurious crossing flag from the contention
    pattern itself.
    """
    cost = Decimal("0.0100")
    budget = Decimal("100.0000")  # very generous; total is 0.16 ≪ 100
    await _seed_plan(db_pool, "plan-bcc-nocross", budget_usd=budget)
    await _seed_worker(db_pool, "w-bcc-nocross")
    task_ids = await _seed_pre_claimed_tasks(db_pool, "plan-bcc-nocross", N_COMPLETERS, "w-bcc-nocross")

    await asyncio.gather(*(task_repo.complete_task(tid, 1, cost_usd=cost) for tid in task_ids))

    _, spent = await _fetch_plan_spent(db_pool, "plan-bcc-nocross")
    assert spent == cost * N_COMPLETERS
    sentinels = await _fetch_sentinels(db_pool, "plan-bcc-nocross")
    assert sentinels == []


# --------------------------------------------------------------------------- #
# VAL-BUDGET-072 — strict-monotonic spent_usd under contention
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("trial", range(3))
async def test_spent_usd_strictly_monotonic_under_contention(
    db_pool: asyncpg.Pool, task_repo: TaskRepository, trial: int
) -> None:
    """``spent_usd`` is strictly monotonic non-decreasing under N concurrent completes (VAL-BUDGET-072).

    We seed N tasks, fire N concurrent completes, and concurrently
    poll ``plans.spent_usd`` from a separate task. Every observed
    value must be >= the previous observed value, and the final
    value must equal the cumulative total.

    Re-runs three times via parametrize to amplify a flake into a
    near-deterministic failure (a single accidentally-non-monotonic
    read is unlikely to repeat, but a real regression in the FOR
    UPDATE serialisation would hit on every iteration).
    """
    plan_id: PlanId = f"plan-bcc-mono-{trial}"
    cost = Decimal("0.0250")
    expected_total = cost * N_COMPLETERS

    await _seed_plan(db_pool, plan_id, budget_usd=None)
    await _seed_worker(db_pool, f"w-bcc-mono-{trial}")
    task_ids = await _seed_pre_claimed_tasks(db_pool, plan_id, N_COMPLETERS, f"w-bcc-mono-{trial}")

    # Concurrent observer that polls the plan's spent_usd while the
    # gather is in flight. Must not cancel the completes — we exit on
    # an event the writer task fires when the gather is done.
    done = asyncio.Event()
    observed: list[Decimal] = []

    async def _poll() -> None:
        # Cap the poll loop so a deadlock in the writer doesn't hang
        # the test forever.
        for _ in range(200):
            if done.is_set():
                break
            async with db_pool.acquire() as conn:
                value = await conn.fetchval("SELECT spent_usd FROM plans WHERE id = $1", plan_id)
            observed.append(value)
            await asyncio.sleep(0.001)

    async def _completers() -> None:
        try:
            await asyncio.gather(*(task_repo.complete_task(tid, 1, cost_usd=cost) for tid in task_ids))
        finally:
            done.set()

    await asyncio.gather(_poll(), _completers())

    # Final value must equal the cumulative total.
    _, final_spent = await _fetch_plan_spent(db_pool, plan_id)
    assert final_spent == expected_total

    # And the observed values, in observation order, must be
    # non-decreasing — pin the strict-monotonic invariant.
    assert observed, "poller should have observed at least one value"
    last = Decimal(0)
    for value in observed:
        assert value >= last, f"spent_usd went backwards: previous={last} → observed={value}; full sequence={observed}"
        last = value
    assert observed[-1] <= expected_total
