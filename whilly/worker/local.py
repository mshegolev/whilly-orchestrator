"""Local worker async-loop for Whilly v4.0 (TASK-019a, PRD FR-1.6, FR-2.2).

This module is the first composer that ties :mod:`whilly.core` (pure
domain), :mod:`whilly.adapters.db` (Postgres :class:`TaskRepository`) and
:mod:`whilly.adapters.runner` (Claude CLI subprocess) into a unit that
actually runs tasks. The 019a slice was deliberately minimal — no heartbeat,
no signal handling, no CLI; those land in 019b1 / 019b2 / 019c respectively.
TASK-019b2 layered cooperative shutdown on top via an optional ``stop``
event without touching the inner state-transition logic, so the testable
surface stays small enough to exercise exhaustively without spinning up
Postgres or the ``claude`` binary.

Loop contract
-------------
:func:`run_local_worker` is the public entry point. One iteration:

1. ``repo.claim_task(worker_id, plan.id)`` — atomically grab one PENDING
   task, transitioning it to CLAIMED. Returns ``None`` when the queue is
   empty (or every candidate is locked by a peer).
2. ``repo.start_task(task.id, version)`` — flip CLAIMED → IN_PROGRESS so
   the eventual ``complete_task`` passes its ``status = 'IN_PROGRESS'``
   filter. A :class:`VersionConflictError` here means the
   visibility-timeout sweep released the task before we got to it; we drop
   and continue.
3. ``runner(task, prompt)`` — invoke the agent with the prompt built by
   :func:`whilly.core.prompts.build_task_prompt`. The runner is responsible
   for its own retry / error handling (see
   :mod:`whilly.adapters.runner.claude_cli`); we get back a fully-formed
   :class:`AgentResult`.
4. Outcome routing:

   * ``is_complete=True`` and ``exit_code == 0`` →
     ``repo.complete_task(...)`` (IN_PROGRESS → DONE).
   * Anything else → ``repo.fail_task(reason=...)`` (IN_PROGRESS →
     FAILED), with the reason carrying the exit code and a truncated
     stdout snippet so dashboards / post-mortems don't have to dig
     through worker logs.

When ``claim_task`` returns ``None`` we sleep ``idle_wait`` seconds and
poll again — the documented "idle wait → repeat poll" path from the AC.

Termination
-----------
Three exit paths, in order of precedence:

1. **Graceful shutdown via ``stop`` event** (TASK-019b2). When the optional
   :class:`asyncio.Event` argument fires, the loop checks it at every
   safe boundary (top of iteration, after an idle sleep) and exits
   cleanly. If the event fires *during* the runner call, the runner is
   cancelled and the in-flight task is released back to ``PENDING``
   via :meth:`TaskRepository.release_task` so a peer (or this worker on
   restart) can pick it up — no lost work, no aged-out claim waiting on
   the visibility-timeout sweep.
2. ``max_iterations`` (test-only). Hard cap on outer iterations.
3. Outer :class:`asyncio.CancelledError`. Production fallback if the
   composer cancels the task without setting ``stop`` first.

Production callers (``run_worker`` in :mod:`whilly.worker.main`) wire
``stop`` to the SIGTERM / SIGINT signal handlers so a kill from the
process supervisor triggers path #1.

Concurrency note (PRD FR-2.4)
-----------------------------
Three writers can race for any task row: this worker, a peer worker after a
sweep release, and the visibility-timeout sweep itself. The state-machine
+ optimistic-locking lattice means *exactly one* of them wins any given
state transition. Whenever this worker loses a race we get a
:class:`VersionConflictError` from the repository — we log and continue
rather than crashing the loop, because by the time the conflict surfaces
another writer has already taken responsibility for the row. Abandoning is
the safe, correct thing to do.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from whilly.adapters.db.repository import TaskRepository, VersionConflictError
from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.agent_runner import (
    SHELL_COMMAND_BLOCKED_EVENT_TYPE,
    SHELL_COMMAND_FAIL_REASON,
    scan_task_command_surface,
)
from whilly.core.models import Plan, Task, WorkerId
from whilly.core.prompts import (
    PROMPT_INJECTION_BLOCKED_EVENT_TYPE,
    PROMPT_INJECTION_FAIL_REASON,
    PromptInjectionBlocked,
    build_task_prompt,
)
from whilly.llm_ops import (
    LLM_RUN_CANCELLED_EVENT_TYPE,
    LLM_RUN_FAILED_EVENT_TYPE,
    LLM_RUN_FINISHED_EVENT_TYPE,
    LLM_RUN_STARTED_EVENT_TYPE,
    LLMOpsSession,
    finish_llm_session,
    session_event_detail,
    session_event_payload,
    start_llm_session,
)
from whilly.pipeline.events import (
    PipelineTaskEvent,
    make_stage_failed_event,
    make_stage_started_event,
    make_stage_succeeded_event,
    stage_context_from_task,
)
from whilly.pipeline.human_review import (
    HUMAN_REVIEW_REQUIRED_RELEASE_REASON,
    build_human_review_checkpoint,
    is_human_review_approved,
    make_human_review_required_event,
)
from whilly.pipeline.verification import (
    VERIFICATION_FAILED_EVENT,
    VerificationRunOutcome,
    make_verification_result_event,
    make_verification_started_event,
)
from whilly.slack_task_notify import notify_slack_task_started, notify_slack_task_terminal

log = logging.getLogger(__name__)

# Default poll interval when the queue is empty. Short enough that a task
# imported via ``whilly plan import`` feels live; long enough that a fully
# idle worker doesn't burn the database with claim queries. Tests pass a
# smaller value (often 0) so the loop rolls through quickly.
DEFAULT_IDLE_WAIT: Final[float] = 1.0

# Cap on the AgentResult.output snippet stored in the FAIL event payload.
# Claude tool-use transcripts can be megabytes; the audit log only needs
# enough context to identify the failure mode without bloating Postgres
# rows. 500 chars covers every observed real-world error message.
_FAIL_REASON_OUTPUT_CAP: Final[int] = 500

# Type alias for the runner side of the loop. Matches
# :func:`whilly.adapters.runner.claude_cli.run_task` after model and
# backoff_schedule fall back to defaults — passing ``run_task`` directly
# satisfies the alias without an adapter wrapper.
RunnerCallable = Callable[[Task, str], Awaitable[AgentResult]]

VerificationRunnerCallable = Callable[[Task], Awaitable[VerificationRunOutcome]]


@dataclass(frozen=True)
class WorkerStats:
    """Counters returned by one :func:`run_local_worker` invocation.

    Frozen so callers can pass it around without defensive copying. Tests
    assert on these fields directly; later the 019c CLI command will print
    them and the metrics endpoint will surface them.

    Attributes
    ----------
    iterations:
        Outer-loop iterations executed — includes idle polls so a test that
        runs the worker against an empty plan still sees the count grow.
    completed:
        Tasks that reached ``DONE`` via :meth:`TaskRepository.complete_task`.
    failed:
        Tasks marked ``FAILED`` via :meth:`TaskRepository.fail_task` because
        the agent didn't emit ``<promise>COMPLETE</promise>`` or returned a
        non-zero exit code.
    idle_polls:
        Iterations where ``claim_task`` returned ``None`` and the loop
        slept ``idle_wait`` seconds before polling again.
    released_on_shutdown:
        Tasks the worker put back to ``PENDING`` via
        :meth:`TaskRepository.release_task` because a shutdown signal
        arrived mid-runner (TASK-019b2). Always 0 in legacy non-stop runs;
        at most 1 per ``run_local_worker`` invocation since the loop
        breaks out after the release.
    """

    iterations: int = 0
    completed: int = 0
    failed: int = 0
    idle_polls: int = 0
    released_on_shutdown: int = 0


def _truncate_output(output: str) -> str:
    """Trim agent ``output`` for the FAIL event payload.

    Worker logs (and Claude CLI's own stderr capture) keep the full stdout;
    the audit log only needs enough context to identify the failure mode
    without bloating ``events.payload``.
    """
    if len(output) <= _FAIL_REASON_OUTPUT_CAP:
        return output
    return output[:_FAIL_REASON_OUTPUT_CAP] + "…"


def _build_fail_reason(result: AgentResult) -> str:
    """Render an :class:`AgentResult` into a human-readable FAIL reason.

    The reason is what shows up in the dashboard and ``events.payload``
    when a task transitions to FAILED. We always include ``exit_code``
    (so negative codes like ``EXIT_BINARY_NOT_FOUND`` are diagnosable
    without cross-referencing source) and append a truncated output
    snippet when present.
    """
    snippet = _truncate_output(result.output).strip()
    if snippet:
        return f"exit_code={result.exit_code}: {snippet}"
    return f"exit_code={result.exit_code}"


async def _record_llm_event(
    repo: TaskRepository,
    session: LLMOpsSession,
    event_type: str,
    payload: dict[str, object],
    detail: dict[str, object] | None = None,
) -> None:
    """Best-effort Postgres event append for LLM Ops metadata."""

    recorder = getattr(repo, "record_task_event", None)
    if recorder is None:
        return
    try:
        await recorder(session.task_id, event_type, payload, detail=detail)
    except Exception:  # noqa: BLE001 - observability must not fail task execution
        log.warning(
            "llm ops event append failed: task=%s event_type=%s",
            session.task_id,
            event_type,
            exc_info=True,
        )


async def _record_pipeline_event(repo: TaskRepository, event: PipelineTaskEvent | None) -> None:
    """Best-effort Postgres event append for pipeline runtime metadata."""

    if event is None:
        return
    recorder = getattr(repo, "record_task_event", None)
    if recorder is None:
        return
    try:
        await recorder(*event.record_task_event_args(), **event.record_task_event_kwargs())
    except Exception:  # noqa: BLE001 - observability must not fail task execution
        log.warning(
            "pipeline event append failed: task=%s event_type=%s",
            event.task_id,
            event.event_type,
            exc_info=True,
        )


def _verification_failure_detail(outcome: VerificationRunOutcome) -> dict[str, object]:
    """Compact FAIL detail for required verification failures."""

    failed_results = [
        {
            "name": result.name,
            "command": result.command,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "blocked": result.blocked,
            "pattern_matched": result.pattern_matched,
        }
        for result in outcome.results
        if result.required and not result.succeeded
    ]
    return {"reason": "verification_failed", "failed_results": failed_results}


# Reason string written into the RELEASE event payload when the worker
# releases an in-flight task because of SIGTERM / SIGINT (TASK-019b2). Distinct
# from ``"visibility_timeout"`` (the sweep) so dashboards / post-mortems can
# tell why a row bounced. Keep in sync with the value asserted in
# tests/integration/test_worker_signals.py.
SHUTDOWN_RELEASE_REASON: Final[str] = "shutdown"
OPERATOR_PAUSE_RELEASE_REASON: Final[str] = "operator_pause"


async def _sleep_or_stop(idle_wait: float, stop: asyncio.Event | None) -> None:
    """Sleep ``idle_wait`` seconds, or return early when ``stop`` fires.

    Used for the empty-queue idle poll so a SIGTERM during a long
    ``idle_wait`` doesn't have to wait out the full interval before the
    loop notices the shutdown request. Equivalent to ``asyncio.sleep`` when
    ``stop`` is ``None`` — keeps the call site readable without a branch.
    """
    if stop is None:
        await asyncio.sleep(idle_wait)
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=idle_wait)
    except TimeoutError:
        # Normal "interval elapsed" path — caller re-checks ``stop`` at
        # the top of the next iteration.
        return


async def _workers_paused(repo: TaskRepository) -> bool:
    checker = getattr(repo, "is_workers_paused", None)
    if checker is None:
        return False
    return bool(await checker())


async def _release_for_operator_pause(repo: TaskRepository, task: Task, worker_id: WorkerId) -> None:
    try:
        await repo.release_task(task.id, task.version, OPERATOR_PAUSE_RELEASE_REASON)
    except VersionConflictError as exc:
        log.warning(
            "operator-pause release lost the race: worker=%s task=%s expected_version=%d actual=%s",
            worker_id,
            task.id,
            task.version,
            exc.actual_version,
        )


async def _await_runner_or_stop(
    runner_coro: Awaitable[AgentResult],
    stop: asyncio.Event,
) -> tuple[AgentResult | None, bool]:
    """Race ``runner_coro`` against ``stop``; return ``(result, shutdown_requested)``.

    Returns ``(result, False)`` when the runner finishes normally and
    ``(None, True)`` when ``stop`` fires first. In the shutdown path the
    runner task is cancelled and awaited so its underlying subprocess /
    sockets get a chance to tear down before we report shutdown — without
    this, an ``asyncpg`` connection inside the runner could leak past the
    test boundary.

    We deliberately swallow the runner's ``CancelledError`` (and any other
    exception it surfaces during teardown) rather than re-raising. The
    point of the shutdown path is to release the task atomically; a
    cancellation-time error from the runner is not a worker bug, just the
    natural consequence of yanking the agent mid-call.
    """
    runner_task: asyncio.Task[AgentResult] = asyncio.ensure_future(runner_coro)
    stop_task: asyncio.Task[bool] = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait(
            {runner_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # Outer cancellation: tear down both tasks before propagating so
        # we don't leak orphaned coroutines.
        runner_task.cancel()
        stop_task.cancel()
        raise

    if stop_task in done:
        runner_task.cancel()
        try:
            await runner_task
        except (asyncio.CancelledError, Exception) as exc:
            log.debug("runner cancelled during shutdown: %r", exc)
        return None, True

    # Runner finished normally — clean up the stop watcher.
    stop_task.cancel()
    try:
        await stop_task
    except asyncio.CancelledError:
        pass
    return runner_task.result(), False


async def _await_verification_or_stop(
    verification_coro: Awaitable[VerificationRunOutcome],
    stop: asyncio.Event,
) -> tuple[VerificationRunOutcome | None, bool]:
    """Race verification against shutdown, mirroring the runner path."""

    verification_task: asyncio.Task[VerificationRunOutcome] = asyncio.ensure_future(verification_coro)
    stop_task: asyncio.Task[bool] = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait(
            {verification_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        verification_task.cancel()
        stop_task.cancel()
        raise

    if stop_task in done:
        verification_task.cancel()
        try:
            await verification_task
        except (asyncio.CancelledError, Exception) as exc:
            log.debug("verification cancelled during shutdown: %r", exc)
        return None, True

    stop_task.cancel()
    try:
        await stop_task
    except asyncio.CancelledError:
        pass
    return verification_task.result(), False


async def run_local_worker(
    repo: TaskRepository,
    runner: RunnerCallable,
    plan: Plan,
    worker_id: WorkerId,
    *,
    idle_wait: float = DEFAULT_IDLE_WAIT,
    max_iterations: int | None = None,
    stop: asyncio.Event | None = None,
    post_complete_hook: Callable[[Task], Awaitable[None]] | None = None,
    verification_runner: VerificationRunnerCallable | None = None,
) -> WorkerStats:
    """Run the local worker loop against ``plan.id`` for ``worker_id``.

    See module docstring for the full per-iteration contract. The loop
    exits when any of the following hold (checked at iteration boundaries
    and during the runner call):

    * ``stop`` is set (graceful shutdown — TASK-019b2),
    * ``max_iterations`` reached (test-only),
    * outer :class:`asyncio.CancelledError` (production fallback).

    Parameters
    ----------
    repo:
        Postgres-backed task repository. The worker calls
        :meth:`~whilly.adapters.db.repository.TaskRepository.claim_task`,
        :meth:`~whilly.adapters.db.repository.TaskRepository.start_task`,
        :meth:`~whilly.adapters.db.repository.TaskRepository.complete_task`,
        :meth:`~whilly.adapters.db.repository.TaskRepository.fail_task`,
        and :meth:`~whilly.adapters.db.repository.TaskRepository.release_task`.
    runner:
        Coroutine ``(task, prompt) -> AgentResult``. In production this is
        :func:`whilly.adapters.runner.run_task`; tests pass a stub that
        returns canned :class:`AgentResult` values.
    plan:
        Plan whose tasks are eligible for claim. Only ``plan.id`` is read
        for ``claim_task``; the full plan is forwarded to
        :func:`whilly.core.prompts.build_task_prompt` so the agent has
        plan-level context (name, id) without an extra DB round-trip.
    worker_id:
        Identity passed to ``claim_task``. Must already exist in the
        ``workers`` table — the registration flow is owned by TASK-021b.
        Tests insert a fixture row directly.
    idle_wait:
        Seconds to ``asyncio.sleep`` between polls when the queue is
        empty. Defaults to :data:`DEFAULT_IDLE_WAIT`. When ``stop`` is
        provided, the sleep wakes early on shutdown so SIGTERM doesn't
        have to wait out the full interval.
    max_iterations:
        Hard cap on outer-loop iterations. ``None`` (production default)
        means loop forever — exit only on cancellation or ``stop``. Tests
        pass an integer to make the loop terminable.
    stop:
        Optional :class:`asyncio.Event` for cooperative graceful shutdown.
        When the event fires, the loop releases any in-flight task back
        to ``PENDING`` via :meth:`TaskRepository.release_task` and exits.
        Wired up by :func:`whilly.worker.main.run_worker` to SIGTERM /
        SIGINT in production; tests set it directly to exercise the
        shutdown path without sending real signals.

    Returns
    -------
    WorkerStats
        Counters covering iterations, completions, failures, idle polls,
        and shutdown releases observed during this invocation.
    """
    iterations = 0
    completed = 0
    failed = 0
    idle_polls = 0
    released_on_shutdown = 0

    while max_iterations is None or iterations < max_iterations:
        # Check ``stop`` *before* incrementing ``iterations`` so a shutdown
        # at the boundary doesn't inflate the iteration count for tests.
        if stop is not None and stop.is_set():
            log.info(
                "worker=%s plan=%s: shutdown requested, exiting loop cleanly",
                worker_id,
                plan.id,
            )
            break

        iterations += 1

        if await _workers_paused(repo):
            idle_polls += 1
            log.info(
                "worker=%s plan=%s: global pause active, skipping claim",
                worker_id,
                plan.id,
            )
            await _sleep_or_stop(idle_wait, stop)
            continue

        claimed = await repo.claim_task(worker_id, plan.id)
        if claimed is None:
            idle_polls += 1
            log.debug(
                "worker=%s plan=%s: no PENDING tasks, sleeping %ss",
                worker_id,
                plan.id,
                idle_wait,
            )
            await _sleep_or_stop(idle_wait, stop)
            continue

        # CLAIMED → IN_PROGRESS. Lost-race here means the visibility-timeout
        # sweep released the row to a peer; drop silently and re-poll.
        try:
            running = await repo.start_task(claimed.id, claimed.version)
        except VersionConflictError as exc:
            log.warning(
                "start_task lost the race: task=%s expected_version=%d actual=%s",
                claimed.id,
                claimed.version,
                exc.actual_version,
            )
            continue

        if await _workers_paused(repo):
            log.info(
                "worker=%s task=%s: global pause observed after start, releasing claim",
                worker_id,
                running.id,
            )
            await _release_for_operator_pause(repo, running, worker_id)
            continue

        stage_context = stage_context_from_task(running, plan)
        await _record_pipeline_event(repo, make_stage_started_event(stage_context))
        checkpoint = build_human_review_checkpoint(
            task=running,
            stage={"id": stage_context.stage_id} if stage_context is not None else None,
            plan_id=plan.id,
        )
        if checkpoint is not None:
            await _record_pipeline_event(repo, make_human_review_required_event(checkpoint))

        try:
            prompt = build_task_prompt(running, plan)
        except PromptInjectionBlocked as exc:
            try:
                await _record_pipeline_event(
                    repo,
                    make_stage_failed_event(
                        stage_context,
                        reason=PROMPT_INJECTION_FAIL_REASON,
                        detail=exc.event_payload,
                    ),
                )
                await repo.fail_task(
                    running.id,
                    running.version,
                    PROMPT_INJECTION_FAIL_REASON,
                    detail=exc.event_payload,
                    prelude_event_type=PROMPT_INJECTION_BLOCKED_EVENT_TYPE,
                    prelude_payload=exc.event_payload,
                )
            except VersionConflictError as conflict:
                log.warning(
                    "prompt guard fail_task lost the race: task=%s expected_version=%d actual=%s",
                    running.id,
                    running.version,
                    conflict.actual_version,
                )
                continue
            failed += 1
            log.warning(
                "worker=%s task=%s → FAILED (%s marker=%r)",
                worker_id,
                running.id,
                PROMPT_INJECTION_FAIL_REASON,
                exc.match.matched_marker,
            )
            continue

        shell_scan = scan_task_command_surface(running)
        if shell_scan.blocked:
            payload = shell_scan.event_payload(task_id=running.id, plan_id=plan.id)
            try:
                await _record_pipeline_event(
                    repo,
                    make_stage_failed_event(
                        stage_context,
                        reason=SHELL_COMMAND_FAIL_REASON,
                        detail=payload,
                    ),
                )
                await repo.fail_task(
                    running.id,
                    running.version,
                    SHELL_COMMAND_FAIL_REASON,
                    detail=payload,
                    prelude_event_type=SHELL_COMMAND_BLOCKED_EVENT_TYPE,
                    prelude_payload=payload,
                )
            except VersionConflictError as conflict:
                log.warning(
                    "shell guard fail_task lost the race: task=%s expected_version=%d actual=%s",
                    running.id,
                    running.version,
                    conflict.actual_version,
                )
                continue
            failed += 1
            log.warning(
                "worker=%s task=%s → FAILED (%s pattern=%r)",
                worker_id,
                running.id,
                SHELL_COMMAND_FAIL_REASON,
                shell_scan.pattern_matched,
            )
            continue

        llm_session: LLMOpsSession | None = None
        try:
            llm_session = start_llm_session(running, plan, worker_id, prompt, attempt=running.version)
            await _record_llm_event(
                repo,
                llm_session,
                LLM_RUN_STARTED_EVENT_TYPE,
                session_event_payload(llm_session, "started"),
                session_event_detail(llm_session, "started"),
            )
            notify_slack_task_started(llm_session)
        except Exception:  # noqa: BLE001 - keep task execution independent from observability
            log.warning("llm ops session start failed: task=%s", running.id, exc_info=True)
            llm_session = None

        # Race the runner against ``stop`` so SIGTERM mid-runner doesn't
        # have to wait for an arbitrarily long agent call before we can
        # release the task. When ``stop`` is None (legacy callers, unit
        # tests for 019a/019b1) we just await the runner directly — the
        # original codepath, untouched.
        if llm_session is None:
            if stop is None:
                result: AgentResult | None = await runner(running, prompt)
                shutdown_during_run = False
            else:
                result, shutdown_during_run = await _await_runner_or_stop(runner(running, prompt), stop)
        else:
            with llm_session.runner_environment():
                if stop is None:
                    result = await runner(running, prompt)
                    shutdown_during_run = False
                else:
                    result, shutdown_during_run = await _await_runner_or_stop(runner(running, prompt), stop)

        if shutdown_during_run:
            if llm_session is not None:
                try:
                    detail = finish_llm_session(
                        llm_session,
                        None,
                        "cancelled",
                        error=SHUTDOWN_RELEASE_REASON,
                    )
                    await _record_llm_event(
                        repo,
                        llm_session,
                        LLM_RUN_CANCELLED_EVENT_TYPE,
                        session_event_payload(llm_session, "cancelled", error=SHUTDOWN_RELEASE_REASON),
                        detail,
                    )
                except Exception:  # noqa: BLE001 - shutdown release is the priority
                    log.warning("llm ops cancellation record failed: task=%s", running.id, exc_info=True)
            log.info(
                "worker=%s task=%s: shutdown mid-runner, releasing claim",
                worker_id,
                running.id,
            )
            try:
                await repo.release_task(running.id, running.version, SHUTDOWN_RELEASE_REASON)
                released_on_shutdown += 1
            except VersionConflictError as exc:
                # The visibility-timeout sweep beat us to it — the task is
                # already PENDING with claimed_by cleared. That's exactly
                # what we wanted; treat as success and exit cleanly.
                log.warning(
                    "release_task lost the race: task=%s expected_version=%d actual=%s — already released",
                    running.id,
                    running.version,
                    exc.actual_version,
                )
            break

        # ``shutdown_during_run`` was False, so ``result`` is set.
        assert result is not None  # for mypy; the helper's contract guarantees this
        if llm_session is not None:
            llm_status = "success" if result.is_complete and result.exit_code == 0 else "failed"
            llm_event_type = LLM_RUN_FINISHED_EVENT_TYPE if llm_status == "success" else LLM_RUN_FAILED_EVENT_TYPE
            try:
                detail = finish_llm_session(llm_session, result, llm_status)
                await _record_llm_event(
                    repo,
                    llm_session,
                    llm_event_type,
                    session_event_payload(llm_session, llm_status, result),
                    detail,
                )
            except Exception:  # noqa: BLE001 - task completion/failure still owns the state transition
                log.warning("llm ops finish record failed: task=%s", running.id, exc_info=True)

        if await _workers_paused(repo):
            log.info(
                "worker=%s task=%s: global pause observed before terminal transition, releasing claim",
                worker_id,
                running.id,
            )
            await _release_for_operator_pause(repo, running, worker_id)
            continue

        if result.is_complete and result.exit_code == 0:
            if verification_runner is not None:
                try:
                    await _record_pipeline_event(
                        repo,
                        make_verification_started_event(running.id, plan_id=plan.id),
                    )
                    if stop is None:
                        verification_outcome = await verification_runner(running)
                        shutdown_during_verification = False
                    else:
                        verification_outcome, shutdown_during_verification = await _await_verification_or_stop(
                            verification_runner(running),
                            stop,
                        )
                except Exception as exc:  # noqa: BLE001 - verification failure owns task state
                    detail = {"error": f"{type(exc).__name__}: {exc}"}
                    await _record_pipeline_event(
                        repo,
                        PipelineTaskEvent(
                            task_id=running.id,
                            event_type=VERIFICATION_FAILED_EVENT,
                            payload={
                                "task_id": running.id,
                                "plan_id": plan.id,
                                "reason": "verification_runner_exception",
                            },
                            detail=detail,
                        ),
                    )
                    await _record_pipeline_event(
                        repo,
                        make_stage_failed_event(stage_context, reason="verification_failed", detail=detail),
                    )
                    try:
                        await repo.fail_task(running.id, running.version, "verification_failed", detail=detail)
                    except VersionConflictError as conflict:
                        log.warning(
                            "verification fail_task lost the race: task=%s expected_version=%d actual=%s",
                            running.id,
                            running.version,
                            conflict.actual_version,
                        )
                        continue
                    failed += 1
                    log.info("worker=%s task=%s → FAILED (verification_failed)", worker_id, running.id)
                    continue

                if shutdown_during_verification:
                    log.info(
                        "worker=%s task=%s: shutdown mid-verification, releasing claim",
                        worker_id,
                        running.id,
                    )
                    try:
                        await repo.release_task(running.id, running.version, SHUTDOWN_RELEASE_REASON)
                        released_on_shutdown += 1
                    except VersionConflictError as exc:
                        log.warning(
                            "release_task lost the race: task=%s expected_version=%d actual=%s — already released",
                            running.id,
                            running.version,
                            exc.actual_version,
                        )
                    break

                assert verification_outcome is not None
                for verification_result in verification_outcome.results:
                    await _record_pipeline_event(
                        repo,
                        make_verification_result_event(running.id, verification_result, plan_id=plan.id),
                    )
                if verification_outcome.required_failed:
                    detail = _verification_failure_detail(verification_outcome)
                    try:
                        await _record_pipeline_event(
                            repo,
                            make_stage_failed_event(stage_context, reason="verification_failed", detail=detail),
                        )
                        await repo.fail_task(running.id, running.version, "verification_failed", detail=detail)
                    except VersionConflictError as exc:
                        log.warning(
                            "verification fail_task lost the race: task=%s expected_version=%d actual=%s",
                            running.id,
                            running.version,
                            exc.actual_version,
                        )
                        continue
                    if llm_session is not None:
                        notify_slack_task_terminal(llm_session, "FAILED", result, reason="verification_failed")
                    failed += 1
                    log.info("worker=%s task=%s → FAILED (verification_failed)", worker_id, running.id)
                    continue

            if checkpoint is not None:
                review_events = await repo.list_task_events(running.id, event_prefix="human_review.")
                if not is_human_review_approved(checkpoint, review_events):
                    try:
                        await repo.release_task(
                            running.id,
                            running.version,
                            HUMAN_REVIEW_REQUIRED_RELEASE_REASON,
                        )
                    except VersionConflictError as exc:
                        log.warning(
                            "human-review release lost the race: task=%s expected_version=%d actual=%s",
                            running.id,
                            running.version,
                            exc.actual_version,
                        )
                        continue
                    log.info("worker=%s task=%s → PENDING (human_review_required)", worker_id, running.id)
                    continue

            try:
                # ``cost_usd`` flows from the agent runner's parsed usage
                # envelope into the per-plan spend accumulator (TASK-102,
                # VAL-BUDGET-030). ``None`` / 0.0 (e.g. Claude CLI did
                # not emit ``total_cost_usd``) is the documented
                # no-op-spend path (VAL-BUDGET-032).
                await repo.complete_task(
                    running.id,
                    running.version,
                    cost_usd=result.usage.cost_usd,
                )
            except VersionConflictError as exc:
                log.warning(
                    "complete_task lost the race: task=%s expected_version=%d actual=%s",
                    running.id,
                    running.version,
                    exc.actual_version,
                )
                continue
            await _record_pipeline_event(repo, make_stage_succeeded_event(stage_context))
            if llm_session is not None:
                notify_slack_task_terminal(llm_session, "DONE", result)
            completed += 1
            log.info("worker=%s task=%s → DONE", worker_id, running.id)
            if post_complete_hook is not None:
                try:
                    await post_complete_hook(running)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "worker=%s task=%s: post_complete_hook raised (%s); swallowing",
                        worker_id,
                        running.id,
                        exc,
                    )
        else:
            reason = _build_fail_reason(result)
            try:
                await _record_pipeline_event(
                    repo,
                    make_stage_failed_event(stage_context, reason=reason),
                )
                await repo.fail_task(running.id, running.version, reason)
            except VersionConflictError as exc:
                log.warning(
                    "fail_task lost the race: task=%s expected_version=%d actual=%s",
                    running.id,
                    running.version,
                    exc.actual_version,
                )
                continue
            if llm_session is not None:
                notify_slack_task_terminal(llm_session, "FAILED", result, reason=reason)
            failed += 1
            log.info("worker=%s task=%s → FAILED (%s)", worker_id, running.id, reason)

    return WorkerStats(
        iterations=iterations,
        completed=completed,
        failed=failed,
        idle_polls=idle_polls,
        released_on_shutdown=released_on_shutdown,
    )


__all__ = [
    "DEFAULT_IDLE_WAIT",
    "OPERATOR_PAUSE_RELEASE_REASON",
    "SHUTDOWN_RELEASE_REASON",
    "RunnerCallable",
    "VerificationRunnerCallable",
    "WorkerStats",
    "run_local_worker",
]
