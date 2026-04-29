"""Unit tests for :mod:`whilly.worker.remote` (TASK-022b1, PRD FR-1.1, FR-1.5).

What we cover
-------------
- Happy path: claim → run → complete bumps the ``completed`` counter and
  the underlying client receives the right ``(task_id, worker_id, version)``.
- Failure path: a non-zero exit or missing completion marker triggers
  :meth:`RemoteWorkerClient.fail` with the canonical
  ``exit_code=<n>: <truncated stdout>`` reason string.
- 204 / ``None`` from claim is the AC-mandated re-poll-without-delay path —
  ``idle_polls`` increments, no ``asyncio.sleep`` is called, and the next
  iteration immediately re-issues the claim.
- :class:`VersionConflictError` on complete or fail is logged and the loop
  continues without crashing — the same "abandon and re-poll" policy as
  the local worker.
- The runner receives the prompt rendered by
  :func:`whilly.core.prompts.build_task_prompt` (smoke check: contains the
  task id and the plan name).
- ``max_iterations=0`` is a valid no-op call (the loop never enters the
  body) — pin the boundary so a future regression that flips the comparison
  surfaces here.

How we isolate from real I/O
----------------------------
The tests use a hand-rolled :class:`FakeRemoteClient` rather than
``unittest.mock``. The surface is small (three async methods) and an
in-memory script lets us assert on call ordering and the actual
arguments without spreading ``mock.assert_called_with`` everywhere.
The runner is a small async closure that returns canned
:class:`AgentResult` instances.

The ``asyncio.sleep`` patch (via ``monkeypatch``) is the load-bearing
fixture for the "no delay on 204" assertion: a regression that started
sleeping between idle polls would surface as a non-empty list here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace

import pytest

from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.client import VersionConflictError
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.worker import remote as worker_remote
from whilly.worker.remote import (
    RemoteWorkerStats,
    _build_fail_reason,
    _truncate_output,
    run_remote_worker,
)

# --------------------------------------------------------------------------- #
# Test fixtures and fakes
# --------------------------------------------------------------------------- #


WORKER_ID: WorkerId = "worker-remote-test"
PLAN_ID = "plan-remote-test"


def _make_task(
    task_id: str = "T-001",
    *,
    status: TaskStatus = TaskStatus.CLAIMED,
    version: int = 1,
) -> Task:
    """Build a task with realistic but minimal fields.

    Default status is :data:`TaskStatus.CLAIMED` (and version=1) because
    that's what :meth:`RemoteWorkerClient.claim` returns to the loop —
    the server has already advanced the row before the wire response.
    """
    return Task(
        id=task_id,
        status=status,
        priority=Priority.MEDIUM,
        description=f"description for {task_id}",
        version=version,
    )


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="Remote Test Plan")


def _make_conflict(
    *,
    task_id: TaskId | None,
    expected_version: int,
    actual_version: int | None,
    actual_status: TaskStatus | None,
) -> VersionConflictError:
    """Build a :class:`VersionConflictError` matching the wire 409 envelope.

    Centralised so tests don't need to remember the keyword-only signature
    on every call site. Mirrors the projection
    :class:`whilly.adapters.transport.client._build_version_conflict`
    performs against :class:`ErrorResponse`.
    """
    return VersionConflictError(
        f"version conflict on task={task_id}",
        status_code=409,
        response_body="",
        task_id=task_id,
        expected_version=expected_version,
        actual_version=actual_version,
        actual_status=actual_status,
        error_code="version_conflict",
    )


class FakeRemoteClient:
    """In-memory stand-in for :class:`RemoteWorkerClient`.

    Stores per-method scripted return values and records every call.
    Per-method queues (``claim_results`` etc.) pop left-to-right so a
    test can script multiple iterations precisely.

    Why not :class:`unittest.mock.AsyncMock`?
        Same rationale as the local-worker test fake: AsyncMock obscures
        the per-call wiring behind ``side_effect`` lists / iterators,
        and the real value here is asserting on the exact sequence of
        ``(task_id, worker_id, version)`` arguments — a hand-rolled fake
        keeps that explicit.
    """

    def __init__(self) -> None:
        self.claim_results: list[Task | None] = []
        # ``complete`` / ``fail`` queues take either the post-update Task
        # (for the happy path — we wrap it in a CompleteResponse here so
        # the loop's caller receives a realistic value) or a
        # :class:`VersionConflictError` to script the 409 path.
        self.complete_results: list[Task | VersionConflictError] = []
        self.fail_results: list[Task | VersionConflictError] = []

        self.claim_calls: list[tuple[str, str]] = []
        self.complete_calls: list[tuple[TaskId, str, int, object]] = []
        self.fail_calls: list[tuple[TaskId, str, int, str]] = []

    async def claim(self, worker_id: str, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        if not self.claim_results:
            raise AssertionError("FakeRemoteClient.claim called more times than scripted")
        return self.claim_results.pop(0)

    async def complete(
        self,
        task_id: TaskId,
        worker_id: str,
        version: int,
        cost_usd: object = None,  # TASK-102
    ) -> object:
        self.complete_calls.append((task_id, worker_id, version, cost_usd))
        if not self.complete_results:
            raise AssertionError("FakeRemoteClient.complete called more times than scripted")
        result = self.complete_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        # The loop only ever uses the absence of an exception — return an
        # opaque sentinel rather than reconstructing the full pydantic
        # CompleteResponse. The signature returns ``object`` so mypy
        # against the production client's ``CompleteResponse`` return is
        # checked at the boundary, not here.
        return result

    async def fail(self, task_id: TaskId, worker_id: str, version: int, reason: str) -> object:
        self.fail_calls.append((task_id, worker_id, version, reason))
        if not self.fail_results:
            raise AssertionError("FakeRemoteClient.fail called more times than scripted")
        result = self.fail_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Replace ``asyncio.sleep`` with a recorder.

    The remote loop's contract is *no* sleep on 204 (the server's long-poll
    already absorbed the wait). We patch ``asyncio.sleep`` on the module
    asyncio reference so the assertion is "this list stayed empty" — a
    regression that started sleeping between idle polls would show up as
    a non-empty list here.
    """
    sleeps: list[float] = []

    async def _fake(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    yield sleeps


# --------------------------------------------------------------------------- #
# RemoteWorkerStats / pure helpers
# --------------------------------------------------------------------------- #


def test_remote_worker_stats_defaults_are_zero() -> None:
    """Empty stats are the natural zero so callers can compare equality."""
    assert RemoteWorkerStats() == RemoteWorkerStats(iterations=0, completed=0, failed=0, idle_polls=0)


def test_truncate_output_passes_short_strings_through() -> None:
    assert _truncate_output("short") == "short"


def test_truncate_output_caps_long_strings_with_ellipsis() -> None:
    """Long strings get capped at ``_FAIL_REASON_OUTPUT_CAP`` + a single
    ellipsis char — same shape as the local worker's helper, intentionally
    so dashboards can grep both flavours under one prefix."""
    long = "x" * 1000
    truncated = _truncate_output(long)
    assert len(truncated) == worker_remote._FAIL_REASON_OUTPUT_CAP + 1
    assert truncated.endswith("…")


def test_build_fail_reason_includes_exit_code_and_snippet() -> None:
    result = AgentResult(output="boom", exit_code=42)
    assert _build_fail_reason(result) == "exit_code=42: boom"


def test_build_fail_reason_omits_empty_snippet() -> None:
    """No stdout (binary missing, spawn blocked) → bare exit code."""
    result = AgentResult(output="", exit_code=-2)
    assert _build_fail_reason(result) == "exit_code=-2"


def test_build_fail_reason_strips_whitespace_in_snippet() -> None:
    """Whitespace-only output is treated as empty — agent transcripts often
    end in a trailing newline that should not bloat the audit reason."""
    result = AgentResult(output="   \n\n   ", exit_code=1)
    assert _build_fail_reason(result) == "exit_code=1"


# --------------------------------------------------------------------------- #
# Happy path — claim → run → complete
# --------------------------------------------------------------------------- #


async def test_completes_one_task_happy_path(fake_sleep: list[float]) -> None:
    """Single iteration: claim returns a task, runner succeeds, complete is called."""
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-001", status=TaskStatus.CLAIMED, version=1)
    done = replace(claimed, status=TaskStatus.DONE, version=2)

    client.claim_results.append(claimed)
    client.complete_results.append(done)

    captured_prompt: list[str] = []

    async def runner(task: Task, prompt: str) -> AgentResult:
        captured_prompt.append(prompt)
        assert task.id == "T-001"
        return AgentResult(
            output="all good <promise>COMPLETE</promise>",
            exit_code=0,
            is_complete=True,
        )

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]  # FakeRemoteClient duck-types RemoteWorkerClient
        runner,
        plan,
        WORKER_ID,
        max_iterations=1,
    )

    assert stats == RemoteWorkerStats(iterations=1, completed=1, failed=0, idle_polls=0)
    assert client.claim_calls == [(WORKER_ID, PLAN_ID)]
    # Note: the AC for TASK-022b1 says the remote loop is claim → run →
    # complete/fail. complete sees the *post-claim* version (1) — there's
    # no start hop on the wire today (see module docstring "protocol gap").
    assert client.complete_calls == [("T-001", WORKER_ID, 1, 0.0)]
    assert client.fail_calls == []
    # Prompt smoke check: the task id and plan name flow through.
    assert len(captured_prompt) == 1
    assert "T-001" in captured_prompt[0]
    assert "Remote Test Plan" in captured_prompt[0]
    # AC-load-bearing: no asyncio.sleep on the happy path.
    assert fake_sleep == []


async def test_versions_thread_through_to_complete(fake_sleep: list[float]) -> None:
    """The version the worker passes to ``complete`` is what claim returned —
    proves we're not accidentally feeding stale or mutated versions."""
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-9", status=TaskStatus.CLAIMED, version=7)
    done = replace(claimed, status=TaskStatus.DONE, version=8)

    client.claim_results.append(claimed)
    client.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=1,
    )

    assert client.complete_calls == [("T-9", WORKER_ID, 7, 0.0)]


# --------------------------------------------------------------------------- #
# Failure path — non-zero exit or no completion marker → client.fail
# --------------------------------------------------------------------------- #


async def test_fails_task_when_agent_returns_nonzero_exit(fake_sleep: list[float]) -> None:
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-002", status=TaskStatus.CLAIMED, version=3)
    failed = replace(claimed, status=TaskStatus.FAILED, version=4)

    client.claim_results.append(claimed)
    client.fail_results.append(failed)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="exploded", exit_code=1, is_complete=False)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=1,
    )

    assert stats.failed == 1
    assert stats.completed == 0
    assert client.complete_calls == []
    assert len(client.fail_calls) == 1
    task_id, worker_id, version, reason = client.fail_calls[0]
    assert task_id == "T-002"
    assert worker_id == WORKER_ID
    assert version == 3
    assert reason == "exit_code=1: exploded"


async def test_fails_task_when_completion_marker_missing(fake_sleep: list[float]) -> None:
    """``is_complete=False`` with exit_code=0 still fails — the agent has to
    emit ``<promise>COMPLETE</promise>`` to confirm success on the wire."""
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-003", status=TaskStatus.CLAIMED, version=1)
    failed = replace(claimed, status=TaskStatus.FAILED, version=2)

    client.claim_results.append(claimed)
    client.fail_results.append(failed)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="silent success", exit_code=0, is_complete=False)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=1,
    )

    assert stats.failed == 1
    assert client.complete_calls == []
    assert client.fail_calls[0][3] == "exit_code=0: silent success"


# --------------------------------------------------------------------------- #
# Idle / 204 — claim returns None
# --------------------------------------------------------------------------- #


async def test_claim_returning_none_polls_again_without_sleep(fake_sleep: list[float]) -> None:
    """The AC's load-bearing case: 204 from the server → re-poll *immediately*.

    A regression that started sleeping between idle polls would either
    double the long-poll budget on the server (two 30s holds back-to-back)
    or burn worker capacity to no end. Pin both: ``idle_polls`` increments
    AND ``asyncio.sleep`` is never called.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    # Three idle polls in a row — exit via ``max_iterations``.
    client.claim_results.extend([None, None, None])

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover — never called
        raise AssertionError("runner must not be called when claim returns None")

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=3,
    )

    assert stats == RemoteWorkerStats(iterations=3, completed=0, failed=0, idle_polls=3)
    assert client.complete_calls == []
    assert client.fail_calls == []
    # The contract that's the whole point of TASK-022b1's 204-handling AC:
    assert fake_sleep == []


async def test_claim_none_then_task_continues_processing(fake_sleep: list[float]) -> None:
    """A 204 followed by a 200 task: the loop must process the second
    iteration's task normally — no off-by-one on the iteration counter or
    the idle-poll counter."""
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-004", status=TaskStatus.CLAIMED, version=1)
    done = replace(claimed, status=TaskStatus.DONE, version=2)

    client.claim_results.extend([None, claimed])
    client.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=2,
    )

    assert stats == RemoteWorkerStats(iterations=2, completed=1, failed=0, idle_polls=1)
    assert client.complete_calls == [("T-004", WORKER_ID, 1, 0.0)]


# --------------------------------------------------------------------------- #
# Version conflict — log + continue
# --------------------------------------------------------------------------- #


async def test_complete_version_conflict_is_logged_and_loop_continues(
    fake_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 409 on complete (lost race or protocol-gap CLAIMED-state) must not
    crash the loop. ``completed`` stays at 0 and the next iteration runs."""
    client = FakeRemoteClient()
    plan = _make_plan()

    first = _make_task("T-005", status=TaskStatus.CLAIMED, version=1)
    second = _make_task("T-006", status=TaskStatus.CLAIMED, version=2)
    done = replace(second, status=TaskStatus.DONE, version=3)

    client.claim_results.extend([first, second])
    client.complete_results.append(
        _make_conflict(
            task_id="T-005",
            expected_version=1,
            actual_version=2,
            actual_status=TaskStatus.PENDING,
        )
    )
    client.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    with caplog.at_level("WARNING", logger="whilly.worker.remote"):
        stats = await run_remote_worker(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            max_iterations=2,
        )

    # The first complete lost the race → not counted; the second succeeded.
    assert stats == RemoteWorkerStats(iterations=2, completed=1, failed=0, idle_polls=0)
    assert client.complete_calls == [("T-005", WORKER_ID, 1, 0.0), ("T-006", WORKER_ID, 2, 0.0)]
    assert any("remote complete lost the race" in record.message for record in caplog.records)


async def test_fail_version_conflict_is_logged_and_loop_continues(
    fake_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Symmetric to the complete path: a 409 on fail must not crash the loop."""
    client = FakeRemoteClient()
    plan = _make_plan()

    first = _make_task("T-007", status=TaskStatus.CLAIMED, version=1)
    second = _make_task("T-008", status=TaskStatus.CLAIMED, version=2)
    failed = replace(second, status=TaskStatus.FAILED, version=3)

    client.claim_results.extend([first, second])
    client.fail_results.append(
        _make_conflict(
            task_id="T-007",
            expected_version=1,
            actual_version=2,
            actual_status=TaskStatus.PENDING,
        )
    )
    client.fail_results.append(failed)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="boom", exit_code=1, is_complete=False)

    with caplog.at_level("WARNING", logger="whilly.worker.remote"):
        stats = await run_remote_worker(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            max_iterations=2,
        )

    assert stats == RemoteWorkerStats(iterations=2, completed=0, failed=1, idle_polls=0)
    assert client.fail_calls == [
        ("T-007", WORKER_ID, 1, "exit_code=1: boom"),
        ("T-008", WORKER_ID, 2, "exit_code=1: boom"),
    ]
    assert any("remote fail lost the race" in record.message for record in caplog.records)


async def test_complete_conflict_with_none_actual_status_logs_cleanly(
    fake_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``actual_status=None`` (row gone — FK cascade in tests, mis-routed
    worker in prod) must not crash the log formatter. The defensive
    ``actual_status.value if exc.actual_status else None`` ternary in the
    log call is exactly what this test pins.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-009", status=TaskStatus.CLAIMED, version=1)
    client.claim_results.append(claimed)
    client.complete_results.append(
        _make_conflict(
            task_id="T-009",
            expected_version=1,
            actual_version=None,
            actual_status=None,
        )
    )

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    with caplog.at_level("WARNING", logger="whilly.worker.remote"):
        stats = await run_remote_worker(
            client,  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            max_iterations=1,
        )

    assert stats.completed == 0
    # The log line went through without a TypeError on the ternary.
    assert any("remote complete lost the race" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- #
# max_iterations boundary
# --------------------------------------------------------------------------- #


async def test_max_iterations_zero_is_a_noop(fake_sleep: list[float]) -> None:
    """``max_iterations=0`` skips the loop body entirely — no claim, no
    runner. Pin the boundary so a future regression that flipped the
    comparison (``<=`` vs ``<``) surfaces here rather than silently
    burning a claim."""
    client = FakeRemoteClient()
    plan = _make_plan()

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover — never called
        raise AssertionError("runner must not be called when max_iterations=0")

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=0,
    )

    assert stats == RemoteWorkerStats()
    assert client.claim_calls == []


# --------------------------------------------------------------------------- #
# max_processed cap (TASK-022c — wires --once)
# --------------------------------------------------------------------------- #


async def test_max_processed_one_exits_after_first_completed(fake_sleep: list[float]) -> None:
    """``max_processed=1`` exits the loop right after one successful complete.

    AC for TASK-022c (--once flag). The loop scripts two PENDING tasks
    but the second claim must never be issued because the cap fires
    after the first complete. Idle polls are still possible *before* the
    first task arrives (handled by the next test) but here we get the
    happy single-task path.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-once", status=TaskStatus.CLAIMED, version=1)
    done = replace(claimed, status=TaskStatus.DONE, version=2)
    extra = _make_task("T-extra", status=TaskStatus.CLAIMED, version=1)

    client.claim_results.extend([claimed, extra])
    client.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_processed=1,
    )

    assert stats == RemoteWorkerStats(iterations=1, completed=1, failed=0, idle_polls=0)
    # Crucial: the second pending claim must not have been issued.
    assert client.claim_calls == [(WORKER_ID, PLAN_ID)]
    assert len(client.claim_results) == 1, "second scripted claim must remain unconsumed"


async def test_max_processed_one_exits_after_first_failed(fake_sleep: list[float]) -> None:
    """``max_processed=1`` honours fails too — completed + failed >= 1 wins.

    Symmetric with the completed test above; pinning fail-counts means a
    refactor that only checked ``completed`` would surface here. The AC
    for --once reads "одна задача" (one task) — terminal status counts
    regardless of which terminal status it landed in.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    claimed = _make_task("T-fail-once", status=TaskStatus.CLAIMED, version=1)
    failed_terminal = replace(claimed, status=TaskStatus.FAILED, version=2)

    client.claim_results.append(claimed)
    client.fail_results.append(failed_terminal)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="boom", exit_code=1, is_complete=False)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_processed=1,
    )

    assert stats == RemoteWorkerStats(iterations=1, completed=0, failed=1, idle_polls=0)
    assert len(client.fail_calls) == 1


async def test_max_processed_skips_idle_polls(fake_sleep: list[float]) -> None:
    """Idle polls do NOT count against ``max_processed``.

    A --once worker against an empty queue must keep polling until it
    actually owns a task — pinning this means the cap is "completed +
    failed" semantics, not "iterations". Without the test, a refactor
    that incremented the processed counter on idle polls would silently
    turn --once into "exit on the first 204".
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    # First two claims return None (idle), third returns a task. The
    # cap is 1, so we need to reach the third iteration before exit.
    claimed = _make_task("T-after-idles", status=TaskStatus.CLAIMED, version=3)
    done = replace(claimed, status=TaskStatus.DONE, version=4)
    client.claim_results.extend([None, None, claimed])
    client.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_processed=1,
    )

    assert stats == RemoteWorkerStats(iterations=3, completed=1, failed=0, idle_polls=2)
    assert len(client.claim_calls) == 3


async def test_max_processed_skips_lost_race_409(fake_sleep: list[float]) -> None:
    """A 409 lost race does NOT count as processed — the cap keeps trying.

    Pin the AC's "терминальный статус" semantics: only an actual write
    that the server accepted (complete or fail without 409) advances
    the cap. A 409 means another writer beat us; the worker must
    keep going until it owns a real outcome.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    first = _make_task("T-conflict", status=TaskStatus.CLAIMED, version=1)
    second = _make_task("T-success", status=TaskStatus.CLAIMED, version=1)
    second_done = replace(second, status=TaskStatus.DONE, version=2)

    client.claim_results.extend([first, second])
    # First complete loses the race; second succeeds.
    client.complete_results.extend(
        [
            _make_conflict(
                task_id=first.id,
                expected_version=1,
                actual_version=2,
                actual_status=TaskStatus.DONE,
            ),
            second_done,
        ]
    )

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_processed=1,
    )

    assert stats == RemoteWorkerStats(iterations=2, completed=1, failed=0, idle_polls=0)
    assert client.complete_calls == [(first.id, WORKER_ID, 1, 0.0), (second.id, WORKER_ID, 1, 0.0)]


async def test_max_processed_none_is_uncapped(fake_sleep: list[float]) -> None:
    """``max_processed=None`` (default) keeps the loop running past N completions.

    Defends against a default-flip regression where ``None`` accidentally
    behaves like ``1`` (e.g. if someone wrote ``max_processed or 1``).
    Two tasks complete; only ``max_iterations=2`` ends the loop.
    """
    client = FakeRemoteClient()
    plan = _make_plan()

    a = _make_task("T-a", status=TaskStatus.CLAIMED, version=1)
    a_done = replace(a, status=TaskStatus.DONE, version=2)
    b = _make_task("T-b", status=TaskStatus.CLAIMED, version=1)
    b_done = replace(b, status=TaskStatus.DONE, version=2)

    client.claim_results.extend([a, b])
    client.complete_results.extend([a_done, b_done])

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_remote_worker(
        client,  # type: ignore[arg-type]
        runner,
        plan,
        WORKER_ID,
        max_iterations=2,
        max_processed=None,
    )

    assert stats == RemoteWorkerStats(iterations=2, completed=2, failed=0, idle_polls=0)
