"""Local worker composition root with heartbeat (TASK-019b1, PRD FR-1.6, NFR-1).

This module is one notch up the composition stack from
:mod:`whilly.worker.local`. ``local.run_local_worker`` is the bare claim →
start → run → complete loop; the present module pairs it with a parallel
heartbeat task under a single :class:`asyncio.TaskGroup` so the control
plane can distinguish a busy worker from a crashed one.

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
    plumbing. SIGTERM-driven shutdown (TASK-019b2) will set the same
    event from a signal handler.

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
from typing import Final

from whilly.adapters.db.repository import TaskRepository
from whilly.core.models import Plan, WorkerId
from whilly.worker.local import (
    DEFAULT_IDLE_WAIT,
    RunnerCallable,
    WorkerStats,
    run_local_worker,
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


async def run_worker(
    repo: TaskRepository,
    runner: RunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    idle_wait: float = DEFAULT_IDLE_WAIT,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    max_iterations: int | None = None,
) -> WorkerStats:
    """Run :func:`run_local_worker` paired with a parallel heartbeat task.

    Both coroutines run under one :class:`asyncio.TaskGroup`. When the
    main worker loop returns (production: cancelled by SIGTERM in
    TASK-019b2; tests: ``max_iterations`` reached), it sets a shared
    :class:`asyncio.Event`. The heartbeat coroutine wakes up, returns
    cleanly, and the TaskGroup exits. No explicit cancellation, no
    ``CancelledError`` plumbing — see module docstring.

    Parameters mirror :func:`whilly.worker.local.run_local_worker` plus
    one extra:

    Parameters
    ----------
    heartbeat_interval:
        Seconds between heartbeat ticks. Defaults to
        :data:`DEFAULT_HEARTBEAT_INTERVAL` (30s). Tests pass a small
        value so the heartbeat ticks observably during a short test run.

    Returns
    -------
    WorkerStats
        The :class:`whilly.worker.local.WorkerStats` returned by the
        inner loop — same fields, same semantics. The heartbeat is a
        side-effect on ``workers.last_heartbeat`` and does not show up
        in the stats counters.
    """
    stop = asyncio.Event()

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
            )
        finally:
            stop.set()

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
