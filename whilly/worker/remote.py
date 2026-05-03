"""Remote worker async-loop for Whilly v4.0 (TASK-022b1 / TASK-022b2 / TASK-022b3, PRD FR-1.1, FR-1.5, FR-1.6, NFR-1).

Counterpart to :mod:`whilly.worker.local`: same outer state-machine pattern
(``claim → run → complete | fail``), but every state-mutating step goes
through :class:`whilly.adapters.transport.client.RemoteWorkerClient` over
HTTP instead of touching :class:`whilly.adapters.db.repository.TaskRepository`
directly. The split keeps the worker process a thin httpx + pydantic +
:mod:`whilly.core` consumer (PRD FR-1.5) — there is no asyncpg / FastAPI
import path inside this module by design (PRD SC-6).

This module hosts two layered entry points:

* :func:`run_remote_worker` — the bare loop (TASK-022b1). One concern:
  ``claim → run → complete | fail`` over HTTP. ``stop`` is honoured at
  iteration boundaries and during the runner call so SIGTERM / SIGINT
  (TASK-022b3) returns the in-flight task to ``PENDING`` via
  :meth:`RemoteWorkerClient.release` instead of either failing it or
  waiting out the visibility-timeout sweep.
* :func:`run_remote_worker_with_heartbeat` — the composition root
  (TASK-022b2 / 022b3). Pairs the bare loop with a parallel heartbeat
  task under one :class:`asyncio.TaskGroup` *and* installs SIGTERM /
  SIGINT signal handlers that flip the shared shutdown event so the
  worker releases its in-flight task back to ``PENDING`` on a
  cooperative kill from a process supervisor (Kubernetes, systemd,
  tmux). Mirrors the local-worker ``run_worker`` from
  :mod:`whilly.worker.main`.

Loop contract (one iteration)
-----------------------------
1. ``client.claim(worker_id, plan.id)``. The server's long-poll budget
   (``CLAIM_LONG_POLL_TIMEOUT_DEFAULT`` = 30s) holds the connection open
   while the queue is empty. Two terminal outcomes:

   * ``Task`` — a row transitioned ``PENDING`` → ``CLAIMED`` server-side.
     The wire payload is already projected back to the domain
     :class:`Task` by :meth:`RemoteWorkerClient.claim`, so the rest of
     the loop speaks pure-domain types.
   * ``None`` — the long-poll budget expired. The AC pins the response:
     **re-poll immediately, no client-side sleep**. Adding a worker-side
     idle wait would double the budget on the server and burn worker
     capacity to no end; the supervisor's heartbeat (TASK-022b2) and
     signal handling (TASK-022b3) interleave between iterations, so the
     "tight re-poll" here is a feature, not a regression.

2. ``runner(task, prompt)``. Same callable shape as the local loop —
   :data:`RemoteRunnerCallable` is a structural alias matching
   :func:`whilly.adapters.runner.run_task`. The runner owns its own
   subprocess / retry policy; we just consume an :class:`AgentResult`.

3. Outcome routing:

   * ``is_complete=True`` and ``exit_code == 0`` →
     ``client.complete(task.id, worker_id, task.version)`` (server flips
     ``IN_PROGRESS`` → ``DONE``; see "protocol gap" note below).
   * Anything else → ``client.fail(task.id, worker_id, task.version,
     reason)``. The reason follows the local worker's shape so the
     dashboard / post-mortem queries can grep the same prefix
     (``exit_code=<n>: <truncated stdout>``).

Why no ``start`` step (and the protocol gap)
--------------------------------------------
The local worker calls :meth:`TaskRepository.start_task` between claim
and run to flip ``CLAIMED`` → ``IN_PROGRESS`` so the eventual
``complete_task`` SQL filter matches. The HTTP transport intentionally
does **not** expose ``/tasks/{id}/start`` today — TASK-022a2 / 022a3
shipped only the four worker RPCs the AC required (``register``,
``heartbeat``, ``claim``, ``complete``, ``fail``). Until a future task
adds a start endpoint (or relaxes the server's complete filter to accept
``CLAIMED`` too), a real run against the production server will surface
the gap as :class:`VersionConflictError` on ``complete`` with
``actual_status=CLAIMED``. We treat that the same as any other 409 here:
log and continue. The scope of TASK-022b1 is the loop *shape*, not the
wire-level start gap — the unit tests below use a stub client that
returns the post-update CompleteResponse so the loop logic is fully
exercised regardless of the gap.

Termination
-----------
Three exit paths:

* ``stop`` event set (graceful shutdown — TASK-022b3): if the event
  fires *between* iterations the loop exits cleanly without an
  in-flight task to release; if it fires *during* the runner call the
  runner is cancelled and the in-flight task is returned to
  ``PENDING`` via :meth:`RemoteWorkerClient.release` so a peer (or
  this worker on restart) can re-claim it within one poll cycle.
* ``max_iterations`` (test-only) hard cap on outer iterations.
* Outer :class:`asyncio.CancelledError` from the supervisor.

When ``stop`` is ``None`` (legacy callers, the heartbeat-composition
root in 022b2 before signals were wired) the loop runs without the
shutdown race — the original 022b1 codepath, untouched, so unit tests
that pre-date 022b3 still exercise the bare contract.

Concurrency note (PRD FR-2.4)
-----------------------------
Three writers can race for any task row even on the remote path: this
worker, a peer worker that claimed after a sweep release, and the
visibility-timeout sweep itself (TASK-025a). The optimistic-locking
contract surfaces every lost race as 409 :class:`VersionConflictError`,
which we log and skip. Abandoning the row is the safe move because by
the time the conflict surfaces another writer (sweep or peer) has
already taken responsibility for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.client import HTTPClientError, RemoteWorkerClient, VersionConflictError
from whilly.core.models import Plan, Task, TaskStatus, WorkerId
from whilly.core.prompts import build_task_prompt

log = logging.getLogger(__name__)

# 30s aligns with the PRD's heartbeat cadence (FR-1.6) and matches the
# local worker's :data:`whilly.worker.main.DEFAULT_HEARTBEAT_INTERVAL` so
# the visibility-timeout sweep (TASK-025a) can use one cadence to reason
# about staleness regardless of worker flavour. Tests pin this constant
# (a future tweak forces a docs review) and pass a tiny value when
# exercising the loop body.
DEFAULT_HEARTBEAT_INTERVAL: Final[float] = 30.0

# Cap on the AgentResult.output snippet stored in the FAIL event payload.
# Mirrors :data:`whilly.worker.local._FAIL_REASON_OUTPUT_CAP` — keeping the
# two values in lock-step means a dashboard query that filters fail-reason
# prefixes works the same for local and remote tasks. Worker logs already
# carry the full stdout; the audit row only needs the failure mode.
_FAIL_REASON_OUTPUT_CAP: Final[int] = 500

# Type alias for the runner side of the loop. Identical to
# :data:`whilly.worker.local.RunnerCallable` — both flavours of worker
# accept the same ``(task, prompt) -> AgentResult`` shape, so production
# callers can pass :func:`whilly.adapters.runner.run_task` to either.
RemoteRunnerCallable = Callable[[Task, str], Awaitable[AgentResult]]

# Reason string written into the RELEASE event payload when the worker
# releases an in-flight task because of SIGTERM / SIGINT (TASK-022b3).
# Mirrors :data:`whilly.worker.local.SHUTDOWN_RELEASE_REASON` so a single
# dashboard query can attribute releases regardless of worker flavour —
# both local and remote workers emit ``payload.reason = "shutdown"`` for
# the cooperative-shutdown path, distinct from the visibility-timeout
# sweep's ``"visibility_timeout"``.
SHUTDOWN_RELEASE_REASON: Final[str] = "shutdown"

# Signals we install handlers for in :func:`run_remote_worker_with_heartbeat`.
# Same set as the local worker's :data:`whilly.worker.main._SHUTDOWN_SIGNALS`
# — SIGTERM is the standard process-supervisor shutdown signal (Kubernetes,
# systemd, tmux kill-window); SIGINT is the same path for ``Ctrl-C`` from
# an interactive shell. Both must end with the in-flight task released to
# ``PENDING`` on the server so a peer can re-claim it within one poll cycle.
_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = (
    signal.SIGTERM,
    signal.SIGINT,
)


@dataclass(frozen=True)
class RemoteWorkerStats:
    """Counters returned by one :func:`run_remote_worker` invocation.

    Frozen so tests can assert on it without defensive copying. Fields
    mirror :class:`whilly.worker.local.WorkerStats` exactly so dashboards
    and per-flavour tests can share assertion helpers without branching
    on worker flavour.

    Attributes
    ----------
    iterations:
        Outer-loop iterations executed — includes idle polls (204 from
        the server) so a worker against an empty plan still sees the
        count grow.
    completed:
        Tasks the server flipped to ``DONE`` via ``POST /tasks/{id}/complete``
        on this invocation.
    failed:
        Tasks the server flipped to ``FAILED`` via ``POST /tasks/{id}/fail``.
    idle_polls:
        Iterations where ``client.claim`` returned ``None`` (server-side
        long-poll budget expired). The next iteration re-polls
        immediately — see module docstring for why no client-side sleep.
    released_on_shutdown:
        Tasks the worker put back to ``PENDING`` via
        :meth:`RemoteWorkerClient.release` because a shutdown signal
        arrived mid-runner (TASK-022b3). Always 0 in legacy non-stop
        runs; at most 1 per :func:`run_remote_worker` invocation since
        the loop ``break``s after releasing.
    """

    iterations: int = 0
    completed: int = 0
    failed: int = 0
    idle_polls: int = 0
    released_on_shutdown: int = 0


def _truncate_output(output: str) -> str:
    """Trim agent ``output`` for the FAIL event payload.

    Symmetric with :func:`whilly.worker.local._truncate_output`. The two
    helpers don't share an import because the local module's ``_``-prefixed
    names are private; duplicating six lines is cheaper than promoting a
    public helper that adds zero value to non-worker callers.
    """
    if len(output) <= _FAIL_REASON_OUTPUT_CAP:
        return output
    return output[:_FAIL_REASON_OUTPUT_CAP] + "…"


def _build_fail_reason(result: AgentResult) -> str:
    """Render an :class:`AgentResult` into a human-readable FAIL reason.

    Format matches :func:`whilly.worker.local._build_fail_reason` — both
    workers ship ``exit_code=<n>: <truncated stdout>`` so the dashboard
    aggregates them under the same prefix without having to branch on
    worker flavour.
    """
    snippet = _truncate_output(result.output).strip()
    if snippet:
        return f"exit_code={result.exit_code}: {snippet}"
    return f"exit_code={result.exit_code}"


async def _await_runner_or_stop(
    runner_coro: Awaitable[AgentResult],
    stop: asyncio.Event,
) -> tuple[AgentResult | None, bool]:
    """Race ``runner_coro`` against ``stop``; return ``(result, shutdown_requested)``.

    Mirror of :func:`whilly.worker.local._await_runner_or_stop` — the
    two helpers don't share an import because the local module's
    ``_``-prefixed names are private and the helper is small enough to
    duplicate without inviting drift. Returns ``(result, False)`` when
    the runner finishes normally and ``(None, True)`` when ``stop``
    fires first.

    On the shutdown path the runner task is cancelled and awaited so
    its underlying subprocess / sockets get a chance to tear down
    before we report shutdown — without this, an in-flight httpx /
    asyncpg connection inside the runner could leak past the test
    boundary. We swallow the runner's :class:`asyncio.CancelledError`
    (and any other exception it surfaces during teardown) rather than
    re-raising: the point of the shutdown path is to release the task
    atomically; a cancellation-time error from the runner is not a
    worker bug, just the natural consequence of yanking the agent
    mid-call.
    """
    runner_task: asyncio.Task[AgentResult] = asyncio.ensure_future(runner_coro)
    stop_task: asyncio.Task[bool] = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait(
            {runner_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        runner_task.cancel()
        stop_task.cancel()
        raise

    if stop_task in done:
        runner_task.cancel()
        try:
            await runner_task
        except (asyncio.CancelledError, Exception) as exc:
            log.debug("remote runner cancelled during shutdown: %r", exc)
        return None, True

    stop_task.cancel()
    try:
        await stop_task
    except asyncio.CancelledError:
        pass
    return runner_task.result(), False


async def run_remote_worker(
    client: RemoteWorkerClient,
    runner: RemoteRunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    max_iterations: int | None = None,
    max_processed: int | None = None,
    stop: asyncio.Event | None = None,
) -> RemoteWorkerStats:
    """Run the remote worker loop against ``plan.id`` for ``worker_id``.

    See module docstring for the per-iteration contract. The loop exits
    when ``stop`` is set (graceful shutdown — TASK-022b3),
    ``max_iterations`` is reached (test-only), ``max_processed`` is
    reached (the ``--once`` CLI flag from TASK-022c), or when the outer
    task is cancelled (production fallback).

    Parameters
    ----------
    client:
        Open :class:`RemoteWorkerClient` — the caller is responsible for
        the surrounding ``async with`` block. Reusing one client across
        iterations is the documented hot path: keep-alive lets the
        long-polled ``claim`` reuse a warm TCP connection on the next
        iteration.
    runner:
        Coroutine ``(task, prompt) -> AgentResult``. Same shape as the
        local worker; production callers pass
        :func:`whilly.adapters.runner.run_task`, tests pass an async
        closure.
    plan:
        Plan whose tasks the worker draws from. Only ``plan.id`` hits
        the wire; the full plan is forwarded to
        :func:`whilly.core.prompts.build_task_prompt` for the agent
        prompt context — same projection as the local worker.
    worker_id:
        Identity returned by :meth:`RemoteWorkerClient.register` on a
        previous run (or registered out-of-band). Echoed in every claim
        / complete / fail body for defence-in-depth and audit-log
        correlation.
    max_iterations:
        Hard cap on outer iterations. ``None`` (production default)
        means loop forever — exit only on cancellation or ``stop``.
        Tests pass an integer to make the loop terminable without
        wiring a stop event through the bare loop signature.
    max_processed:
        Hard cap on the number of tasks successfully passed to either
        ``client.complete`` or ``client.fail`` during this invocation.
        ``None`` (production default) means uncapped — completed and
        failed tasks accrue until ``stop`` / ``max_iterations`` /
        cancellation. ``1`` is the ``--once`` CLI flag's wire (TASK-022c):
        the loop runs until it processes (completes or fails) one
        task, then exits cleanly without releasing anything. Idle
        polls (204 from the server) and 409 lost-race iterations do
        **not** count as processed — they don't change the row's
        terminal status, so a ``--once`` worker fights through an
        empty queue or version skew until it actually owns a task.
    stop:
        Optional :class:`asyncio.Event` for cooperative graceful shutdown
        (TASK-022b3). When set, the loop releases any in-flight task
        back to ``PENDING`` via :meth:`RemoteWorkerClient.release` and
        exits. Wired up by :func:`run_remote_worker_with_heartbeat` to
        SIGTERM / SIGINT in production; tests set it directly to
        exercise the shutdown path without sending real signals.

    Returns
    -------
    RemoteWorkerStats
        Counters covering iterations, completions, failures, idle polls
        and shutdown releases observed during this invocation.
    """
    iterations = 0
    completed = 0
    failed = 0
    idle_polls = 0
    released_on_shutdown = 0

    while max_iterations is None or iterations < max_iterations:
        # Check ``stop`` *before* incrementing ``iterations`` so a
        # shutdown at the boundary doesn't inflate the iteration count
        # for tests. Mirrors :func:`whilly.worker.local.run_local_worker`.
        if stop is not None and stop.is_set():
            log.info(
                "remote worker=%s plan=%s: shutdown requested, exiting loop cleanly",
                worker_id,
                plan.id,
            )
            break

        iterations += 1

        claimed = await client.claim(worker_id, plan.id)
        if claimed is None:
            # 204 No Content: the server's long-poll already absorbed the
            # idle-wait budget. Re-poll immediately — adding a sleep here
            # would double the wait on every empty-queue iteration. The
            # stop check at the top of the next iteration is the
            # shutdown wake-up: a SIGTERM during an idle long-poll is
            # bounded by the server-side ``CLAIM_LONG_POLL_TIMEOUT_DEFAULT``
            # (30s), and the next iteration will see ``stop.is_set()``
            # and exit before re-polling.
            idle_polls += 1
            log.debug(
                "remote worker=%s plan=%s: 204 (no PENDING tasks), re-polling immediately",
                worker_id,
                plan.id,
            )
            continue

        prompt = build_task_prompt(claimed, plan)

        # Race the runner against ``stop`` so SIGTERM mid-runner doesn't
        # have to wait for an arbitrarily long agent call before we can
        # release the task. When ``stop`` is None (legacy callers, the
        # 022b1 contract before signals were wired) we just await the
        # runner directly — the original codepath, untouched.
        if stop is None:
            result: AgentResult | None = await runner(claimed, prompt)
            shutdown_during_run = False
        else:
            result, shutdown_during_run = await _await_runner_or_stop(runner(claimed, prompt), stop)

        if shutdown_during_run:
            log.info(
                "remote worker=%s task=%s: shutdown mid-runner, releasing claim",
                worker_id,
                claimed.id,
            )
            try:
                await client.release(
                    claimed.id,
                    worker_id,
                    claimed.version,
                    SHUTDOWN_RELEASE_REASON,
                )
                released_on_shutdown += 1
            except VersionConflictError as exc:
                # 409 on release: the visibility-timeout sweep beat us
                # to it (status already PENDING) or — extremely narrow
                # window — the worker actually finished the task
                # between the stop event firing and this RPC reaching
                # the server. Either way, the row is in a terminal-or-
                # better state without our help; treat as success and
                # exit cleanly. ``actual_status == PENDING`` is the
                # canonical "already released" signal.
                if exc.actual_status == TaskStatus.PENDING:
                    log.warning(
                        "remote release lost the race: task=%s expected_version=%d actual_version=%s — already PENDING",
                        claimed.id,
                        claimed.version,
                        exc.actual_version,
                    )
                else:
                    log.warning(
                        "remote release lost the race: task=%s expected_version=%d actual_version=%s actual_status=%s",
                        claimed.id,
                        claimed.version,
                        exc.actual_version,
                        exc.actual_status.value if exc.actual_status else None,
                    )
            except HTTPClientError as exc:
                # Auth / generic 4xx during shutdown release is best-
                # effort: the visibility-timeout sweep is the safety net
                # if we can't reach the server right now. Log loudly so
                # operators see the dropped release in the journal, but
                # don't raise — the supervisor is already unwinding.
                log.warning(
                    "remote release HTTP error during shutdown: task=%s worker=%s exc=%s",
                    claimed.id,
                    worker_id,
                    exc,
                )
            break

        # ``shutdown_during_run`` was False, so ``result`` is set.
        assert result is not None  # for mypy; helper contract guarantees this

        if result.is_complete and result.exit_code == 0:
            try:
                # Forward the agent's parsed ``cost_usd`` so the server-
                # side ``complete_task`` can update ``plans.spent_usd``
                # atomically with the task transition (TASK-102, PRD
                # FR-2.4 / VAL-BUDGET-030). ``None`` / 0.0 short-circuits
                # the spend update on the server side (VAL-BUDGET-032).
                await client.complete(
                    claimed.id,
                    worker_id,
                    claimed.version,
                    cost_usd=result.usage.cost_usd,
                )
            except VersionConflictError as exc:
                # 409 on complete: lost race (peer / sweep grabbed the
                # row), or the protocol gap surfacing as
                # ``actual_status == CLAIMED`` until a future task adds
                # the start endpoint. Either way: another writer or a
                # missing primitive owns the resolution; abandon and
                # re-poll.
                log.warning(
                    "remote complete lost the race: task=%s expected_version=%d actual_version=%s actual_status=%s",
                    claimed.id,
                    claimed.version,
                    exc.actual_version,
                    exc.actual_status.value if exc.actual_status else None,
                )
                continue
            completed += 1
            log.info("remote worker=%s task=%s → DONE", worker_id, claimed.id)
        else:
            reason = _build_fail_reason(result)
            try:
                await client.fail(claimed.id, worker_id, claimed.version, reason)
            except VersionConflictError as exc:
                # 409 on fail mirrors the complete branch — server SQL
                # accepts both ``CLAIMED`` and ``IN_PROGRESS`` source
                # states, so a 409 here always means another writer
                # already advanced the row past us.
                log.warning(
                    "remote fail lost the race: task=%s expected_version=%d actual_version=%s actual_status=%s",
                    claimed.id,
                    claimed.version,
                    exc.actual_version,
                    exc.actual_status.value if exc.actual_status else None,
                )
                continue
            failed += 1
            log.info("remote worker=%s task=%s → FAILED (%s)", worker_id, claimed.id, reason)

        # ``max_processed`` honours --once (TASK-022c): exit cleanly after
        # the first task whose terminal status was successfully written
        # to the server. Lost-race 409s ``continue`` above before this
        # check, so they never satisfy the cap — a --once worker keeps
        # trying until it owns a real outcome.
        if max_processed is not None and (completed + failed) >= max_processed:
            log.info(
                "remote worker=%s plan=%s: reached max_processed=%d, exiting loop cleanly",
                worker_id,
                plan.id,
                max_processed,
            )
            break

    return RemoteWorkerStats(
        iterations=iterations,
        completed=completed,
        failed=failed,
        idle_polls=idle_polls,
        released_on_shutdown=released_on_shutdown,
    )


# --------------------------------------------------------------------------- #
# Heartbeat composition (TASK-022b2, PRD FR-1.5, FR-1.6, NFR-1)
# --------------------------------------------------------------------------- #


async def run_remote_heartbeat_loop(
    client: RemoteWorkerClient,
    worker_id: WorkerId,
    *,
    interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    stop: asyncio.Event,
) -> None:
    """Refresh ``workers.last_heartbeat`` over HTTP every ``interval`` seconds.

    Mirror of :func:`whilly.worker.main.run_heartbeat_loop` for the
    remote-worker side of the house. Two structural differences from the
    local-worker heartbeat:

    * The state-mutating call goes through
      :meth:`RemoteWorkerClient.heartbeat` (POST
      ``/workers/{id}/heartbeat``), not a direct
      :meth:`TaskRepository.update_heartbeat` SQL update. The server is
      what actually advances the column; from the worker's perspective
      heartbeat is just an RPC.
    * The set of "expected" failure modes is broader — every
      :class:`whilly.adapters.transport.client.HTTPClientError` subclass
      (auth, version-conflict-which-cannot-happen-here, server) plus raw
      :class:`OSError` / :class:`asyncio.TimeoutError` from httpx all
      count as transient and get logged + retried on the next tick.

    The first tick fires immediately on entry so a freshly-started
    worker shows a fresh ``last_heartbeat`` from the moment its main
    loop begins polling — same rationale as the local-worker heartbeat:
    no one-interval gap where the visibility-timeout sweep could mistake
    a brand-new worker for a stale one.

    Subsequent waits use :func:`asyncio.wait_for` against ``stop.wait()``
    so a graceful shutdown (TASK-022b3 SIGTERM, ``max_iterations``
    reached, outer cancellation propagated through ``_worker_then_stop``)
    wakes up the loop without waiting out the full interval.
    :class:`TimeoutError` is the "interval elapsed, tick again" path; a
    normal return from :func:`asyncio.wait_for` means ``stop`` fired and
    we exit cleanly — no :class:`asyncio.CancelledError` plumbing needed
    for the TaskGroup to unwind.

    Failure isolation
    -----------------
    Heartbeat is **best-effort liveness** (PRD FR-1.6). A failed tick
    must not kill the worker — the visibility-timeout sweep (TASK-025a)
    will eventually reclaim an in-flight task whose worker stopped
    heartbeating, and that's a recoverable problem; crashing the worker
    over a transient server hiccup trades it for an unrecoverable one.

    The except clause catches :class:`Exception` (so every concrete
    httpx / typed error flows through), but **not**
    :class:`asyncio.CancelledError` — that inherits from
    :class:`BaseException`, so structured cancellation still works if
    the supervisor decides to cancel the task explicitly. We also
    surface a recoverable ``ok=False`` from the server (worker row
    missing — the supervisor's job to re-register, see
    :class:`whilly.adapters.transport.schemas.HeartbeatResponse`) at
    INFO so an operator can spot misconfigured ``WHILLY_WORKER_TOKEN`` /
    revoked rows in the journal.

    Parameters
    ----------
    client:
        Open :class:`RemoteWorkerClient` — the caller (the supervisor)
        owns the surrounding ``async with`` block. Reusing one client
        for both the main loop's claim/complete RPCs and the heartbeat
        is intentional: httpx's pooled :class:`httpx.AsyncClient`
        multiplexes the requests over the keep-alive connection.
    worker_id:
        Identifier returned by :meth:`RemoteWorkerClient.register` on
        a previous run. Sent in the heartbeat body for the server's
        per-row UPDATE filter.
    interval:
        Seconds between heartbeat ticks. Defaults to
        :data:`DEFAULT_HEARTBEAT_INTERVAL` (30s, PRD FR-1.6). Tests pass
        a tiny value so the heartbeat ticks observably during a
        millisecond-scoped run.
    stop:
        Shared shutdown event. Set by the supervisor when the main loop
        exits (``finally: stop.set()`` inside ``_worker_then_stop``) so
        the heartbeat returns cleanly without external cancellation.
    """
    while not stop.is_set():
        try:
            response = await client.heartbeat(worker_id)
        except HTTPClientError as exc:
            # Auth / server / generic 4xx: log at WARNING because the
            # operator may need to act (rotated bootstrap, revoked
            # bearer). We still don't crash — the next tick has a
            # chance, and visibility-timeout sweep is the safety net.
            log.warning(
                "remote heartbeat worker=%s failed (%s); will retry next tick",
                worker_id,
                exc,
            )
        except Exception as exc:
            # Catch-all for httpx-level / network failures that didn't
            # map to a typed HTTPClientError (rare paths like
            # asyncio.TimeoutError on a custom transport, or a logic
            # bug in _request that we'd rather log than crash on).
            # CancelledError bypasses this clause (BaseException), so
            # structured cancellation still works.
            log.warning(
                "remote heartbeat worker=%s unexpected error (%s); will retry next tick",
                worker_id,
                exc,
            )
        else:
            # ``ok=False`` is the recoverable "worker_id not registered
            # on the server" branch documented on
            # :class:`HeartbeatResponse`. We log it at INFO rather than
            # WARNING because the supervisor (TASK-022b3 / future re-
            # register flow) is the one expected to act — log noise
            # at WARNING for every tick of a misconfigured worker would
            # drown the journal.
            if not response.ok:
                log.info(
                    "remote heartbeat worker=%s server reports unknown worker_id; supervisor must re-register",
                    worker_id,
                )

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            # ``stop`` fired — return immediately, no further tick.
            return
        except TimeoutError:
            # Interval elapsed, no shutdown request — loop and tick again.
            continue


def _install_signal_handlers(stop: asyncio.Event) -> list[signal.Signals]:
    """Install SIGTERM / SIGINT handlers that flip ``stop`` (TASK-022b3).

    Mirror of :func:`whilly.worker.main._install_signal_handlers` for the
    remote-worker side of the house. Returns the list of signals whose
    handlers were actually installed — the caller passes that list back
    to :func:`_remove_signal_handlers` on exit so we don't leak handlers
    across sequential :func:`run_remote_worker_with_heartbeat` invocations
    (or between tests).

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
                "remote worker signal handler for %s not installable on this loop; skipping",
                sig.name,
            )
            continue
        installed.append(sig)
    if installed:
        log.info(
            "remote worker signal handlers installed for %s",
            ", ".join(s.name for s in installed),
        )
    return installed


def _remove_signal_handlers(installed: list[signal.Signals]) -> None:
    """Restore default signal disposition for handlers we installed.

    Symmetric with :func:`_install_signal_handlers`. Errors during
    teardown are logged at DEBUG but never raised — a failed cleanup
    must not mask whatever exception the caller is unwinding through.
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
            log.debug("could not remove remote-worker signal handler for %s: %r", sig.name, exc)


def _make_shutdown_handler(stop: asyncio.Event, sig: signal.Signals) -> Callable[[], None]:
    """Build a thread-safe handler that flips ``stop`` and logs the signal.

    The handler runs on the asyncio loop (because we registered it via
    ``loop.add_signal_handler``), so calling :meth:`asyncio.Event.set`
    from inside is safe — no cross-thread synchronization needed. We
    keep the body trivial: flipping the event is sufficient, and the
    rest of the shutdown logic lives in :func:`run_remote_worker` where
    the state-transition context is.

    Logging at INFO so a SIGTERM kill from kubectl / systemd / tmux
    leaves an unambiguous breadcrumb in the worker journal — operators
    investigating "why did this remote worker exit" can correlate with
    the process supervisor's log without reading code.
    """

    def _handler() -> None:
        log.info("remote worker received %s; requesting graceful shutdown", sig.name)
        stop.set()

    return _handler


async def run_remote_worker_with_heartbeat(
    client: RemoteWorkerClient,
    runner: RemoteRunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    max_iterations: int | None = None,
    max_processed: int | None = None,
    install_signal_handlers: bool = True,
    stop: asyncio.Event | None = None,
) -> RemoteWorkerStats:
    """Run :func:`run_remote_worker` paired with :func:`run_remote_heartbeat_loop`.

    Composition root for the remote worker — the layered counterpart to
    :func:`whilly.worker.main.run_worker` on the local side. Both
    coroutines run under one :class:`asyncio.TaskGroup`; when the main
    worker loop returns (``max_iterations`` reached in tests, outer
    cancellation propagated, or a SIGTERM / SIGINT -flipped ``stop``
    event in production) the inner closure's ``finally`` block sets the
    shared :class:`asyncio.Event`, the heartbeat coroutine wakes from
    its ``wait_for(stop, interval)`` and returns cleanly, and the
    TaskGroup unwinds without :class:`asyncio.CancelledError` plumbing.

    Signal handling (TASK-022b3)
    ----------------------------
    When ``install_signal_handlers=True`` (production default),
    :func:`run_remote_worker_with_heartbeat` installs handlers for
    SIGTERM and SIGINT via :meth:`asyncio.AbstractEventLoop.add_signal_handler`.
    Both flip the same ``stop`` event the heartbeat / loop already use.
    The inner :func:`run_remote_worker` races each runner call against
    ``stop`` and, on a mid-runner shutdown, calls
    :meth:`RemoteWorkerClient.release` to put the in-flight task back to
    ``PENDING`` before exiting. Net effect: a peer worker (or this
    worker on restart) re-claims it within one poll cycle, no work is
    lost, and the audit log carries a ``RELEASE`` event with
    ``payload.reason = "shutdown"`` so post-mortems can tell signal-
    driven releases apart from visibility-timeout sweeps.

    Handlers are installed only on the main thread of an asyncio loop
    that supports them — Windows' ``ProactorEventLoop`` and any non-
    main thread raise :class:`NotImplementedError` from
    ``add_signal_handler``. We catch that and silently degrade: callers
    in those environments still get heartbeats and can shut down via
    ``max_iterations`` / outer cancellation. The
    ``install_signal_handlers=False`` parameter is the test-side toggle
    — pytest's own SIGINT handler must not be replaced by the worker's
    during unit tests.

    Why a stop event rather than ``heartbeat_task.cancel()``?
        Same rationale as :mod:`whilly.worker.main`: explicit
        cancellation surfaces a :class:`asyncio.CancelledError` from
        the cancelled task that :class:`asyncio.TaskGroup` treats as a
        propagatable error. Using a stop event lets the heartbeat exit
        *normally* — the TaskGroup just awaits both children, sees
        clean returns, and drops out. The signal handlers set the same
        event from inside the asyncio loop via
        ``loop.add_signal_handler``, so a ``kill -TERM`` arrives as
        ordinary cooperative shutdown.

    Parameters
    ----------
    client:
        Open :class:`RemoteWorkerClient`. The caller owns the
        surrounding ``async with`` block — heartbeat and main loop
        share one pooled connection, so the supervisor outlives both.
    runner:
        Coroutine ``(task, prompt) -> AgentResult``. Forwarded to
        :func:`run_remote_worker` unchanged — this layer doesn't touch
        the per-iteration contract.
    plan:
        The plan whose tasks the worker draws from. Forwarded.
    worker_id:
        Registered worker identity. Used by both the main loop
        (claim/complete/fail/release bodies) and the heartbeat task.
    heartbeat_interval:
        Seconds between heartbeat ticks. Defaults to
        :data:`DEFAULT_HEARTBEAT_INTERVAL` (30s).
    max_iterations:
        Hard cap on outer iterations of the main loop. ``None`` means
        loop until cancellation or shutdown. Tests pass an integer to
        make the composition terminable without wiring a cancellation
        token through the bare loop's signature.
    max_processed:
        Hard cap on completed + failed tasks. ``None`` means uncapped;
        ``1`` is the wire for the ``whilly-worker --once`` CLI flag
        (TASK-022c). Forwarded to :func:`run_remote_worker` unchanged
        — the bare loop owns the bookkeeping and the exit condition.
    install_signal_handlers:
        When ``True`` (production default), SIGTERM and SIGINT are
        installed via :meth:`asyncio.AbstractEventLoop.add_signal_handler`
        and flip the shared ``stop`` event. Tests pass ``False`` to
        avoid clobbering pytest's own SIGINT handler (and to keep unit
        tests deterministic across loop implementations).
    stop:
        Optional shared shutdown event. Callers that already own a
        shutdown signal (CLI in TASK-022c, integration tests that
        drive shutdown without sending real signals) pass it in;
        otherwise we allocate one internally. Either way the same
        event drives the heartbeat termination, the main-loop
        release-on-shutdown path, and the signal handlers.

    Returns
    -------
    RemoteWorkerStats
        Counters returned by the inner :func:`run_remote_worker`. The
        heartbeat is a side-effect on the server (and on
        ``workers.last_heartbeat`` once it lands in Postgres) and
        does not contribute to any counter; signal-driven releases
        surface via ``RemoteWorkerStats.released_on_shutdown`` — same
        convention as the local-worker composition.
    """
    if stop is None:
        stop = asyncio.Event()

    installed = _install_signal_handlers(stop) if install_signal_handlers else []

    async def _worker_then_stop() -> RemoteWorkerStats:
        """Run the inner loop; signal heartbeat to stop on any exit path.

        ``finally`` rather than a normal-path ``stop.set()`` so a crash
        inside the worker still releases the heartbeat — without it,
        an unexpected exception would leave the heartbeat task waiting
        on ``stop`` and the TaskGroup would hang indefinitely.
        """
        try:
            return await run_remote_worker(
                client,
                runner,
                plan,
                worker_id,
                max_iterations=max_iterations,
                max_processed=max_processed,
                stop=stop,
            )
        finally:
            stop.set()

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                run_remote_heartbeat_loop(
                    client,
                    worker_id,
                    interval=heartbeat_interval,
                    stop=stop,
                )
            )
            worker_task = tg.create_task(_worker_then_stop())
    finally:
        _remove_signal_handlers(installed)

    # ``worker_task`` is guaranteed done after the TaskGroup exits.
    # ``.result()`` re-raises whatever the inner loop raised (TaskGroup
    # would already have surfaced an ExceptionGroup, but the explicit
    # call documents the contract: this function returns the inner
    # stats or raises whatever the inner loop raised).
    return worker_task.result()


# --------------------------------------------------------------------------- #
# URL-rotation supervisor (M2: m2-worker-url-refresh-on-rotation)
# --------------------------------------------------------------------------- #

# Honoured env-var name for the M2 funnel-URL discovery mode. Re-exported
# from :mod:`whilly.worker.funnel` so callers do not have to know which
# submodule owns the constant.
WORKER_RECONNECT_URL_REASON: Final[str] = "url_rotation"


@dataclass(frozen=True)
class RotationStats:
    """Aggregate counters returned by :func:`run_remote_worker_with_url_rotation`.

    ``inner_runs`` is the number of distinct ``RemoteWorkerClient``
    sessions opened (one per observed control URL). ``url_rotations``
    is the number of times the discovery source returned a *new* URL
    while the worker was already connected — strictly less than or
    equal to ``inner_runs``. ``stats`` aggregates the per-session
    :class:`RemoteWorkerStats` so dashboards can sum them without
    having to know the rotation count.
    """

    inner_runs: int = 0
    url_rotations: int = 0
    stats: RemoteWorkerStats = RemoteWorkerStats()


# Type alias for the factory the rotation supervisor uses to mint a
# fresh transport client per session. Production callers pass a small
# closure around :class:`RemoteWorkerClient`; tests inject a stub that
# yields an in-memory fake. Returning an async-context-manager keeps
# the supervisor blind to the underlying implementation.
RemoteClientFactory = Callable[[str], contextlib.AbstractAsyncContextManager[RemoteWorkerClient]]


async def _watch_url_changes(
    source: "FunnelUrlSource",
    current_url: str,
    *,
    on_change: Callable[[str], None],
    stop: asyncio.Event,
) -> None:
    """Poll ``source`` until it reports a different URL or ``stop`` fires.

    Any non-``None`` value that does not equal the seed ``current_url``
    is treated as a rotation: the watcher invokes ``on_change`` with
    the new value and returns. Transient ``None`` returns are
    swallowed — the rotation loop owns the "URL temporarily missing"
    semantics, not the watcher.

    The wait is racing the ``source.poll_interval`` against ``stop``
    via :func:`asyncio.wait_for` so a SIGTERM-driven shutdown wakes
    the watcher immediately rather than waiting out a 30 s tick.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=source.poll_interval)
            return
        except TimeoutError:
            pass
        try:
            latest = await source.fetch()
        except Exception as exc:
            log.warning("funnel URL source raised during fetch (%s); will retry", exc)
            continue
        if latest is None or latest == current_url:
            continue
        log.info(
            "funnel URL rotation detected: %s -> %s",
            current_url,
            latest,
        )
        on_change(latest)
        return


async def run_remote_worker_with_url_rotation(
    client_factory: RemoteClientFactory,
    runner: RemoteRunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    initial_url: str,
    source: "FunnelUrlSource",
    *,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    max_iterations: int | None = None,
    max_processed: int | None = None,
    install_signal_handlers: bool = True,
    stop: asyncio.Event | None = None,
) -> RotationStats:
    """Run :func:`run_remote_worker_with_heartbeat` across funnel-URL rotations.

    Wraps the steady-state composition root with an outer reconnect
    loop. While the inner worker is running, a sibling task polls
    ``source`` for URL changes; when one is observed it sets the
    inner loop's stop event so the worker releases any in-flight task
    via :meth:`RemoteWorkerClient.release` and unwinds cleanly. The
    outer loop then closes the previous client, opens a new one
    against the rotated URL, and resumes — preserving the same
    ``worker_id`` and bearer the caller provided so the control
    plane sees the same identity reconnecting (no duplicate-worker
    error, no orphaned task).

    The outer ``stop`` event (typically wired to SIGTERM via the
    inner :func:`run_remote_worker_with_heartbeat`) terminates the
    rotation loop; passing it to the inner call keeps shutdown
    semantics unified.

    Returns
    -------
    RotationStats
        Summary of how many sessions ran, how many rotations occurred,
        and the aggregated per-session worker stats.
    """
    if stop is None:
        stop = asyncio.Event()

    current_url = initial_url
    inner_runs = 0
    rotations = 0
    aggregate = RemoteWorkerStats()

    while not stop.is_set():
        rotated_url: list[str] = []

        def _on_change(new_url: str, _bucket: list[str] = rotated_url) -> None:
            _bucket.append(new_url)

        # The inner stop event is shared with the heartbeat composition
        # so the in-flight task is released via the existing shutdown
        # path. Either (a) URL rotation, (b) outer stop, or (c) the
        # worker reaching ``max_iterations`` / ``max_processed`` flips
        # it, after which the watcher and outer-stop bridge both wake
        # up and exit cleanly.
        inner_stop = asyncio.Event()

        async def _run_inner() -> RemoteWorkerStats:
            try:
                async with client_factory(current_url) as client:
                    return await run_remote_worker_with_heartbeat(
                        client,
                        runner,
                        plan,
                        worker_id,
                        heartbeat_interval=heartbeat_interval,
                        max_iterations=max_iterations,
                        max_processed=max_processed,
                        install_signal_handlers=install_signal_handlers,
                        stop=inner_stop,
                    )
            finally:
                inner_stop.set()

        async def _run_watcher() -> None:
            await _watch_url_changes(
                source,
                current_url,
                on_change=_on_change,
                stop=inner_stop,
            )
            if rotated_url:
                inner_stop.set()

        # Forward outer SIGTERM to the inner stop event AND wake on
        # ``inner_stop`` so we don't hang past worker exit.
        async def _bridge_outer_stop() -> None:
            assert stop is not None
            outer_done = asyncio.create_task(stop.wait())
            inner_done = asyncio.create_task(inner_stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {outer_done, inner_done},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                outer_done.cancel()
                inner_done.cancel()
            if outer_done in done:
                inner_stop.set()

        inner_runs += 1
        log.info("remote worker session %d starting against %s", inner_runs, current_url)
        async with asyncio.TaskGroup() as tg:
            worker_task = tg.create_task(_run_inner(), name="whilly-remote-worker")
            tg.create_task(_run_watcher(), name="whilly-funnel-url-watcher")
            tg.create_task(_bridge_outer_stop(), name="whilly-outer-stop-bridge")

        session_stats = worker_task.result()
        aggregate = RemoteWorkerStats(
            iterations=aggregate.iterations + session_stats.iterations,
            completed=aggregate.completed + session_stats.completed,
            failed=aggregate.failed + session_stats.failed,
            idle_polls=aggregate.idle_polls + session_stats.idle_polls,
            released_on_shutdown=(aggregate.released_on_shutdown + session_stats.released_on_shutdown),
        )

        if stop.is_set():
            log.info("remote worker outer stop fired; exiting rotation loop")
            break
        if not rotated_url:
            # Inner exited for non-rotation reasons (max_iterations,
            # max_processed, or unexpected). Don't reconnect — caller
            # asked for a bounded run.
            log.info("remote worker inner loop exited without URL rotation")
            break

        rotations += 1
        current_url = rotated_url[-1]
        log.info(
            "remote worker reconnecting against rotated URL %s (rotation #%d)",
            current_url,
            rotations,
        )

    return RotationStats(
        inner_runs=inner_runs,
        url_rotations=rotations,
        stats=aggregate,
    )


# Late import keeps the static graph clean: ``whilly.worker.funnel``
# only depends on stdlib + ``importlib``, but stating the dependency
# at the top of the file would couple the typing imports to the
# module-load order test harnesses care about.
from whilly.worker.funnel import FunnelUrlSource  # noqa: E402  pylint: disable=wrong-import-position


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "SHUTDOWN_RELEASE_REASON",
    "WORKER_RECONNECT_URL_REASON",
    "RemoteClientFactory",
    "RemoteRunnerCallable",
    "RemoteWorkerStats",
    "RotationStats",
    "run_remote_heartbeat_loop",
    "run_remote_worker",
    "run_remote_worker_with_heartbeat",
    "run_remote_worker_with_url_rotation",
]
