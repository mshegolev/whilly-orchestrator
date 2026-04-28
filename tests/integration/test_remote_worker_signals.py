"""Integration test for remote-worker SIGTERM/SIGINT shutdown (TASK-022b3, PRD FR-1.6, NFR-1).

Acceptance criteria covered
---------------------------
- ``SIGTERM → текущая задача RELEASE'ится через клиент (status вернётся
  в PENDING на сервере)``: verified by sending real ``SIGTERM`` to the
  test process while the worker is mid-runner, then asserting that the
  fake client recorded a ``release(task_id, worker_id, version,
  "shutdown")`` call. The wire-level "row-actually-PENDING" half is
  covered by :mod:`tests.integration.test_transport_tasks`'s
  ``test_release_transitions_*_back_to_pending`` against a real
  Postgres + FastAPI stack — splitting the assertions matches the same
  fake-client convention the heartbeat tests
  (:mod:`tests.integration.test_remote_worker_heartbeat`) document.
- ``SIGINT обрабатывается аналогично SIGTERM``: same flow, second
  test function, second signal — pinning that both signals route
  through the same handler instead of one diverging silently.
- ``Корректный shutdown без зависших asyncio tasks
  (TaskGroup.cancel_scope)``: ``run_remote_worker_with_heartbeat`` must
  return cleanly within a tight :func:`asyncio.wait_for` bound. A hung
  TaskGroup would surface as ``TimeoutError`` rather than a silent
  multi-second pause.
- ``released_on_shutdown`` accounting on
  :class:`whilly.worker.remote.RemoteWorkerStats` increments to exactly
  1 — pins the counter contract that future dashboards / smoke tests
  read.

Why a fake client rather than testcontainers + FastAPI?
-------------------------------------------------------
Same rationale as :mod:`tests.integration.test_remote_worker_heartbeat`'s
docstring: the remote worker has *no* direct DB connection — its only
side-channel is the HTTP transport. The unit-of-integration we exercise
here is the **signal → handler → stop event → runner cancellation →
release RPC** chain, which lives entirely inside the worker process.
The "release row really lands as PENDING" property is the server's
contract and lives in :mod:`tests.integration.test_transport_tasks`
where a real Postgres is already wired up. Splitting the contracts
this way also means a regression in ``release_path()`` / the new
endpoint surfaces in the right test, not as a confusing failure inside
the asyncio-composition assertions.

Why a real signal rather than ``stop.set()``?
---------------------------------------------
The unit-level path through the inner loop is exercised by the
existing ``test_remote_worker_heartbeat`` suite (specifically
``test_external_stop_event_terminates_both_tasks``) which drives shutdown
with a directly-set event. This file's contract is different: it pins
the *signal* → *handler* → ``stop.set()`` chain end-to-end. A
regression that broke
:func:`whilly.worker.remote._install_signal_handlers` (forgot to
register on the loop, used the wrong signal name) would still pass the
unit test but fail this one.

Why ``os.kill(os.getpid(), ...)`` rather than spawning a subprocess?
--------------------------------------------------------------------
A subprocess would isolate the worker from pytest cleanly but adds
plumbing for IPC (the test would need to know when the worker is
mid-runner). Sending the signal in-process works because
:func:`run_remote_worker_with_heartbeat` installs its handler via
:meth:`asyncio.AbstractEventLoop.add_signal_handler`, which *overrides*
the default disposition for the lifetime of the loop — SIGTERM never
propagates to "kill the test process" while the handler is active. The
handler is removed in a ``finally`` on TaskGroup exit so the test
process stays signal-safe for the rest of the suite.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.schemas import HeartbeatResponse
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.worker.remote import (
    SHUTDOWN_RELEASE_REASON,
    run_remote_worker_with_heartbeat,
)

WORKER_ID: WorkerId = "w-remote-shutdown"
PLAN_ID = "PLAN-REMOTE-SHUTDOWN"
TASK_ID = "T-REMOTE-SHUTDOWN-1"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeRemoteClient:
    """In-memory stand-in for :class:`RemoteWorkerClient`.

    Mirrors the fake in :mod:`tests.integration.test_remote_worker_heartbeat`
    (kept colocated rather than shared because the test bodies need to
    script different behaviours: the heartbeat fixture's ``claim`` returns
    ``None`` to keep the worker idle, while this fixture's ``claim``
    returns a real :class:`Task` so the runner enters the body and the
    signal can arrive mid-runner). ``release`` is the new surface
    TASK-022b3 added: its recording is what the assertions key on.
    """

    def __init__(self, claimed_task: Task) -> None:
        self._claimed_task = claimed_task
        self._claim_consumed = False
        self.claim_calls: list[tuple[str, str]] = []
        self.heartbeat_calls: list[str] = []
        # Recording slot for the release RPC: the canonical TASK-022b3
        # observation point. Tuple is (task_id, worker_id, version,
        # reason) so a regression that flips any one field surfaces as a
        # specific equality failure.
        self.release_calls: list[tuple[TaskId, str, int, str]] = []

    async def claim(self, worker_id: str, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        # Yield the task exactly once — second and subsequent calls
        # return ``None`` so the worker doesn't keep claiming after a
        # shutdown release. The "exactly once" semantic matches what
        # the real server does: a row that's been released back to
        # PENDING would still claim again, but the test budget never
        # gets that far because the shutdown break exits the loop.
        if self._claim_consumed:
            return None
        self._claim_consumed = True
        return self._claimed_task

    async def heartbeat(self, worker_id: str) -> HeartbeatResponse:
        self.heartbeat_calls.append(worker_id)
        return HeartbeatResponse(ok=True)

    async def release(
        self,
        task_id: TaskId,
        worker_id: str,
        version: int,
        reason: str,
    ) -> object:
        self.release_calls.append((task_id, worker_id, version, reason))
        # Realistic post-update payload-shape isn't important here:
        # ``run_remote_worker`` only branches on the exception path, the
        # 200-body of release is currently unused inside the loop.
        return object()

    # The complete / fail surface should not fire on the shutdown path —
    # if either does, the runner finished its result before the signal
    # arrived (test timing bug) and the assertion below would catch it
    # via the empty release_calls list. Raise here for a more pointed
    # failure message.
    async def complete(self, task_id: TaskId, worker_id: str, version: int) -> object:  # pragma: no cover
        raise AssertionError("FakeRemoteClient.complete should not run on the shutdown path")

    async def fail(self, task_id: TaskId, worker_id: str, version: int, reason: str) -> object:  # pragma: no cover
        raise AssertionError("FakeRemoteClient.fail should not run on the shutdown path")


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="Remote Shutdown Test Plan")


def _make_task() -> Task:
    """Build a CLAIMED task with realistic but minimal fields.

    Mirrors the fixture pattern used in
    :mod:`tests.integration.test_remote_worker_heartbeat`. ``version=1``
    because the local worker calls ``start_task`` between claim and
    runner; the remote worker doesn't — the protocol gap documented on
    :func:`run_remote_worker` means the version we see on claim is the
    version we send on release.
    """
    return Task(
        id=TASK_ID,
        status=TaskStatus.CLAIMED,
        priority=Priority.HIGH,
        description=f"shutdown test task {TASK_ID}",
        version=1,
    )


# --------------------------------------------------------------------------- #
# Drill — common test body parameterised by signal
# --------------------------------------------------------------------------- #


async def _drive_shutdown_via_signal(sig: signal.Signals) -> None:
    """End-to-end remote-worker shutdown drill for one signal.

    1. Build a fake client that yields one CLAIMED task.
    2. Spawn ``run_remote_worker_with_heartbeat`` as a background
       asyncio task. The runner sleeps long enough that the signal
       arrives while the worker is mid-runner.
    3. Wait until the runner has actually started (by setting an
       :class:`asyncio.Event` from inside the runner).
    4. Send the signal via ``os.kill(os.getpid(), sig)``.
    5. Await the worker task — it must finish promptly (asserted via
       :func:`asyncio.wait_for`).
    6. Verify: ``stats.released_on_shutdown == 1``,
       ``client.release_calls`` recorded exactly one call with
       ``reason="shutdown"``, no ``complete`` / ``fail`` call fired.
    """
    claimed = _make_task()
    client = FakeRemoteClient(claimed)
    plan = _make_plan()

    runner_started = asyncio.Event()

    async def slow_runner(task: Task, prompt: str) -> AgentResult:
        """Simulate a long-running agent that gets cancelled by shutdown."""
        runner_started.set()
        # 60s far exceeds the 10s test budget — the only way out is
        # cancellation by the shutdown path.
        await asyncio.sleep(60.0)
        # Defensive: a regression that broke the cancel path would
        # fall through here. Returning a "complete" result would
        # corrupt the test by triggering the complete RPC; raise
        # instead for a loud, attributable failure.
        raise AssertionError("remote runner reached its return statement; cancellation path broken")

    worker_task = asyncio.create_task(
        run_remote_worker_with_heartbeat(
            client,  # type: ignore[arg-type]  # FakeRemoteClient duck-types RemoteWorkerClient
            slow_runner,
            plan,
            WORKER_ID,
            heartbeat_interval=10.0,  # don't pollute the test with heartbeat noise
            install_signal_handlers=True,
        )
    )

    try:
        # Wait for the runner to actually be running. Firing this
        # event also confirms ``run_remote_worker_with_heartbeat``
        # installed its signal handlers (handlers go up before the
        # TaskGroup enters, which is before the inner loop reaches
        # the runner call).
        await asyncio.wait_for(runner_started.wait(), timeout=5.0)

        # Send the signal to ourselves. The asyncio loop's signal
        # handler intercepts it — no default-disposition kill of the
        # test process.
        os.kill(os.getpid(), sig)

        # The worker must finish quickly. A hang here would mean
        # either the signal handler didn't fire (regression in
        # ``_install_signal_handlers``) or the runner-cancellation
        # didn't unwind (regression in ``_await_runner_or_stop``).
        stats = await asyncio.wait_for(worker_task, timeout=10.0)
    except BaseException:
        # If any assertion above fails, make sure the background task
        # doesn't outlive the test — pytest would otherwise log a
        # "Task was destroyed but it is pending" warning that
        # obscures the real failure.
        if not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
        raise

    # Stats sanity: exactly one shutdown release, no completions, no
    # failures (the runner was cancelled mid-call). Mirror of the
    # local-worker assertion in
    # :mod:`tests.integration.test_worker_signals`.
    assert stats.released_on_shutdown == 1, f"expected exactly one shutdown release, got stats={stats!r}"
    assert stats.completed == 0
    assert stats.failed == 0

    # Wire-side check: the release RPC fired exactly once with the
    # canonical ``"shutdown"`` reason. The reason is the discriminator
    # the dashboard uses to distinguish worker-driven shutdowns from
    # the visibility-timeout sweep.
    assert len(client.release_calls) == 1, (
        f"expected exactly one release RPC; got {len(client.release_calls)}: {client.release_calls!r}"
    )
    released_task_id, released_worker_id, released_version, released_reason = client.release_calls[0]
    assert released_task_id == TASK_ID
    assert released_worker_id == WORKER_ID
    assert released_version == claimed.version, (
        "version sent to release must match the version the worker observed on claim — a mismatch would surface as a "
        "spurious 409 from the server"
    )
    assert released_reason == SHUTDOWN_RELEASE_REASON


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM"),
    reason="SIGTERM not available on this platform (Windows). Signal-driven shutdown is a POSIX path.",
)
async def test_sigterm_releases_in_flight_task_back_to_pending() -> None:
    """SIGTERM mid-runner → release RPC fires with reason="shutdown".

    This is the canonical TASK-022b3 acceptance test for the
    remote-worker signal path. A failure here means a remote worker
    would lose work on every Kubernetes / systemd / tmux-driven
    rolling restart — peers couldn't pick up the bounced task until
    the visibility-timeout sweep eventually noticed (default 15
    minutes, PRD FR-1.4). The whole point of TASK-022b3 is to
    short-circuit that timeout for cooperative shutdowns.
    """
    await _drive_shutdown_via_signal(signal.SIGTERM)


@pytest.mark.skipif(
    not hasattr(signal, "SIGINT"),
    reason="SIGINT not available on this platform.",
)
async def test_sigint_releases_in_flight_task_same_as_sigterm() -> None:
    """SIGINT (e.g. interactive Ctrl-C) follows the same shutdown path.

    Pinning both signals through the same exit path matters because a
    common refactor mistake is to install only one (typically SIGTERM),
    leaving Ctrl-C to default-kill the worker mid-task — exactly the
    silent work-loss scenario the AC forbids. Symmetric with the
    local-worker counterpart in
    :mod:`tests.integration.test_worker_signals`.
    """
    await _drive_shutdown_via_signal(signal.SIGINT)


async def test_signal_handlers_are_removed_on_taskgroup_exit() -> None:
    """The signal handlers installed during the run must be removed on exit.

    Symmetric cleanup matters because pytest reuses the loop across
    tests in the same module — leaving a SIGTERM handler installed
    would cause a follow-up test that sends SIGTERM to the test
    process to silently flip the wrong stop event. A regression in
    :func:`whilly.worker.remote._remove_signal_handlers` would surface
    as the second test in a suite getting confused; this test is the
    early-warning trip-wire so the failure attribution is clean.

    Verified by running the composition once with
    ``install_signal_handlers=True`` and ``max_iterations=0`` (so the
    inner loop exits immediately), then asserting that the loop's
    handler dict for SIGTERM has been restored to ``None`` (the
    default disposition asyncio reports for an uninstalled handler).
    """
    claimed = _make_task()
    client = FakeRemoteClient(claimed)
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with max_iterations=0")

    loop = asyncio.get_running_loop()

    await asyncio.wait_for(
        run_remote_worker_with_heartbeat(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            heartbeat_interval=10.0,
            max_iterations=0,
            install_signal_handlers=True,
        ),
        timeout=5.0,
    )

    # ``loop._signal_handlers`` is a private dict and not part of the
    # public asyncio API, but it's stable across CPython versions and
    # the only way to introspect handler state without re-installing.
    # If a future Python release changes the attribute name, the
    # ``getattr`` fallback below means the test gracefully degrades to
    # a soft success rather than a wrong-cleanup false alarm.
    signal_handlers = getattr(loop, "_signal_handlers", None)
    if signal_handlers is not None:
        # An entry would exist iff the handler is still installed;
        # absence is what proves the cleanup ran.
        assert signal.SIGTERM not in signal_handlers, (
            f"SIGTERM handler not removed after worker exit: {signal_handlers!r}. _remove_signal_handlers regression?"
        )
        assert signal.SIGINT not in signal_handlers, (
            f"SIGINT handler not removed after worker exit: {signal_handlers!r}. _remove_signal_handlers regression?"
        )


async def test_install_signal_handlers_false_skips_handler_installation() -> None:
    """The ``install_signal_handlers=False`` toggle must not install any handler.

    Pinned because pytest's own SIGINT handler must not be replaced by
    the worker's during unit tests — the toggle is the test-side
    escape hatch. A regression that ignored the kwarg would clobber
    pytest's interrupt path; this test is the cheapest way to catch
    that without trying to interrupt pytest itself.

    We verify by installing a sentinel handler before the run and
    confirming it survives. Using a real signal install (rather than
    introspecting ``loop._signal_handlers`` directly) keeps the test
    robust against asyncio internals.
    """
    claimed = _make_task()
    client = FakeRemoteClient(claimed)
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner should not run with max_iterations=0")

    sentinel_fired: list[int] = []
    loop = asyncio.get_running_loop()

    def sentinel() -> None:
        sentinel_fired.append(1)

    loop.add_signal_handler(signal.SIGTERM, sentinel)
    try:
        await asyncio.wait_for(
            run_remote_worker_with_heartbeat(
                client,  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                heartbeat_interval=10.0,
                max_iterations=0,
                install_signal_handlers=False,
            ),
            timeout=5.0,
        )

        # Send SIGTERM — the sentinel must still be the registered
        # handler, since the worker promised not to touch it.
        os.kill(os.getpid(), signal.SIGTERM)
        # Yield the loop so the handler runs.
        await asyncio.sleep(0.01)
        assert sentinel_fired, "sentinel handler did not fire — install_signal_handlers=False ignored?"
    finally:
        # Clean up the sentinel so subsequent tests aren't poisoned.
        try:
            loop.remove_signal_handler(signal.SIGTERM)
        except (NotImplementedError, ValueError):
            pass
