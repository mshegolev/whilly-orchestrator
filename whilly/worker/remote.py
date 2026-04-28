"""Remote worker async-loop for Whilly v4.0 (TASK-022b1, PRD FR-1.1, FR-1.5).

Counterpart to :mod:`whilly.worker.local`: same outer state-machine pattern
(``claim → run → complete | fail``), but every state-mutating step goes
through :class:`whilly.adapters.transport.client.RemoteWorkerClient` over
HTTP instead of touching :class:`whilly.adapters.db.repository.TaskRepository`
directly. The split keeps the worker process a thin httpx + pydantic +
:mod:`whilly.core` consumer (PRD FR-1.5) — there is no asyncpg / FastAPI
import path inside this module by design (PRD SC-6).

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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.client import RemoteWorkerClient, VersionConflictError
from whilly.core.models import Plan, Task, WorkerId
from whilly.core.prompts import build_task_prompt

log = logging.getLogger(__name__)

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


__all__ = [
    "RemoteRunnerCallable",
    "RemoteWorkerStats",
    "run_remote_worker",
]
