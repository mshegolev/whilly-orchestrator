"""Integration test for worker heartbeat (TASK-019b1, PRD FR-1.6, NFR-1).

Acceptance criteria covered
---------------------------
- Heartbeat запускается параллельной asyncio задачей с интервалом ~30с
  (here: tight intervals so the test runs in milliseconds, but the
  composition root is identical to production).
- ``repository.update_heartbeat(worker_id)`` is invoked: verified by
  observing ``workers.last_heartbeat`` advance against a deliberately-
  stale baseline.
- При остановке main цикла heartbeat корректно завершается (TaskGroup
  cancel): asserted by wrapping ``run_worker`` in
  :func:`asyncio.wait_for` with a tight timeout — a hung heartbeat
  would surface as a TimeoutError instead of a clean return.

Why two tests
-------------
:func:`test_heartbeat_advances_workers_last_heartbeat` proves the
heartbeat *side effect* (the SQL hits Postgres and the column moves).
:func:`test_heartbeat_loop_terminates_with_main_loop` proves the
*lifecycle* (the TaskGroup unwinds when the worker exits). Both are
needed: a heartbeat that ticks but never stops would still pass test
#1 yet hang every production worker; a heartbeat that exits cleanly
but never wrote SQL would pass test #2 yet leave every worker dark.

A direct repository smoke test is included so a regression in the
``UPDATE workers ...`` SQL surfaces with a clearly-attributable failure
rather than as a downstream side-effect noticed via
:func:`run_worker`.
"""

from __future__ import annotations

import asyncio

import asyncpg

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Task
from whilly.worker.main import run_worker

# Module-level marker — pytest skips the whole file in environments without
# Docker (CI runners that don't expose a daemon, contributors on machines
# without colima / Rancher / Docker Desktop). Mirrors the SC-1 file's
# pattern.
pytestmark = DOCKER_REQUIRED


async def _seed_worker_with_stale_heartbeat(pool: asyncpg.Pool, worker_id: str) -> None:
    """Insert a worker row and force ``last_heartbeat`` an hour into the past.

    The default ``last_heartbeat = NOW()`` would race against the test's
    "after" timestamp on machines with low-resolution clocks (Postgres
    truncates to microseconds; the default and the heartbeat update can
    land in the same microsecond). Backdating by an hour makes the
    inequality unambiguous.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            f"host-{worker_id}",
            f"sha256:{worker_id}",
        )
        await conn.execute(
            "UPDATE workers SET last_heartbeat = NOW() - interval '1 hour' WHERE worker_id = $1",
            worker_id,
        )


async def _seed_empty_plan(pool: asyncpg.Pool, plan_id: str) -> None:
    """Insert a plan row with zero tasks so the worker idles cleanly."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            plan_id,
            f"plan {plan_id}",
        )


async def test_update_heartbeat_advances_column_and_returns_true(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """Direct smoke test for ``TaskRepository.update_heartbeat``.

    Isolates the SQL change from the composition logic in
    :func:`whilly.worker.main.run_worker` so a regression in the
    ``UPDATE workers ...`` statement (typo, schema drift) is reported
    against this test rather than the higher-level integration test.
    """
    worker_id = "w-hb-direct"
    await _seed_worker_with_stale_heartbeat(db_pool, worker_id)

    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )

    updated = await task_repo.update_heartbeat(worker_id)
    assert updated is True

    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )

    assert after > before, f"update_heartbeat did not advance last_heartbeat: before={before} after={after}"


async def test_update_heartbeat_returns_false_for_unknown_worker(
    task_repo: TaskRepository,
) -> None:
    """A missing ``worker_id`` is recoverable; the repo returns ``False``.

    Defensive: the heartbeat loop in :mod:`whilly.worker.main` swallows
    real exceptions but reads the bool to decide whether to log a
    warning at higher severity. A future change that turned this into
    a raise would break the worker — pin the contract here.
    """
    updated = await task_repo.update_heartbeat("worker-does-not-exist")
    assert updated is False


async def test_heartbeat_advances_workers_last_heartbeat(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """End-to-end: ``run_worker`` ticks heartbeat while the inner loop idles.

    With an empty plan the inner loop sits in its idle path
    (``claim_task`` → ``None`` → sleep → repeat). The heartbeat task
    runs concurrently and stamps ``workers.last_heartbeat`` at every
    tick. After ``max_iterations`` the inner loop exits, the stop event
    fires, the heartbeat unwinds, and the TaskGroup completes — at
    which point ``last_heartbeat`` must be later than the deliberately-
    backdated baseline.
    """
    worker_id = "w-hb-1"
    plan_id = "PLAN-HB-1"

    await _seed_worker_with_stale_heartbeat(db_pool, worker_id)
    await _seed_empty_plan(db_pool, plan_id)

    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )

    plan = Plan(id=plan_id, name="HB Plan")

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    stats = await asyncio.wait_for(
        run_worker(
            task_repo,
            runner,
            plan,
            worker_id,
            idle_wait=0.001,
            heartbeat_interval=0.005,
            max_iterations=10,
        ),
        timeout=10.0,
    )

    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )

    assert after > before, f"heartbeat did not advance last_heartbeat: before={before} after={after}"
    # Inner-loop accounting unchanged by the heartbeat composition.
    assert stats.iterations == 10
    assert stats.idle_polls == 10
    assert stats.completed == 0
    assert stats.failed == 0


async def test_heartbeat_loop_terminates_with_main_loop(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """A long heartbeat interval must not delay TaskGroup shutdown.

    With ``heartbeat_interval=30s`` the heartbeat would normally sit on
    its ``wait_for(stop, 30)`` for the full interval. The
    ``finally: stop.set()`` in :func:`run_worker`'s inner closure
    wakes it up immediately when ``max_iterations`` is reached. The
    outer 5-second :func:`asyncio.wait_for` converts a regression into
    a clean test failure instead of a 30-second hang.
    """
    worker_id = "w-hb-2"
    plan_id = "PLAN-HB-2"

    await _seed_worker_with_stale_heartbeat(db_pool, worker_id)
    await _seed_empty_plan(db_pool, plan_id)

    plan = Plan(id=plan_id, name="HB Plan 2")

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    stats = await asyncio.wait_for(
        run_worker(
            task_repo,
            runner,
            plan,
            worker_id,
            idle_wait=0,
            heartbeat_interval=30.0,
            max_iterations=1,
        ),
        timeout=5.0,
    )

    assert stats.iterations == 1
