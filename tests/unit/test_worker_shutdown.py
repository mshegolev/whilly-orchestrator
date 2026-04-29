"""Unit tests for the worker shutdown path (TASK-019b2, PRD FR-1.6, NFR-1).

What we cover
-------------
- The optional ``stop`` event short-circuits the loop at the iteration
  boundary (no claim, no idle sleep) so SIGTERM during a long
  ``idle_wait`` returns promptly.
- A ``stop`` set *during* the runner cancels the runner, calls
  :meth:`TaskRepository.release_task` with the canonical
  ``"shutdown"`` reason, and exits — :data:`WorkerStats.released_on_shutdown`
  becomes 1 and ``completed``/``failed`` remain 0.
- A :class:`VersionConflictError` from ``release_task`` (the
  visibility-timeout sweep beat us to it) is logged and the loop exits
  cleanly without crashing.
- :func:`whilly.worker.main.run_worker` forwards ``stop`` so an
  externally-set event drives the shutdown end-to-end without sending
  real signals — this keeps the unit suite Docker-free and signal-free.

Why no real signals here?
    Real ``os.kill`` calls collide with pytest's own SIGINT handler and
    would force every test to disable signal handling. The end-to-end
    signal → handler → stop chain is exercised once in
    :mod:`tests.integration.test_worker_signals`. This file pins the
    *internal* contract: given a stop event (whoever sets it), does
    the loop release and exit correctly?

How we isolate from real I/O
----------------------------
Same hand-rolled :class:`FakeRepo` shape as
:mod:`tests.unit.test_local_worker`, extended with a scriptable
``release_task`` so we can drive both happy-path and conflict scenarios
without :class:`unittest.mock.AsyncMock` clutter.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from whilly.adapters.db.repository import VersionConflictError
from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.worker.local import (
    SHUTDOWN_RELEASE_REASON,
    WorkerStats,
    run_local_worker,
)
from whilly.worker.main import run_worker


WORKER_ID: WorkerId = "worker-test-shutdown"
PLAN_ID = "plan-test-shutdown"
TASK_ID: TaskId = "T-shutdown-1"


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="Shutdown Test Plan")


def _make_task(version: int = 0, status: TaskStatus = TaskStatus.PENDING) -> Task:
    return Task(
        id=TASK_ID,
        status=status,
        priority=Priority.HIGH,
        description="shutdown-path task",
        version=version,
    )


class FakeRepo:
    """In-memory stand-in covering the methods the shutdown path needs.

    Mirrors the shape used in ``test_local_worker.py``'s ``FakeRepo`` —
    scripted return queues plus call records — but adds
    ``release_task`` and ``update_heartbeat`` so :func:`run_worker`
    composes against it without real Postgres.
    """

    def __init__(self) -> None:
        self.claim_results: list[Task | None] = []
        self.start_results: list[Task | VersionConflictError] = []
        self.complete_results: list[Task | VersionConflictError] = []
        self.fail_results: list[Task | VersionConflictError] = []
        self.release_results: list[Task | VersionConflictError] = []

        self.claim_calls: list[tuple[WorkerId, str]] = []
        self.start_calls: list[tuple[TaskId, int]] = []
        self.complete_calls: list[tuple[TaskId, int]] = []
        self.fail_calls: list[tuple[TaskId, int, str]] = []
        self.release_calls: list[tuple[TaskId, int, str]] = []
        self.heartbeat_calls: list[WorkerId] = []

    async def claim_task(self, worker_id: WorkerId, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        if not self.claim_results:
            return None
        return self.claim_results.pop(0)

    async def start_task(self, task_id: TaskId, version: int) -> Task:
        self.start_calls.append((task_id, version))
        result = self.start_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def complete_task(self, task_id: TaskId, version: int) -> Task:  # pragma: no cover
        self.complete_calls.append((task_id, version))
        result = self.complete_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def fail_task(self, task_id: TaskId, version: int, reason: str) -> Task:  # pragma: no cover
        self.fail_calls.append((task_id, version, reason))
        result = self.fail_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def release_task(self, task_id: TaskId, version: int, reason: str) -> Task:
        self.release_calls.append((task_id, version, reason))
        result = self.release_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def update_heartbeat(self, worker_id: WorkerId) -> bool:
        self.heartbeat_calls.append(worker_id)
        return True


# --------------------------------------------------------------------------- #
# stop-set-before-claim — fast iteration-boundary exit
# --------------------------------------------------------------------------- #


async def test_stop_set_before_first_iteration_exits_without_claim() -> None:
    """Pre-set ``stop`` should mean zero iterations and zero claim calls.

    Operationally: a SIGTERM that arrives just after the worker starts
    but before its first poll must not waste a database round-trip.
    """
    repo = FakeRepo()
    plan = _make_plan()
    stop = asyncio.Event()
    stop.set()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run when stop is preset")

    stats = await run_local_worker(
        repo,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        idle_wait=0,
        stop=stop,
    )

    assert stats == WorkerStats()  # all zeros
    assert repo.claim_calls == [], "claim_task should not be called when stop is preset"


async def test_stop_during_idle_wait_wakes_loop_promptly() -> None:
    """A long ``idle_wait`` must not delay shutdown — the sleep races ``stop``.

    With ``idle_wait`` = 30s and a 50ms shutdown trigger, a regression
    where the loop did a plain ``asyncio.sleep`` would mean this test
    runs for the full 30s. The outer ``wait_for(..., timeout=2.0)``
    converts that into a fast failure instead of a slow one.
    """
    repo = FakeRepo()
    plan = _make_plan()
    stop = asyncio.Event()
    # claim returns None forever — keeps the loop in idle path.
    repo.claim_results = [None] * 100

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run on the idle path")

    async def trigger_stop() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(trigger_stop())
        run_task = tg.create_task(
            asyncio.wait_for(
                run_local_worker(
                    repo,  # type: ignore[arg-type]
                    runner,
                    plan,
                    WORKER_ID,
                    idle_wait=30.0,  # would dominate runtime if not racing stop
                    stop=stop,
                ),
                timeout=2.0,
            )
        )

    stats = run_task.result()
    assert stats.idle_polls >= 1, "loop should have made at least one idle poll"
    assert stats.released_on_shutdown == 0, "no in-flight task → no shutdown release"


# --------------------------------------------------------------------------- #
# stop-set-mid-runner — release path
# --------------------------------------------------------------------------- #


async def test_stop_during_runner_releases_task_with_shutdown_reason() -> None:
    """The canonical TASK-019b2 path: stop fires while the runner is awaiting.

    Asserts:
    * ``release_task`` was called once with the in-progress task's
      version and the canonical ``"shutdown"`` reason.
    * ``WorkerStats.released_on_shutdown`` is 1, and
      ``completed`` / ``failed`` are both 0 (no terminal transition
      happened — the task lives to be re-claimed).
    * The loop exited promptly (asserted by the outer ``wait_for``).
    """
    repo = FakeRepo()
    plan = _make_plan()
    stop = asyncio.Event()

    claimed_task = _make_task(version=1, status=TaskStatus.CLAIMED)
    started_task = _make_task(version=2, status=TaskStatus.IN_PROGRESS)
    released_task = _make_task(version=3, status=TaskStatus.PENDING)

    repo.claim_results = [claimed_task]
    repo.start_results = [started_task]
    repo.release_results = [released_task]

    runner_entered = asyncio.Event()

    async def runner(task: Task, prompt: str) -> AgentResult:
        runner_entered.set()
        # Sleep way past the test budget — the only sane exit is
        # cancellation by the shutdown path.
        await asyncio.sleep(60.0)
        raise AssertionError("runner should have been cancelled")

    async def trigger_stop() -> None:
        await runner_entered.wait()
        # Tiny grace so the inner ``asyncio.wait`` is actually awaiting
        # both the runner and the stop — without this the wait could
        # observe ``stop`` set before either child has been scheduled.
        await asyncio.sleep(0)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(trigger_stop())
        run_task = tg.create_task(
            asyncio.wait_for(
                run_local_worker(
                    repo,  # type: ignore[arg-type]
                    runner,
                    plan,
                    WORKER_ID,
                    idle_wait=0,
                    stop=stop,
                ),
                timeout=2.0,
            )
        )

    stats = run_task.result()

    # Single release with the canonical reason and the correct version.
    assert repo.release_calls == [(TASK_ID, started_task.version, SHUTDOWN_RELEASE_REASON)]
    # Counters reflect the release, not a completion or failure.
    assert stats.released_on_shutdown == 1
    assert stats.completed == 0
    assert stats.failed == 0
    # No fail_task / complete_task spurious calls (sanity).
    assert repo.complete_calls == []
    assert repo.fail_calls == []


async def test_release_task_version_conflict_logged_and_loop_exits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the visibility-timeout sweep already released the task, we log + exit.

    The race: SIGTERM arrives, we cancel the runner, but before our
    ``release_task`` UPDATE commits the sweep already flipped the row to
    PENDING. Our UPDATE matches zero rows → :class:`VersionConflictError`.
    The end state is still what we wanted (PENDING, claim cleared), so
    the worker logs and exits cleanly — no crash, no spurious retries.
    """
    repo = FakeRepo()
    plan = _make_plan()
    stop = asyncio.Event()

    claimed_task = _make_task(version=1, status=TaskStatus.CLAIMED)
    started_task = _make_task(version=2, status=TaskStatus.IN_PROGRESS)

    repo.claim_results = [claimed_task]
    repo.start_results = [started_task]
    # Sweep beat us to it — the row is already PENDING at version 3.
    conflict = VersionConflictError(
        task_id=TASK_ID,
        expected_version=2,
        actual_version=3,
        actual_status=TaskStatus.PENDING,
    )
    repo.release_results = [conflict]

    runner_entered = asyncio.Event()

    async def runner(task: Task, prompt: str) -> AgentResult:
        runner_entered.set()
        await asyncio.sleep(60.0)
        raise AssertionError("runner should have been cancelled")

    async def trigger_stop() -> None:
        await runner_entered.wait()
        await asyncio.sleep(0)
        stop.set()

    with caplog.at_level(logging.WARNING, logger="whilly.worker.local"):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(trigger_stop())
            run_task = tg.create_task(
                asyncio.wait_for(
                    run_local_worker(
                        repo,  # type: ignore[arg-type]
                        runner,
                        plan,
                        WORKER_ID,
                        idle_wait=0,
                        stop=stop,
                    ),
                    timeout=2.0,
                )
            )

    stats = run_task.result()

    # Even though the release UPDATE conflicted, the loop exited cleanly.
    # ``released_on_shutdown`` is 0 because we didn't actually do the
    # release (someone else did) — the counter tracks our own work.
    assert stats.released_on_shutdown == 0
    assert stats.completed == 0
    assert stats.failed == 0

    # The conflict was logged at WARNING with the canonical phrasing.
    conflict_logs = [
        rec
        for rec in caplog.records
        if rec.name == "whilly.worker.local" and "release_task lost the race" in rec.getMessage()
    ]
    assert conflict_logs, (
        "expected a WARNING log for the release_task version conflict; "
        f"saw {[rec.getMessage() for rec in caplog.records]!r}"
    )


# --------------------------------------------------------------------------- #
# run_worker forwards stop end-to-end
# --------------------------------------------------------------------------- #


async def test_run_worker_forwards_stop_event_through_to_local() -> None:
    """An externally-set ``stop`` must reach the inner loop and trigger release.

    Pins the wiring contract between :func:`run_worker` (the composer)
    and :func:`run_local_worker` (the inner loop). A regression where
    the composer forgot to forward ``stop`` would surface as the
    inner loop running to completion of ``max_iterations`` instead of
    bailing out promptly — and the in-flight task would stay CLAIMED
    until the visibility-timeout sweep noticed.
    """
    repo = FakeRepo()
    plan = _make_plan()
    stop = asyncio.Event()

    claimed_task = _make_task(version=1, status=TaskStatus.CLAIMED)
    started_task = _make_task(version=2, status=TaskStatus.IN_PROGRESS)
    released_task = _make_task(version=3, status=TaskStatus.PENDING)

    repo.claim_results = [claimed_task]
    repo.start_results = [started_task]
    repo.release_results = [released_task]

    runner_entered = asyncio.Event()

    async def runner(task: Task, prompt: str) -> AgentResult:
        runner_entered.set()
        await asyncio.sleep(60.0)
        raise AssertionError("runner should have been cancelled")

    async def trigger_stop() -> None:
        await runner_entered.wait()
        await asyncio.sleep(0)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(trigger_stop())
        run_task = tg.create_task(
            asyncio.wait_for(
                run_worker(
                    repo,  # type: ignore[arg-type]
                    runner,
                    plan,
                    WORKER_ID,
                    idle_wait=0,
                    heartbeat_interval=10.0,
                    install_signal_handlers=False,  # pytest owns SIGINT
                    stop=stop,
                ),
                timeout=2.0,
            )
        )

    stats = run_task.result()

    assert stats.released_on_shutdown == 1, f"run_worker did not forward stop to run_local_worker; stats={stats!r}"
    assert repo.release_calls == [(TASK_ID, started_task.version, SHUTDOWN_RELEASE_REASON)]
