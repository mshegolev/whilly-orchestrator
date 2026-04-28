"""Remote worker async-loop for Whilly v4.0 (TASK-022b1 / TASK-022b2, PRD FR-1.1, FR-1.5, FR-1.6).

Counterpart to :mod:`whilly.worker.local`: same outer state-machine pattern
(``claim → run → complete | fail``), but every state-mutating step goes
through :class:`whilly.adapters.transport.client.RemoteWorkerClient` over
HTTP instead of touching :class:`whilly.adapters.db.repository.TaskRepository`
directly. The split keeps the worker process a thin httpx + pydantic +
:mod:`whilly.core` consumer (PRD FR-1.5) — there is no asyncpg / FastAPI
import path inside this module by design (PRD SC-6).

This module hosts two layered entry points:

* :func:`run_remote_worker` — the bare loop (TASK-022b1). One concern:
  ``claim → run → complete | fail`` over HTTP, no heartbeat, no signals.
* :func:`run_remote_worker_with_heartbeat` — the composition root
  (TASK-022b2). Pairs the bare loop with a parallel heartbeat task under
  one :class:`asyncio.TaskGroup` so the worker keeps
  ``workers.last_heartbeat`` fresh while running. Mirrors the local-worker
  ``run_worker`` from :mod:`whilly.worker.main`. Signal handling
  (TASK-022b3) extends *this* layer the same way 019b2 extended 019b1.

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
Two exit paths only — TASK-022b1 is the "bare" loop (PRD AC: "без
heartbeat и без сигналов"):

* ``max_iterations`` (test-only) hard cap on outer iterations.
* Outer :class:`asyncio.CancelledError` from the supervisor.

The supervisor that wires SIGTERM / SIGINT and the parallel heartbeat
landing in TASK-022b2 / 022b3 sits *above* this function — it cancels
the loop's task to shut down. Mirrors the 019a → 019b1 → 019b2 slicing
on the local side so each layered concern lands in its own commit.

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
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.client import HTTPClientError, RemoteWorkerClient, VersionConflictError
from whilly.core.models import Plan, Task, WorkerId
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


@dataclass(frozen=True)
class RemoteWorkerStats:
    """Counters returned by one :func:`run_remote_worker` invocation.

    Frozen so tests can assert on it without defensive copying. Fields
    mirror :class:`whilly.worker.local.WorkerStats` minus
    ``released_on_shutdown`` — shutdown plumbing is owned by TASK-022b3,
    not 022b1.

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
    """

    iterations: int = 0
    completed: int = 0
    failed: int = 0
    idle_polls: int = 0


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


async def run_remote_worker(
    client: RemoteWorkerClient,
    runner: RemoteRunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    max_iterations: int | None = None,
) -> RemoteWorkerStats:
    """Run the remote worker loop against ``plan.id`` for ``worker_id``.

    See module docstring for the per-iteration contract. The loop exits
    when ``max_iterations`` is reached (test-only) or when the outer
    task is cancelled (production fallback once 022b3 wires SIGTERM /
    SIGINT).

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
        means loop forever — exit only on cancellation. Tests pass an
        integer to make the loop terminable without forcing a
        cancellation token through the AC's "bare" loop signature.

    Returns
    -------
    RemoteWorkerStats
        Counters covering iterations, completions, failures and idle
        polls observed during this invocation.
    """
    iterations = 0
    completed = 0
    failed = 0
    idle_polls = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1

        claimed = await client.claim(worker_id, plan.id)
        if claimed is None:
            # 204 No Content: the server's long-poll already absorbed the
            # idle-wait budget. Re-poll immediately — adding a sleep here
            # would double the wait on every empty-queue iteration.
            idle_polls += 1
            log.debug(
                "remote worker=%s plan=%s: 204 (no PENDING tasks), re-polling immediately",
                worker_id,
                plan.id,
            )
            continue

        prompt = build_task_prompt(claimed, plan)
        result = await runner(claimed, prompt)

        if result.is_complete and result.exit_code == 0:
            try:
                await client.complete(claimed.id, worker_id, claimed.version)
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

    return RemoteWorkerStats(
        iterations=iterations,
        completed=completed,
        failed=failed,
        idle_polls=idle_polls,
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


async def run_remote_worker_with_heartbeat(
    client: RemoteWorkerClient,
    runner: RemoteRunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    max_iterations: int | None = None,
    stop: asyncio.Event | None = None,
) -> RemoteWorkerStats:
    """Run :func:`run_remote_worker` paired with :func:`run_remote_heartbeat_loop`.

    Composition root for the remote worker — the layered counterpart to
    :func:`whilly.worker.main.run_worker` on the local side. Both
    coroutines run under one :class:`asyncio.TaskGroup`; when the main
    worker loop returns (``max_iterations`` reached in tests, outer
    cancellation propagated, or — once TASK-022b3 lands — a SIGTERM-
    flipped ``stop`` event) the inner closure's ``finally`` block sets
    the shared :class:`asyncio.Event`, the heartbeat coroutine wakes
    from its ``wait_for(stop, interval)`` and returns cleanly, and the
    TaskGroup unwinds without :class:`asyncio.CancelledError` plumbing.

    Why a stop event rather than ``heartbeat_task.cancel()``?
        Same rationale as :mod:`whilly.worker.main`: explicit
        cancellation surfaces a :class:`asyncio.CancelledError` from
        the cancelled task that :class:`asyncio.TaskGroup` treats as a
        propagatable error. Using a stop event lets the heartbeat exit
        *normally* — the TaskGroup just awaits both children, sees
        clean returns, and drops out. TASK-022b3's signal handlers
        will set the same event from inside the asyncio loop via
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
        (claim/complete/fail bodies) and the heartbeat task.
    heartbeat_interval:
        Seconds between heartbeat ticks. Defaults to
        :data:`DEFAULT_HEARTBEAT_INTERVAL` (30s).
    max_iterations:
        Hard cap on outer iterations of the main loop. ``None`` means
        loop until cancellation. Tests pass an integer to make the
        composition terminable without wiring a cancellation token
        through the bare loop's signature.
    stop:
        Optional shared shutdown event. Callers that already own a
        shutdown signal (CLI in TASK-022c, integration tests that
        drive shutdown without sending real signals) pass it in;
        otherwise we allocate one internally. Either way the same
        event drives both the heartbeat termination and (once 022b3
        lands) the main-loop release-on-shutdown path.

    Returns
    -------
    RemoteWorkerStats
        Counters returned by the inner :func:`run_remote_worker`. The
        heartbeat is a side-effect on the server (and on
        ``workers.last_heartbeat`` once it lands in Postgres) and
        does not contribute to any counter — same convention as the
        local-worker composition.
    """
    if stop is None:
        stop = asyncio.Event()

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
            )
        finally:
            stop.set()

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

    # ``worker_task`` is guaranteed done after the TaskGroup exits.
    # ``.result()`` re-raises whatever the inner loop raised (TaskGroup
    # would already have surfaced an ExceptionGroup, but the explicit
    # call documents the contract: this function returns the inner
    # stats or raises whatever the inner loop raised).
    return worker_task.result()


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "RemoteRunnerCallable",
    "RemoteWorkerStats",
    "run_remote_heartbeat_loop",
    "run_remote_worker",
    "run_remote_worker_with_heartbeat",
]
