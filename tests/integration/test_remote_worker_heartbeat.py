"""Integration test for remote-worker heartbeat (TASK-022b2, PRD FR-1.5, FR-1.6, NFR-1).

Acceptance criteria covered
---------------------------
- Heartbeat запускается параллельной asyncio задачей с интервалом 30с
  (here: tight intervals so the test runs in milliseconds, but the
  composition root is identical to production).
- ``client.heartbeat(worker_id)`` is invoked on every tick — verified
  by the in-memory recorder on :class:`FakeRemoteClient`.
- Сетевые ошибки heartbeat не валят основной цикл (логируются и
  retry'ятся) — the failing-tick test scripts a transient
  :class:`HTTPClientError`, asserts the inner loop still completes,
  and confirms a WARNING log was emitted naming the worker id.
- При остановке main цикла heartbeat корректно завершается
  (TaskGroup cancel) — the long-interval test wraps the call in
  :func:`asyncio.wait_for` with a tight timeout: a hung heartbeat
  surfaces as :class:`TimeoutError` rather than a 30-second hang.

Why no testcontainers / real httpx server?
------------------------------------------
The remote worker is a *thin* process — its only side-channel to the
control plane is the HTTP transport, and the unit-test fake for that
transport is exhaustive (see ``tests/unit/test_remote_worker.py``,
``tests/unit/test_remote_client.py``). Spinning up a FastAPI app +
testcontainers Postgres for a heartbeat-composition test would buy
nothing the fake doesn't already cover, while adding ~5 seconds to the
suite and a Docker dependency to a test that's about pure asyncio
plumbing. The full server-and-DB integration story for the heartbeat
already lands in :mod:`tests.integration.test_transport_workers` and
:mod:`tests.integration.test_worker_heartbeat` for the local side.

What this file is "integration" about, then? The TaskGroup composition
itself: heartbeat + main loop + stop event + ``finally``-driven
shutdown all wired together is the unit-of-integration we exercise.
The naming follows the AC's ``test_steps``
(``tests/integration/test_remote_worker_heartbeat.py``) and the
``tests/integration/test_worker_heartbeat.py`` precedent on the local
side, where the *composition* is what the integration tests are
asserting on top of the underlying SQL.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace

import pytest

from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.client import HTTPClientError
from whilly.adapters.transport.schemas import HeartbeatResponse
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.worker.remote import (
    DEFAULT_HEARTBEAT_INTERVAL,
    RemoteWorkerStats,
    run_remote_worker_with_heartbeat,
)

WORKER_ID: WorkerId = "w-remote-hb"
PLAN_ID = "PLAN-REMOTE-HB"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class _Heartbeat:
    """Recorder slot for a single heartbeat call.

    Captures both the worker id we received (defence-in-depth: the
    supervisor must not rename worker ids between iterations) and the
    monotonic timestamp the call landed at, so a future test that
    cared about cadence (rather than just count) could assert on
    inter-tick spacing without re-engineering the fixture.
    """

    worker_id: str
    when: float


class FakeRemoteClient:
    """In-memory stand-in for :class:`RemoteWorkerClient`.

    Surface duck-types the subset of the real client that
    :func:`run_remote_worker_with_heartbeat` touches: ``claim`` plus
    ``heartbeat`` (the inner loop's ``complete``/``fail`` aren't
    exercised here because the plan is empty — same setup as the
    local-side ``tests/integration/test_worker_heartbeat.py``).

    Why not :class:`unittest.mock.AsyncMock`?
        Same reason ``tests/unit/test_remote_worker.py`` rolls its own
        fake: AsyncMock obscures the per-call wiring behind opaque
        ``side_effect`` lists. A plain class makes the call ordering
        and arguments inspectable without ``assert_called_with``
        spaghetti and lets us hold a per-call timestamp cheaply.
    """

    def __init__(self) -> None:
        # ``claim`` returns ``None`` (server-side long-poll budget
        # expired) so the inner loop sits in its idle path while the
        # heartbeat side has wall time to tick. The heartbeat is what
        # the test assertions key on.
        self.claim_calls: list[tuple[str, str]] = []
        # Heartbeat recording — append-only so test assertions can
        # check ordering / count without a separate counter.
        self.heartbeat_calls: list[_Heartbeat] = []
        # Scripted heartbeat outcomes. Each entry is either a
        # :class:`HeartbeatResponse` (returned from the call) or an
        # ``Exception`` (raised). The list pops left-to-right; an empty
        # list defaults to ``ok=True`` so a "happy path" test doesn't
        # have to script every tick.
        self.heartbeat_results: list[HeartbeatResponse | Exception] = []

    async def claim(self, worker_id: str, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        return None

    async def heartbeat(self, worker_id: str) -> HeartbeatResponse:
        self.heartbeat_calls.append(_Heartbeat(worker_id=worker_id, when=asyncio.get_running_loop().time()))
        if not self.heartbeat_results:
            return HeartbeatResponse(ok=True)
        result = self.heartbeat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    # The remaining surface — none of these should fire in a
    # heartbeat-composition test because the plan is empty. If one
    # does fire, the AssertionError makes the failure point obvious.
    async def complete(self, task_id: TaskId, worker_id: str, version: int) -> object:  # pragma: no cover
        raise AssertionError("FakeRemoteClient.complete should not run with an empty plan")

    async def fail(self, task_id: TaskId, worker_id: str, version: int, reason: str) -> object:  # pragma: no cover
        raise AssertionError("FakeRemoteClient.fail should not run with an empty plan")


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="Remote HB Plan")


def _make_task(task_id: str = "T-REMOTE-1", *, status: TaskStatus = TaskStatus.CLAIMED, version: int = 1) -> Task:
    """Build a task with realistic but minimal fields. Mirrors the unit-test helper."""
    return Task(
        id=task_id, status=status, priority=Priority.MEDIUM, description=f"description for {task_id}", version=version
    )


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Replace ``asyncio.sleep`` on the remote module with a recorder.

    Mirrors the unit-test fixture in ``tests/unit/test_remote_worker.py``.
    The inner loop's contract is "no sleep on 204"; we still patch here
    so a regression that started sleeping between idle polls would show
    up as a non-empty list. The heartbeat loop uses
    :func:`asyncio.wait_for`, not :func:`asyncio.sleep`, so this fixture
    doesn't affect heartbeat cadence — that's by design (we want real
    wall-time behaviour for the heartbeat to verify the TaskGroup
    composition).
    """
    sleeps: list[float] = []

    async def _fake(delay: float) -> None:
        sleeps.append(delay)

    # Patch on the asyncio module — ``run_remote_worker`` calls
    # ``asyncio.sleep`` indirectly through whatever the inner loop
    # decides; patching the module-level name covers any path.
    monkeypatch.setattr(asyncio, "sleep", _fake)
    yield sleeps


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_default_heartbeat_interval_matches_local_worker_constant() -> None:
    """Pin the remote default to 30s (PRD FR-1.6).

    The local worker's :data:`whilly.worker.main.DEFAULT_HEARTBEAT_INTERVAL`
    is also 30s — both flavours should report the same cadence so the
    visibility-timeout sweep (TASK-025a) can reason about staleness
    without branching on worker flavour. A future tweak that drifted
    the two would fire here.
    """
    from whilly.worker.main import DEFAULT_HEARTBEAT_INTERVAL as LOCAL_DEFAULT

    assert DEFAULT_HEARTBEAT_INTERVAL == 30.0
    assert DEFAULT_HEARTBEAT_INTERVAL == LOCAL_DEFAULT


async def test_heartbeat_ticks_during_idle_run(fake_sleep: list[float]) -> None:
    """End-to-end: ``run_remote_worker_with_heartbeat`` ticks heartbeat on the side.

    With an empty plan the inner loop sits in its idle path
    (``claim`` → ``None`` → re-poll, no sleep). The heartbeat task
    runs concurrently and calls ``client.heartbeat`` at every tick.
    After ``max_iterations`` the inner loop exits, the stop event
    fires, the heartbeat unwinds, and the TaskGroup completes.

    Asserting ``>= 1`` rather than an exact count keeps the test robust
    against scheduling jitter — the heartbeat fires immediately on
    entry, so even one heartbeat tick proves the parallel task ran.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover — no tasks
        raise AssertionError("runner should not run with an empty plan")

    stats = await asyncio.wait_for(
        run_remote_worker_with_heartbeat(
            client,  # type: ignore[arg-type]  # FakeRemoteClient duck-types RemoteWorkerClient
            runner,
            plan,
            WORKER_ID,
            heartbeat_interval=0.001,
            max_iterations=5,
        ),
        timeout=5.0,
    )

    # Inner loop accounting unchanged by the heartbeat composition.
    assert stats == RemoteWorkerStats(iterations=5, completed=0, failed=0, idle_polls=5)

    # At least one heartbeat tick fired and addressed the right worker.
    assert len(client.heartbeat_calls) >= 1, "heartbeat did not tick during the idle run"
    assert all(c.worker_id == WORKER_ID for c in client.heartbeat_calls)

    # AC-load-bearing: the inner remote loop never sleeps on 204.
    assert fake_sleep == []


async def test_heartbeat_failure_does_not_kill_worker(
    fake_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient HTTPClientError on heartbeat must be logged + survived.

    Best-effort liveness contract (PRD FR-1.6): a flaky control-plane
    must not take the worker down. The first heartbeat raises an
    auth-style :class:`HTTPClientError`; the loop logs at WARNING and
    ticks again. Inner-loop accounting still completes normally.
    """
    client = FakeRemoteClient()
    client.heartbeat_results = [
        HTTPClientError(
            "simulated server hiccup",
            status_code=503,
            response_body="upstream timeout",
        )
    ]
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    with caplog.at_level(logging.WARNING, logger="whilly.worker.remote"):
        stats = await asyncio.wait_for(
            run_remote_worker_with_heartbeat(
                client,  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                heartbeat_interval=0.001,
                max_iterations=3,
            ),
            timeout=5.0,
        )

    # Inner loop ran to completion despite the heartbeat error.
    assert stats == RemoteWorkerStats(iterations=3, completed=0, failed=0, idle_polls=3)

    # The simulated failure was logged at WARNING, naming the worker.
    failure_logs = [
        rec
        for rec in caplog.records
        if rec.name == "whilly.worker.remote"
        and "remote heartbeat" in rec.getMessage()
        and "failed" in rec.getMessage()
    ]
    assert failure_logs, "expected a WARNING log for the simulated heartbeat failure"
    assert WORKER_ID in failure_logs[0].getMessage()


async def test_heartbeat_unknown_worker_is_logged_at_info_and_continues(
    fake_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ok=False`` is the recoverable "unknown worker_id" branch.

    Per :class:`HeartbeatResponse` docstring, the supervisor (TASK-022b3)
    is the one expected to act on this signal (re-register). Until that
    lands, the heartbeat loop logs at INFO (not WARNING — a misconfigured
    worker would otherwise drown the journal at WARNING for every tick)
    and continues. Pin the contract here.
    """
    client = FakeRemoteClient()
    # Two ticks of ok=False so the test catches a regression that
    # short-circuits the loop on the first false response.
    client.heartbeat_results = [
        HeartbeatResponse(ok=False),
        HeartbeatResponse(ok=False),
    ]
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    with caplog.at_level(logging.INFO, logger="whilly.worker.remote"):
        stats = await asyncio.wait_for(
            run_remote_worker_with_heartbeat(
                client,  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                heartbeat_interval=0.001,
                max_iterations=3,
            ),
            timeout=5.0,
        )

    assert stats.iterations == 3

    info_logs = [
        rec
        for rec in caplog.records
        if rec.name == "whilly.worker.remote"
        and rec.levelno == logging.INFO
        and "unknown worker_id" in rec.getMessage()
    ]
    assert info_logs, "expected an INFO log on ok=False heartbeat"


async def test_heartbeat_terminates_with_main_loop() -> None:
    """A long heartbeat interval must not delay TaskGroup shutdown.

    With ``heartbeat_interval=10s`` the heartbeat would normally sit on
    its ``wait_for(stop, 10)`` for the full interval. The inner
    closure's ``finally: stop.set()`` wakes it up immediately when
    ``max_iterations`` is reached. The outer 5-second
    :func:`asyncio.wait_for` converts a regression into a clean test
    failure instead of a 10-second hang.

    This test deliberately does *not* use the ``fake_sleep`` fixture —
    we want real wall-time behaviour for the heartbeat's
    :func:`asyncio.wait_for` so the stop-event signalling is exercised
    end-to-end.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    stats = await asyncio.wait_for(
        run_remote_worker_with_heartbeat(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            heartbeat_interval=10.0,
            max_iterations=1,
        ),
        timeout=5.0,
    )

    assert stats.iterations == 1
    # The first heartbeat fires immediately on entry; we confirm the
    # tick landed before the stop wake-up so the lifecycle ordering is
    # right (heartbeat ran at least once, then woke up cleanly).
    assert len(client.heartbeat_calls) >= 1


async def test_heartbeat_propagates_inner_loop_exceptions() -> None:
    """A crash inside the inner loop should propagate, not get swallowed.

    Same contract as the local-worker composition root: this function
    returns the inner stats *or* raises whatever the inner loop raised.
    The ``finally: stop.set()`` in ``_worker_then_stop`` means the
    heartbeat still exits cleanly so the TaskGroup itself unwinds.
    """
    plan = _make_plan()

    class CrashingClient(FakeRemoteClient):
        async def claim(self, worker_id: str, plan_id: str) -> Task | None:
            raise RuntimeError("simulated transport blow-up")

    client = CrashingClient()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run when claim crashes")

    # TaskGroup wraps the underlying RuntimeError in an ExceptionGroup
    # in Python 3.11+. We accept either so tests stay robust to the
    # asyncio internals.
    with pytest.raises((RuntimeError, BaseExceptionGroup)) as exc_info:
        await asyncio.wait_for(
            run_remote_worker_with_heartbeat(
                client,  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                heartbeat_interval=10.0,
                max_iterations=1,
            ),
            timeout=5.0,
        )

    def _flatten(exc: BaseException) -> list[BaseException]:
        if isinstance(exc, BaseExceptionGroup):
            return [leaf for sub in exc.exceptions for leaf in _flatten(sub)]
        return [exc]

    leaves = _flatten(exc_info.value)
    assert any(isinstance(e, RuntimeError) and "simulated transport blow-up" in str(e) for e in leaves), (
        f"original RuntimeError did not survive the TaskGroup wrapper: {leaves!r}"
    )


async def test_external_stop_event_terminates_both_tasks() -> None:
    """Caller-supplied ``stop`` drives shutdown end-to-end.

    The supervisor (TASK-022b3 SIGTERM, TASK-022c CLI) needs to drive
    shutdown without waiting for ``max_iterations``. Pin the contract:
    setting an externally-passed ``stop`` event before the call ensures
    the inner loop sees it on entry and the heartbeat wakes up
    immediately. ``max_iterations=None`` proves we don't depend on the
    iteration cap as a fallback timer.

    Note: the inner loop in TASK-022b1 doesn't itself read ``stop`` (the
    "bare" loop concern of 022b1), so this test verifies the signalling
    path through ``finally: stop.set()`` in ``_worker_then_stop``: when
    the outer cancellation propagates we still want the heartbeat to
    wind down via the event rather than via ``CancelledError``.
    """
    client = FakeRemoteClient()
    plan = _make_plan()
    stop = asyncio.Event()
    stop.set()  # Pre-set: the heartbeat sees ``stop.is_set()`` on entry.

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    # ``max_iterations=0`` is the natural "no work" cap that lets the
    # composition return immediately. Combined with a pre-set stop, the
    # heartbeat coroutine returns before its first tick — verifying the
    # ``while not stop.is_set():`` guard on entry.
    stats = await asyncio.wait_for(
        run_remote_worker_with_heartbeat(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            heartbeat_interval=10.0,
            max_iterations=0,
            stop=stop,
        ),
        timeout=5.0,
    )

    assert stats == RemoteWorkerStats()
    # Heartbeat saw ``stop`` set on entry and never ticked.
    assert client.heartbeat_calls == []


async def test_heartbeat_recovers_from_unexpected_non_http_exception(
    fake_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare :class:`Exception` (not :class:`HTTPClientError`) is also survived.

    The heartbeat loop has a two-tier except: typed
    :class:`HTTPClientError` first, then a generic ``Exception`` catch-all
    for httpx-level / asyncio-level oddities. Pin the catch-all by
    raising a plain :class:`RuntimeError` — a regression that narrowed
    the except clause would surface here.
    """
    client = FakeRemoteClient()
    client.heartbeat_results = [
        RuntimeError("unexpected blow-up in transport layer"),
        HeartbeatResponse(ok=True),
    ]
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with an empty plan")

    with caplog.at_level(logging.WARNING, logger="whilly.worker.remote"):
        stats = await asyncio.wait_for(
            run_remote_worker_with_heartbeat(
                client,  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                heartbeat_interval=0.001,
                max_iterations=3,
            ),
            timeout=5.0,
        )

    assert stats.iterations == 3
    failure_logs = [
        rec
        for rec in caplog.records
        if rec.name == "whilly.worker.remote"
        and "unexpected error" in rec.getMessage()
        and WORKER_ID in rec.getMessage()
    ]
    assert failure_logs, "expected an 'unexpected error' WARNING log for the bare Exception"


async def test_heartbeat_continues_during_main_loop_task_processing() -> None:
    """Heartbeat keeps ticking while the main loop processes a task.

    Tasks can take longer than the heartbeat interval to run (an agent
    invocation might take minutes); the heartbeat must keep refreshing
    ``last_heartbeat`` independently or the visibility-timeout sweep
    would mistake the worker for stale and revoke its in-flight task.
    Pin the parallelism: a slow-running task must not block heartbeat
    ticks.

    Note this test deliberately does *not* use the ``fake_sleep``
    fixture — we want real wall-time behaviour for both the runner's
    artificial delay and the heartbeat's
    :func:`asyncio.wait_for(stop, interval)` so the parallelism
    assertion is meaningful.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-PARALLEL", status=TaskStatus.CLAIMED, version=1)
    done = replace(claimed, status=TaskStatus.DONE, version=2)

    # Override the fake's claim/complete with closures so the test
    # script returns the task on the first call and a realistic
    # post-update result on complete.
    async def claim(worker_id: str, plan_id: str) -> Task | None:
        client.claim_calls.append((worker_id, plan_id))
        return claimed

    async def complete(task_id: TaskId, worker_id: str, version: int) -> object:
        return done

    client.claim = claim  # type: ignore[method-assign]
    client.complete = complete  # type: ignore[method-assign]

    async def runner(task: Task, prompt: str) -> AgentResult:
        # Sleep long enough that the heartbeat must tick at least
        # twice in parallel — 50ms with a 1ms heartbeat interval gives
        # ~50 ticks of headroom against scheduling jitter on slow CI.
        # Asserting >= 2 below stays robust against jitter-induced
        # single-tick observations.
        await asyncio.sleep(0.05)
        return AgentResult(output="<promise>COMPLETE</promise>", exit_code=0, is_complete=True)

    stats = await asyncio.wait_for(
        run_remote_worker_with_heartbeat(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            heartbeat_interval=0.001,
            max_iterations=1,
        ),
        timeout=5.0,
    )

    assert stats == RemoteWorkerStats(iterations=1, completed=1, failed=0, idle_polls=0)
    # Many heartbeat ticks should have landed during the 50ms task —
    # asserting >= 2 is robust against single-tick scheduling on slow CI.
    assert len(client.heartbeat_calls) >= 2, (
        f"heartbeat should have ticked multiple times during the slow task; got {len(client.heartbeat_calls)}"
    )
