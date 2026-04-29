"""Unit tests for :mod:`whilly.worker.main` (TASK-019b1, PRD FR-1.6, NFR-1).

What we cover
-------------
- Module-level constants (heartbeat interval default).
- :func:`run_worker` ticks the heartbeat at least once during a short
  idle run, and the worker loop ``WorkerStats`` come back unchanged
  from the inner :func:`run_local_worker`.
- A heartbeat ``update_heartbeat`` failure is logged but does not kill
  the worker — best-effort liveness contract.
- When the inner loop exits (max_iterations reached), the heartbeat
  also exits; the TaskGroup unwinds without hanging. Asserted via
  :func:`asyncio.wait_for` with a tight timeout.

How we isolate from real I/O
----------------------------
A small :class:`FakeRepo` duck-types the subset of
:class:`whilly.adapters.db.repository.TaskRepository` the heartbeat
composer touches: ``update_heartbeat`` (TASK-019b1) plus the four
methods :func:`run_local_worker` already needs. ``claim_task`` returns
``None`` so the inner loop sits in its idle path while the heartbeat
ticks; that gives the heartbeat enough wall time to fire without
exercising the agent runner (covered exhaustively in
``tests/unit/test_local_worker.py``).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Task, TaskId, WorkerId
from whilly.worker.main import (
    DEFAULT_HEARTBEAT_INTERVAL,
    run_worker,
)


WORKER_ID: WorkerId = "worker-test-hb"
PLAN_ID = "plan-test-hb"


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="Heartbeat Test Plan")


class FakeRepo:
    """In-memory stand-in for :class:`TaskRepository`.

    Only ``update_heartbeat`` and ``claim_task`` are exercised in these
    tests; the remaining methods raise loudly so an accidental call
    (which would mean the inner loop took the work path instead of the
    idle path) shows up as a clear test failure rather than silent
    drift.
    """

    def __init__(self) -> None:
        self.heartbeat_calls: list[WorkerId] = []
        self.heartbeat_results: list[bool | Exception] = []
        # ``claim_task`` returning ``None`` keeps the inner loop on the
        # idle path so the heartbeat side has room to tick.
        self.claim_calls: list[tuple[WorkerId, str]] = []

    async def update_heartbeat(self, worker_id: WorkerId) -> bool:
        self.heartbeat_calls.append(worker_id)
        if not self.heartbeat_results:
            return True
        result = self.heartbeat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def claim_task(self, worker_id: WorkerId, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        return None

    # The remaining repository surface — none of these should fire
    # because every test runs the inner loop on the idle path. If one
    # does fire, the AssertionError makes the failure point obvious
    # (better than a generic "AttributeError: no attribute X" later).
    async def start_task(self, task_id: TaskId, version: int) -> Task:  # pragma: no cover
        raise AssertionError("start_task should not be called in heartbeat tests")

    async def complete_task(
        self,
        task_id: TaskId,
        version: int,
        cost_usd: object = None,
    ) -> Task:  # pragma: no cover
        raise AssertionError("complete_task should not be called in heartbeat tests")

    async def fail_task(self, task_id: TaskId, version: int, reason: str) -> Task:  # pragma: no cover
        raise AssertionError("fail_task should not be called in heartbeat tests")


def test_default_heartbeat_interval_is_30_seconds() -> None:
    """30s is the PRD-mandated cadence (FR-1.6); pin it so a future
    tweak shows up in the diff and forces a docs review."""
    assert DEFAULT_HEARTBEAT_INTERVAL == 30.0


async def test_run_worker_ticks_heartbeat_during_idle_loop() -> None:
    """A short idle run should produce at least one heartbeat tick.

    With ``heartbeat_interval`` at 1ms and ``max_iterations=5`` the
    inner loop spends a handful of millis polling (each iteration is
    one ``claim_task`` returning ``None`` plus an ``asyncio.sleep(0)``).
    The heartbeat task starts immediately on entry, so the very first
    tick fires before the inner loop even reaches its first poll —
    asserting ``>= 1`` is robust against scheduling jitter.
    """
    repo = FakeRepo()
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run when no tasks claimed")

    stats = await run_worker(
        repo,  # type: ignore[arg-type]  # FakeRepo duck-types TaskRepository
        runner,
        plan,
        WORKER_ID,
        idle_wait=0,
        heartbeat_interval=0.001,
        max_iterations=5,
    )

    # Inner loop accounting unchanged by heartbeat composition.
    assert stats.iterations == 5
    assert stats.idle_polls == 5
    assert stats.completed == 0
    assert stats.failed == 0

    # At least one heartbeat tick fired and addressed the right worker.
    assert len(repo.heartbeat_calls) >= 1, "heartbeat did not tick during the idle run"
    assert all(wid == WORKER_ID for wid in repo.heartbeat_calls)


async def test_run_worker_logs_heartbeat_failure_and_keeps_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient ``update_heartbeat`` failure must not kill the worker.

    The first heartbeat raises; the loop logs and ticks again. Inner-
    loop accounting must still complete normally. A WARNING log entry
    is the operator-visible breadcrumb.
    """
    repo = FakeRepo()
    repo.heartbeat_results = [RuntimeError("simulated network blip")]
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run when no tasks claimed")

    with caplog.at_level(logging.WARNING, logger="whilly.worker.main"):
        stats = await run_worker(
            repo,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            idle_wait=0,
            heartbeat_interval=0.001,
            max_iterations=3,
        )

    # Inner loop ran to completion despite the heartbeat error.
    assert stats.iterations == 3
    assert stats.idle_polls == 3

    # The simulated failure was logged at WARNING, naming the worker.
    failure_logs = [
        rec
        for rec in caplog.records
        if rec.name == "whilly.worker.main" and "heartbeat update failed" in rec.getMessage()
    ]
    assert failure_logs, "expected a WARNING log for the simulated heartbeat failure"
    assert WORKER_ID in failure_logs[0].getMessage()


async def test_run_worker_heartbeat_terminates_when_main_loop_exits() -> None:
    """When the inner loop returns, the heartbeat must wind down.

    With ``heartbeat_interval=10s`` the heartbeat would normally sit on
    its ``wait_for(stop, 10)`` for the full interval — but the inner
    loop's ``finally: stop.set()`` wakes it up immediately. If the
    stop-event signalling were broken, this test would hang on the
    TaskGroup exit until the wait_for timed out at 10s; the
    :func:`asyncio.wait_for` with a 5s outer timeout converts that
    hang into a clean test failure.
    """
    repo = FakeRepo()
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run when no tasks claimed")

    stats = await asyncio.wait_for(
        run_worker(
            repo,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            idle_wait=0,
            heartbeat_interval=10.0,
            max_iterations=1,
        ),
        timeout=5.0,
    )

    assert stats.iterations == 1


async def test_run_worker_propagates_inner_loop_exceptions() -> None:
    """A crash inside the inner loop should propagate, not get swallowed.

    The worker contract says ``run_worker`` returns the inner stats *or*
    raises whatever the inner loop raised — so a buggy adapter / runner
    fails loudly instead of silently disappearing into the TaskGroup.
    The ``finally: stop.set()`` in ``_worker_then_stop`` means the
    heartbeat still exits cleanly so the TaskGroup itself unwinds.
    """
    plan = _make_plan()

    class CrashingRepo(FakeRepo):
        async def claim_task(self, worker_id: WorkerId, plan_id: str) -> Task | None:
            raise RuntimeError("simulated repo crash")

    repo = CrashingRepo()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run when claim fails")

    # TaskGroup wraps the underlying RuntimeError in an ExceptionGroup
    # in Python 3.11+. We accept either so tests stay robust to the
    # asyncio internals.
    with pytest.raises((RuntimeError, BaseExceptionGroup)) as exc_info:
        await asyncio.wait_for(
            run_worker(
                repo,  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                idle_wait=0,
                heartbeat_interval=10.0,
                max_iterations=1,
            ),
            timeout=5.0,
        )

    # Walk the exception tree — the original message must surface
    # somewhere, whether bare or under an ExceptionGroup wrapper.
    def _flatten(exc: BaseException) -> list[BaseException]:
        if isinstance(exc, BaseExceptionGroup):
            return [leaf for sub in exc.exceptions for leaf in _flatten(sub)]
        return [exc]

    leaves = _flatten(exc_info.value)
    assert any(isinstance(e, RuntimeError) and "simulated repo crash" in str(e) for e in leaves), (
        f"original RuntimeError did not survive the TaskGroup wrapper: {leaves!r}"
    )
