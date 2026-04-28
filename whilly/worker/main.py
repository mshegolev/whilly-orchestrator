"""Local worker composition root with heartbeat + signals (TASK-019b1/b2, PRD FR-1.6, NFR-1).

This module is one notch up the composition stack from
:mod:`whilly.worker.local`. ``local.run_local_worker`` is the bare claim →
start → run → complete loop; the present module pairs it with a parallel
heartbeat task under a single :class:`asyncio.TaskGroup`, plus SIGTERM /
SIGINT signal handlers (TASK-019b2) that flip a shared shutdown event so
the worker releases its in-flight task back to ``PENDING`` and exits
cleanly when the process supervisor kills it.

Why a separate module rather than folding heartbeat into
:mod:`whilly.worker.local`?
    The 019a slice is intentionally I/O-narrow — one concern, one queue
    of repo calls, exhaustive unit-test coverage. Heartbeat adds a second
    independent concurrency dimension (it ticks regardless of whether a
    task is in flight). Keeping it here means each module stays small
    enough to reason about in isolation, and TASK-019c's CLI can
    substitute either entry point without rewiring the inner loop.

Liveness contract (PRD FR-1.4 / FR-1.6)
---------------------------------------
The visibility-timeout sweep (TASK-009d / TASK-025) reclaims rows whose
``claimed_at`` predates ``NOW() - visibility_timeout`` — but ``claimed_at``
is set once at claim time, so a long-running agent would look stale to the
sweep without an independent liveness signal. ``workers.last_heartbeat`` is
that signal: every :data:`DEFAULT_HEARTBEAT_INTERVAL` seconds the worker
refreshes its row, and the sweep / dashboard read it to decide which workers
are alive. 30s gives ~30 heartbeats of headroom inside the default 15-minute
visibility timeout.

TaskGroup composition
---------------------
:func:`run_worker` pins both coroutines to one :class:`asyncio.TaskGroup`:

* The main worker task delegates straight to
  :func:`whilly.worker.local.run_local_worker` and stamps an
  :class:`asyncio.Event` (``stop``) on exit so the heartbeat coroutine
  can wind down without external cancellation.
* The heartbeat task loops ``update_heartbeat`` + ``wait_for(stop, interval)``
  until ``stop`` fires.

Why a stop event rather than ``heartbeat_task.cancel()``?
    Explicit cancellation surfaces a :class:`asyncio.CancelledError` from
    the cancelled task, which :class:`asyncio.TaskGroup` treats as a
    cancellation request that should propagate. Using a stop event lets
    the heartbeat exit *normally* — the TaskGroup just awaits both
    children, sees clean returns, and unwinds without exception
    plumbing. The SIGTERM / SIGINT signal handlers (TASK-019b2) set the
    same event from inside the asyncio loop via
    ``loop.add_signal_handler``, so a ``kill -TERM`` arrives as ordinary
    cooperative shutdown rather than a default-disposition process kill.

Signal handling (TASK-019b2)
----------------------------
``run_worker`` installs handlers for SIGTERM and SIGINT via
:meth:`asyncio.AbstractEventLoop.add_signal_handler`. Both flip the same
``stop`` event the heartbeat / loop already use. The inner
``run_local_worker`` races each runner call against ``stop`` and, on a
mid-runner shutdown, calls :meth:`TaskRepository.release_task` to put the
in-flight task back to ``PENDING`` before exiting. Net effect: a peer
worker (or this worker on restart) re-claims it within one poll cycle, no
work is lost, and the audit log carries a ``RELEASE`` event with
``payload.reason = "shutdown"`` so post-mortems can tell signal-driven
releases apart from visibility-timeout sweeps.

Handlers are installed only on the main thread of an asyncio loop that
supports them — Windows' ``ProactorEventLoop`` and any non-main thread
raise :class:`NotImplementedError` from ``add_signal_handler``. We catch
that and silently degrade: callers in those environments still get
heartbeats and can shut down via ``max_iterations`` / outer cancellation.
The ``install_signal_handlers=False`` parameter is the test-side toggle —
pytest's own SIGINT handler must not be replaced by the worker's during
unit tests.

Failure isolation
-----------------
A single ``update_heartbeat`` exception (network blip, transient
asyncpg disconnect) is logged and the loop ticks again — heartbeat is
strictly best-effort and must never kill the worker. The repository
already returns ``False`` rather than raising on a missing worker row;
real exceptions are unexpected enough to log loudly without giving the
loop the satisfaction of crashing the whole TaskGroup over them.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from typing import Final

from whilly.adapters.db.repository import TaskRepository
from whilly.core.models import Plan, WorkerId
from whilly.worker.local import (
    DEFAULT_IDLE_WAIT,
    RunnerCallable,
    WorkerStats,
    run_local_worker,
)

# Signals we install handlers for in :func:`run_worker`. SIGTERM is the
# standard process-supervisor shutdown signal (Kubernetes, systemd, tmux
# kill-window); SIGINT is the same path for ``Ctrl-C`` from an interactive
# shell. Both must end with the in-flight task released to ``PENDING`` so a
# peer can re-claim it.
_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = (
    signal.SIGTERM,
    signal.SIGINT,
)

log = logging.getLogger(__name__)

# 30s aligns with the PRD's heartbeat cadence (FR-1.6) and gives roughly
# 30 ticks of headroom inside the default 15-minute visibility timeout
# (TASK-025). Tests pass a smaller value to make the loop tick fast.
DEFAULT_HEARTBEAT_INTERVAL: Final[float] = 30.0


async def run_heartbeat_loop(
    repo: TaskRepository,
    worker_id: WorkerId,
    *,
    interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    stop: asyncio.Event,
) -> None:
    """Refresh ``workers.last_heartbeat`` every ``interval`` seconds until ``stop``.

    The first tick fires immediately so a freshly-started worker shows
    a fresh ``last_heartbeat`` from the moment its main loop begins
    polling — no one-interval gap where the sweep could mistake a brand-
    new worker for a stale one.

    Subsequent waits use :func:`asyncio.wait_for` against ``stop.wait()``
    so a graceful shutdown wakes up the loop without waiting out the full
    interval. ``TimeoutError`` is the "interval elapsed, tick again"
    path; a normal return from the wait means ``stop`` fired and we
    exit.

    Heartbeat exceptions are intentionally swallowed (logged at
    WARNING). Heartbeat is best-effort liveness — if it fails, the
    worker stays alive and the visibility-timeout sweep will eventually
    reclaim the in-flight task; killing the worker over a transient
    update failure trades a recoverable problem for an unrecoverable
    one. :class:`asyncio.CancelledError` is *not* swallowed (re-raised
    via ``except Exception`` not catching it), so structured
    cancellation still works.
    """
    while not stop.is_set():
        try:
            await repo.update_heartbeat(worker_id)
        except Exception as exc:
            # Best-effort: log and keep ticking. CancelledError bypasses
            # this except clause (it inherits from BaseException, not
            # Exception) so structured shutdown still works.
            log.warning(
                "worker=%s heartbeat update failed (%s); will retry next tick",
                worker_id,
                exc,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            # ``stop`` fired — return immediately, no further tick.
            return
        except TimeoutError:
            # Interval elapsed, no shutdown request — loop and tick again.
            continue


def _install_signal_handlers(stop: asyncio.Event) -> list[signal.Signals]:
    """Install SIGTERM / SIGINT handlers that flip ``stop`` (TASK-019b2).

    Returns the list of signals whose handlers were actually installed —
    the caller passes that list back to :func:`_remove_signal_handlers`
    on exit so we don't leak handlers across sequential ``run_worker``
    invocations or between tests.

    Two layers of defensive degradation:

    * ``loop.add_signal_handler`` raises :class:`NotImplementedError` on
      Windows ``ProactorEventLoop`` and on any non-main thread. We catch
      and skip — the worker stays functional, just without graceful
      signal shutdown (callers can still use ``max_iterations`` or outer
      cancellation).
    * Anything else propagating out of ``add_signal_handler`` indicates a
      genuinely broken loop; we let it surface so the worker fails fast
      instead of pretending it's wired up.
    """
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _make_shutdown_handler(stop, sig))
        except NotImplementedError:
            log.debug(
                "signal handler for %s not installable on this loop; skipping",
                sig.name,
            )
            continue
        installed.append(sig)
    if installed:
        log.info(
            "worker signal handlers installed for %s",
            ", ".join(s.name for s in installed),
        )
    return installed


def _remove_signal_handlers(installed: list[signal.Signals]) -> None:
    """Restore default signal disposition for handlers we installed.

    Symmetric with :func:`_install_signal_handlers`. Errors during
    teardown are logged but never raised — a failed cleanup must not
    mask whatever exception the caller is unwinding through.
    """
    if not installed:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Loop already gone (called from an outer exception path with a
        # shut-down loop); nothing left to clean up.
        return
    for sig in installed:
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, ValueError, RuntimeError) as exc:
            log.debug("could not remove signal handler for %s: %r", sig.name, exc)


def _make_shutdown_handler(stop: asyncio.Event, sig: signal.Signals) -> Callable[[], None]:
    """Build a thread-safe handler that flips ``stop`` and logs the signal.

    The handler runs on the asyncio loop (because we registered it via
    ``loop.add_signal_handler``), so calling :meth:`asyncio.Event.set`
    from inside is safe — no cross-thread synchronization needed. We
    keep the body trivial: flipping the event is sufficient, and the
    rest of the shutdown logic lives in ``run_local_worker`` where the
    state-transition context is.

    Logging at INFO so a SIGTERM kill from kubectl / systemd / tmux
    leaves an unambiguous breadcrumb in the worker journal — operators
    investigating "why did this worker exit" can correlate with the
    process supervisor's log without reading code.
    """

    def _handler() -> None:
        log.info("received %s; requesting graceful shutdown", sig.name)
        stop.set()

    return _handler


async def run_worker(
    repo: TaskRepository,
    runner: RunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    idle_wait: float = DEFAULT_IDLE_WAIT,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    max_iterations: int | None = None,
    install_signal_handlers: bool = True,
    stop: asyncio.Event | None = None,
) -> WorkerStats:
    """Run :func:`run_local_worker` paired with heartbeat + signal handlers.

    Both coroutines run under one :class:`asyncio.TaskGroup`. When the
    main worker loop returns — because ``max_iterations`` was reached
    (tests), ``stop`` was set (production: SIGTERM, TASK-019b2), or an
    outer cancellation propagated — the loop ``finally`` block sets the
    shared :class:`asyncio.Event`. The heartbeat coroutine wakes up,
    returns cleanly, and the TaskGroup exits. No explicit cancellation,
    no ``CancelledError`` plumbing — see module docstring.

    Parameters
    ----------
    repo, runner, plan, worker_id, idle_wait, max_iterations:
        Forwarded to :func:`whilly.worker.local.run_local_worker`. See
        that function for the full per-iteration contract.
    heartbeat_interval:
        Seconds between heartbeat ticks. Defaults to
        :data:`DEFAULT_HEARTBEAT_INTERVAL` (30s). Tests pass a small
        value so the heartbeat ticks observably during a short test run.
    install_signal_handlers:
        When ``True`` (production default), SIGTERM and SIGINT are
        installed via :meth:`asyncio.AbstractEventLoop.add_signal_handler`
        and flip the shared ``stop`` event. Tests pass ``False`` to
        avoid clobbering pytest's own SIGINT handler (and to keep unit
        tests deterministic across loop implementations).
    stop:
        Optional shared shutdown event. Callers that already own a
        shutdown signal (CLI in TASK-019c, integration tests that drive
        shutdown without sending real signals) pass it in; otherwise
        ``run_worker`` allocates one internally. Either way the same
        event is forwarded to ``run_local_worker`` and to the heartbeat
        loop.

    Returns
    -------
    WorkerStats
        The :class:`whilly.worker.local.WorkerStats` returned by the
        inner loop — same fields, same semantics. The heartbeat is a
        side-effect on ``workers.last_heartbeat`` and does not show up
        in the stats counters; signal-driven releases are surfaced via
        ``WorkerStats.released_on_shutdown``.
    """
    if stop is None:
        stop = asyncio.Event()

    installed = _install_signal_handlers(stop) if install_signal_handlers else []

    async def _worker_then_stop() -> WorkerStats:
        """Run the inner loop; signal heartbeat to stop on any exit path.

        ``finally`` rather than a normal-path call so a crash inside the
        worker still releases the heartbeat — without it, an unexpected
        exception would leave the heartbeat task waiting on ``stop`` and
        the TaskGroup would hang indefinitely.
        """
        try:
            return await run_local_worker(
                repo,
                runner,
                plan,
                worker_id,
                idle_wait=idle_wait,
                max_iterations=max_iterations,
                stop=stop,
            )
        finally:
            stop.set()

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                run_heartbeat_loop(
                    repo,
                    worker_id,
                    interval=heartbeat_interval,
                    stop=stop,
                )
            )
            worker_task = tg.create_task(_worker_then_stop())
    finally:
        _remove_signal_handlers(installed)

    # ``worker_task`` is guaranteed done after the TaskGroup exits.
    # ``.result()`` re-raises if the inner loop crashed (TaskGroup would
    # have already surfaced the ExceptionGroup on its own, but the
    # explicit call documents the contract: this function returns the
    # inner stats or raises whatever the inner loop raised).
    return worker_task.result()


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "run_heartbeat_loop",
    "run_worker",
]
