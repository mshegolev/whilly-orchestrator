"""Integration test for worker SIGTERM/SIGINT shutdown (TASK-019b2, PRD FR-1.6, NFR-1).

Acceptance criteria covered
---------------------------
- ``SIGTERM → текущая задача RELEASE'ится (status=PENDING, claimed_by=NULL,
  version+=1)``: verified by sending real ``SIGTERM`` to the test process
  while the worker is mid-runner, then asserting the row in Postgres
  is back to ``PENDING`` with ``claimed_by IS NULL`` and a ``RELEASE``
  event with ``payload.reason = "shutdown"``.
- ``SIGINT обрабатывается аналогично SIGTERM``: same flow, second test
  function, second signal — pinning that both signals route through the
  same handler instead of one diverging silently.
- ``Корректный shutdown без зависших asyncio tasks (TaskGroup.cancel_scope)``:
  ``run_worker`` must return cleanly within a tight :func:`asyncio.wait_for`
  bound. A hung TaskGroup would surface as ``TimeoutError`` rather than a
  silent multi-second pause; the wall-clock budget converts that bug into
  an obvious failure.
- ``Тест на graceful shutdown зелёный (SIGTERM kill → задача снова PENDING
  в БД)``: this file. Skips when Docker isn't available because the
  testcontainers Postgres bootstrap can't run.

Why a real signal rather than ``stop.set()``?
    The unit-level test in ``tests/unit/test_worker_shutdown.py`` already
    drives the inner loop's stop-aware path with a directly-set event;
    that's faster and exhaustive about the loop-side state transitions.
    This file's contract is different: it pins the *signal* → *handler*
    → ``stop.set()`` chain end-to-end. A regression that broke
    :func:`whilly.worker.main._install_signal_handlers` (e.g. forgot to
    register on the loop, used the wrong signal name) would still pass
    the unit test but fail this one. We pay the testcontainers boot cost
    once per session via the shared ``postgres_dsn`` fixture.

Why ``os.kill(os.getpid(), ...)`` rather than spawning a subprocess?
    A subprocess would isolate the worker from pytest cleanly but adds
    significant plumbing (asyncpg can't share a pool across forks, the
    test would need IPC to know when the child claimed a task, etc.).
    Sending the signal in-process works because
    :func:`run_worker` installs its handler via
    :meth:`asyncio.AbstractEventLoop.add_signal_handler`, which
    *overrides* the default disposition for the lifetime of the loop —
    SIGTERM never propagates to "kill the process" while the handler is
    active. The handler restoration on TaskGroup exit means the test
    process stays signal-safe for the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Task, TaskStatus
from whilly.core.state_machine import Transition
from whilly.worker.local import SHUTDOWN_RELEASE_REASON
from whilly.worker.main import run_worker

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-SHUTDOWN-1"
WORKER_ID = "worker-shutdown-1"
TASK_ID = "T-SHUTDOWN-1"


async def _seed_one_task_plan(pool: asyncpg.Pool, plan_id: str, worker_id: str, task_id: str) -> None:
    """Insert a worker, a plan, and a single PENDING task ready to claim.

    Mirrors the seeding shape used in :mod:`tests.integration.test_worker_heartbeat`
    so a regression in the shared schema surfaces consistently across the
    integration suite.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            f"host-{worker_id}",
            f"sha256:{worker_id}",
        )
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            plan_id,
            f"plan {plan_id}",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'high', $3, '[]'::jsonb, '[]'::jsonb, 'FR-1.6', 0)
            """,
            task_id,
            plan_id,
            f"shutdown test task {task_id}",
        )


async def _wait_for_status(pool: asyncpg.Pool, task_id: str, target: TaskStatus, *, timeout: float = 5.0) -> None:
    """Poll the ``tasks`` row until its status equals ``target``.

    Used to synchronise the test with the worker's progress without
    plumbing internal events out of the loop. The poll interval is
    deliberately small (5ms) — we want to send the signal as soon as
    the runner has actually started, not seconds later, so the
    visibility-timeout sweep can't beat us to the release path.

    ``timeout`` is wall-clock; if exceeded we ``pytest.fail`` with the
    last-observed status so a hung worker is diagnosable from the test
    output alone.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last_status: str | None = None
    while asyncio.get_event_loop().time() < deadline:
        async with pool.acquire() as conn:
            last_status = await conn.fetchval(
                "SELECT status FROM tasks WHERE id = $1",
                task_id,
            )
        if last_status == target.value:
            return
        await asyncio.sleep(0.005)
    pytest.fail(f"task {task_id} did not reach {target.value} within {timeout}s; last status was {last_status!r}")


async def _drive_shutdown_via_signal(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    sig: signal.Signals,
) -> None:
    """End-to-end shutdown drill for one signal.

    1. Seed plan + worker + task.
    2. Spawn ``run_worker`` as a background asyncio task. The runner
       coroutine sleeps long enough that the signal will arrive while
       the task is in ``IN_PROGRESS``.
    3. Wait for the row to reach ``IN_PROGRESS`` so we know the handler
       is installed (it's installed at the top of ``run_worker``, and
       the runner is only entered after handler installation).
    4. Send the signal via ``os.kill(os.getpid(), sig)``.
    5. Await the worker task — it must finish promptly (asserted via
       :func:`asyncio.wait_for`).
    6. Verify the post-conditions: status PENDING, claimed_by NULL,
       version > original, RELEASE event with reason="shutdown".
    """
    await _seed_one_task_plan(db_pool, PLAN_ID, WORKER_ID, TASK_ID)

    runner_started = asyncio.Event()

    async def slow_runner(task: Task, prompt: str) -> AgentResult:
        """Simulate a long-running agent that gets cancelled by shutdown.

        The ``runner_started`` event is a fast-path signal for the test
        — once it fires we know the worker is past ``start_task`` and
        truly in the runner phase, so the visibility-timeout sweep can't
        race us. The long sleep ensures the signal arrives while we're
        here, not after we've returned a fake AgentResult.
        """
        runner_started.set()
        # Sleep way longer than the test budget so the only way out is
        # cancellation by the shutdown path.
        await asyncio.sleep(60.0)
        # Defensive: if the cancel path breaks we'd fall through here.
        # Returning a "complete" result would corrupt the test by
        # transitioning the task to DONE; raise instead so the failure
        # is loud and attributable.
        raise AssertionError("runner reached its return statement; cancellation path broken")

    plan = Plan(id=PLAN_ID, name="Shutdown Test Plan")

    worker_task = asyncio.create_task(
        run_worker(
            task_repo,
            slow_runner,
            plan,
            WORKER_ID,
            idle_wait=0.01,
            heartbeat_interval=10.0,  # don't pollute the test with heartbeat noise
            install_signal_handlers=True,
        )
    )

    try:
        # Wait for the runner to actually be running. ``runner_started``
        # firing also confirms ``run_worker`` installed its signal
        # handler (the handler is installed before the TaskGroup enters,
        # which is before ``run_local_worker`` reaches the runner call).
        await asyncio.wait_for(runner_started.wait(), timeout=5.0)
        await _wait_for_status(db_pool, TASK_ID, TaskStatus.IN_PROGRESS, timeout=2.0)

        # Send the signal to ourselves. The asyncio loop's signal
        # handler intercepts it — no default-disposition kill of the
        # test process.
        os.kill(os.getpid(), sig)

        # Worker must finish quickly. A hang here would mean either
        # the signal handler didn't fire (regression in
        # ``_install_signal_handlers``) or the runner-cancellation
        # didn't unwind (regression in ``_await_runner_or_stop``).
        stats = await asyncio.wait_for(worker_task, timeout=10.0)
    except BaseException:
        # If any assertion above fails, make sure the background task
        # doesn't outlive the test — pytest would otherwise log a
        # "Task was destroyed but it is pending" warning that obscures
        # the real failure.
        if not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
        raise

    # Stats sanity: exactly one shutdown release, no completions, no
    # failures (the runner was cancelled mid-call).
    assert stats.released_on_shutdown == 1, f"expected exactly one shutdown release, got stats={stats!r}"
    assert stats.completed == 0
    assert stats.failed == 0

    # Database post-conditions: row is PENDING again with claim cleared.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, claimed_by, claimed_at, version FROM tasks WHERE id = $1",
            TASK_ID,
        )
    assert row is not None, f"task {TASK_ID} disappeared from the database"
    assert row["status"] == TaskStatus.PENDING.value, f"status not reset: {row['status']!r}"
    assert row["claimed_by"] is None, f"claimed_by not cleared: {row['claimed_by']!r}"
    assert row["claimed_at"] is None, f"claimed_at not cleared: {row['claimed_at']!r}"
    # Initial version 0 → claim 1 → start 2 → release 3.
    assert row["version"] >= 3, f"version did not advance through release: {row['version']}"

    # Audit log: a RELEASE event with reason="shutdown" must exist for
    # this task. The visibility-timeout sweep would write
    # reason="visibility_timeout" instead, so the reason field is the
    # discriminator that proves the signal path fired (not a fallback
    # release from the sweep).
    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            """
            SELECT event_type, payload
            FROM events
            WHERE task_id = $1
            ORDER BY created_at, id
            """,
            TASK_ID,
        )
    release_events = [e for e in events if e["event_type"] == Transition.RELEASE.value]
    assert release_events, (
        f"no RELEASE event recorded for task {TASK_ID}; events were: {[e['event_type'] for e in events]!r}"
    )
    payload = json.loads(release_events[0]["payload"])
    assert payload.get("reason") == SHUTDOWN_RELEASE_REASON, (
        f"RELEASE event reason was {payload.get('reason')!r}, expected "
        f"{SHUTDOWN_RELEASE_REASON!r} — the visibility-timeout sweep beat "
        f"the signal handler? Payload was {payload!r}"
    )


async def test_sigterm_releases_in_flight_task_back_to_pending(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """SIGTERM mid-runner → task re-PENDINGed and a RELEASE/shutdown event is logged.

    This is the canonical TASK-019b2 acceptance test. A failure here means
    the worker would lose work on every Kubernetes / systemd-driven
    rolling restart — peers couldn't pick up the bounced task until the
    visibility-timeout sweep eventually noticed (default 15 minutes, PRD
    FR-1.4). The whole point of TASK-019b2 is to short-circuit that
    timeout for cooperative shutdowns.
    """
    await _drive_shutdown_via_signal(db_pool, task_repo, signal.SIGTERM)


async def test_sigint_releases_in_flight_task_same_as_sigterm(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """SIGINT (e.g. interactive Ctrl-C) follows the same shutdown path.

    Pinning both signals through the same exit path matters because a
    common refactor mistake is to install only one (typically SIGTERM),
    leaving Ctrl-C to default-kill the worker mid-task — exactly the
    silent work-loss scenario the AC forbids.
    """
    await _drive_shutdown_via_signal(db_pool, task_repo, signal.SIGINT)
