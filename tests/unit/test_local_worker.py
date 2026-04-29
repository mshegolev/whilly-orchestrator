"""Unit tests for :mod:`whilly.worker.local` (TASK-019a, PRD FR-1.6, FR-2.2).

What we cover
-------------
- Happy path: claim → start → run → complete bumps ``completed`` and the
  underlying repo records the right version per call.
- Failure path: a :class:`AgentResult` with non-zero exit OR
  ``is_complete=False`` triggers ``fail_task`` with the documented reason
  shape ``exit_code=<n>: <truncated stdout>``.
- Idle path: ``claim_task`` returning ``None`` increments ``idle_polls``
  and sleeps, but the loop still terminates when ``max_iterations`` is
  reached.
- VersionConflict at start / complete / fail is logged and the loop
  continues without crashing.
- The runner receives a prompt built from
  :func:`whilly.core.prompts.build_task_prompt` (smoke check: prompt
  contains the task id and the plan name).

How we isolate from real I/O
----------------------------
Tests use a hand-rolled :class:`FakeRepo` rather than ``unittest.mock``:
the surface is small (four async methods) and the in-memory state lets
us assert on call ordering and the actual ``(task_id, version)``
arguments without sprinkling ``mock.assert_called_with`` everywhere. The
runner is a small async closure that returns canned
:class:`AgentResult`\\s.

The ``asyncio.sleep`` patch (via ``monkeypatch``) keeps tests
millisecond-fast even when ``DEFAULT_IDLE_WAIT`` is in play, and
captures the durations so idle-path tests can assert the worker
actually slept.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from whilly.adapters.db.repository import VersionConflictError
from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.worker import local as worker_local
from whilly.worker.local import (
    DEFAULT_IDLE_WAIT,
    WorkerStats,
    _build_fail_reason,
    _truncate_output,
    run_local_worker,
)

# --------------------------------------------------------------------------- #
# Test fixtures and fakes
# --------------------------------------------------------------------------- #


WORKER_ID: WorkerId = "worker-test"
PLAN_ID = "plan-test"


def _make_task(
    task_id: str = "T-001",
    *,
    status: TaskStatus = TaskStatus.PENDING,
    version: int = 0,
) -> Task:
    """Build a task with realistic but minimal fields."""
    return Task(
        id=task_id,
        status=status,
        priority=Priority.MEDIUM,
        description=f"description for {task_id}",
        version=version,
    )


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="Test Plan")


class FakeRepo:
    """In-memory stand-in for :class:`whilly.adapters.db.repository.TaskRepository`.

    Stores canned per-method results and records every call. Per-method
    queues (``claim_results`` etc.) are popped left-to-right so a test can
    script multiple iterations precisely.

    Why not :class:`unittest.mock.AsyncMock`?
        AsyncMock would require ``return_value=...`` set per call and either
        an iterator side_effect or a custom subclass to script results
        across iterations. The hand-rolled fake gives us cleaner per-call
        assertions and forces tests to declare the exact transcript they
        expect.
    """

    def __init__(self) -> None:
        # Scripted return values for each method. Tests append to these
        # before invoking ``run_local_worker``. Each method pops from the
        # front; an empty queue means the test wired the fixture wrong and
        # we raise loudly so the bug is obvious.
        self.claim_results: list[Task | None] = []
        self.start_results: list[Task | VersionConflictError] = []
        self.complete_results: list[Task | VersionConflictError] = []
        self.fail_results: list[Task | VersionConflictError] = []

        # Recorded calls for assertion. Each entry captures the exact
        # arguments passed so tests can verify version threading.
        self.claim_calls: list[tuple[WorkerId, str]] = []
        self.start_calls: list[tuple[TaskId, int]] = []
        self.complete_calls: list[tuple[TaskId, int, object]] = []
        self.fail_calls: list[tuple[TaskId, int, str]] = []

    async def claim_task(self, worker_id: WorkerId, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        if not self.claim_results:
            raise AssertionError("FakeRepo.claim_task called more times than scripted")
        return self.claim_results.pop(0)

    async def start_task(self, task_id: TaskId, version: int) -> Task:
        self.start_calls.append((task_id, version))
        if not self.start_results:
            raise AssertionError("FakeRepo.start_task called more times than scripted")
        result = self.start_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def complete_task(
        self,
        task_id: TaskId,
        version: int,
        cost_usd: object = None,  # TASK-102: optional spend echo
    ) -> Task:
        self.complete_calls.append((task_id, version, cost_usd))
        if not self.complete_results:
            raise AssertionError("FakeRepo.complete_task called more times than scripted")
        result = self.complete_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def fail_task(self, task_id: TaskId, version: int, reason: str) -> Task:
        self.fail_calls.append((task_id, version, reason))
        if not self.fail_results:
            raise AssertionError("FakeRepo.fail_task called more times than scripted")
        result = self.fail_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Replace ``asyncio.sleep`` with a recorder so idle-path tests are fast.

    Returns the list the patched ``sleep`` appends to; assertions can
    check the durations and the count without timing dependencies.
    """
    sleeps: list[float] = []

    async def _fake(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(worker_local.asyncio, "sleep", _fake)
    yield sleeps


# --------------------------------------------------------------------------- #
# Module-level constants — pin the operator-visible defaults
# --------------------------------------------------------------------------- #


def test_default_idle_wait_is_one_second() -> None:
    """A second is short enough to feel live but doesn't hammer the DB —
    pin the value so a future tweak shows up in the diff."""
    assert DEFAULT_IDLE_WAIT == 1.0


def test_worker_stats_defaults_are_zero() -> None:
    """Empty stats are the natural zero so callers can compare equality."""
    assert WorkerStats() == WorkerStats(iterations=0, completed=0, failed=0, idle_polls=0)


# --------------------------------------------------------------------------- #
# _truncate_output / _build_fail_reason — pure helpers
# --------------------------------------------------------------------------- #


def test_truncate_output_passes_short_strings_through() -> None:
    """Anything below the cap should be returned verbatim."""
    assert _truncate_output("short") == "short"


def test_truncate_output_caps_long_strings_with_ellipsis() -> None:
    """Long strings get the cap + a single-char ellipsis marker so the
    ``events.payload`` row size stays bounded."""
    long = "x" * 1000
    truncated = _truncate_output(long)
    assert len(truncated) == worker_local._FAIL_REASON_OUTPUT_CAP + 1  # +1 for "…"
    assert truncated.endswith("…")


def test_build_fail_reason_includes_exit_code_and_snippet() -> None:
    result = AgentResult(output="boom", exit_code=42)
    reason = _build_fail_reason(result)
    assert reason == "exit_code=42: boom"


def test_build_fail_reason_omits_empty_snippet() -> None:
    """Empty stdout (binary not found, spawn blocked) — just exit code."""
    result = AgentResult(output="", exit_code=-2)
    reason = _build_fail_reason(result)
    assert reason == "exit_code=-2"


def test_build_fail_reason_strips_whitespace_in_snippet() -> None:
    """Whitespace-only output should be treated as empty."""
    result = AgentResult(output="   \n\n   ", exit_code=1)
    assert _build_fail_reason(result) == "exit_code=1"


# --------------------------------------------------------------------------- #
# Happy path — claim → start → run → complete
# --------------------------------------------------------------------------- #


async def test_completes_one_task_happy_path(fake_sleep: list[float]) -> None:
    repo = FakeRepo()
    plan = _make_plan()

    pending = _make_task("T-001", status=TaskStatus.PENDING, version=0)
    claimed = replace(pending, status=TaskStatus.CLAIMED, version=1)
    running = replace(claimed, status=TaskStatus.IN_PROGRESS, version=2)
    done = replace(running, status=TaskStatus.DONE, version=3)

    repo.claim_results.append(claimed)
    repo.start_results.append(running)
    repo.complete_results.append(done)

    captured_prompt: list[str] = []

    async def runner(task: Task, prompt: str) -> AgentResult:
        captured_prompt.append(prompt)
        assert task.id == "T-001"
        return AgentResult(
            output="all good <promise>COMPLETE</promise>",
            exit_code=0,
            is_complete=True,
        )

    stats = await run_local_worker(
        repo,  # type: ignore[arg-type]  # FakeRepo duck-types TaskRepository
        runner,
        plan,
        WORKER_ID,
        idle_wait=0,
        max_iterations=1,
    )

    assert stats == WorkerStats(iterations=1, completed=1, failed=0, idle_polls=0)
    assert repo.claim_calls == [(WORKER_ID, PLAN_ID)]
    assert repo.start_calls == [("T-001", 1)]
    assert repo.complete_calls == [("T-001", 2, 0.0)]
    assert repo.fail_calls == []
    # Prompt smoke check: the task id and plan name are inside the rendered text.
    assert len(captured_prompt) == 1
    assert "T-001" in captured_prompt[0]
    assert "Test Plan" in captured_prompt[0]


async def test_versions_thread_through_state_transitions(fake_sleep: list[float]) -> None:
    """Each repo method receives the version that the previous step
    returned — proves we're not accidentally feeding stale versions."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed = _make_task("T-9", status=TaskStatus.CLAIMED, version=7)
    running = replace(claimed, status=TaskStatus.IN_PROGRESS, version=8)
    done = replace(running, status=TaskStatus.DONE, version=9)

    repo.claim_results.append(claimed)
    repo.start_results.append(running)
    repo.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=1)  # type: ignore[arg-type]

    # start receives the post-claim version (7); complete receives the post-start version (8).
    assert repo.start_calls == [("T-9", 7)]
    assert repo.complete_calls == [("T-9", 8, 0.0)]


# --------------------------------------------------------------------------- #
# Failure path — non-zero exit or no completion marker → fail_task
# --------------------------------------------------------------------------- #


async def test_fails_task_when_agent_returns_nonzero_exit(fake_sleep: list[float]) -> None:
    repo = FakeRepo()
    plan = _make_plan()

    claimed = _make_task("T-001", status=TaskStatus.CLAIMED, version=1)
    running = replace(claimed, status=TaskStatus.IN_PROGRESS, version=2)
    failed = replace(running, status=TaskStatus.FAILED, version=3)

    repo.claim_results.append(claimed)
    repo.start_results.append(running)
    repo.fail_results.append(failed)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="exploded", exit_code=1, is_complete=False)

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=1)  # type: ignore[arg-type]

    assert stats.failed == 1
    assert stats.completed == 0
    assert repo.complete_calls == []
    assert len(repo.fail_calls) == 1
    task_id, version, reason = repo.fail_calls[0]
    assert task_id == "T-001"
    assert version == 2
    assert reason == "exit_code=1: exploded"


async def test_fails_task_when_completion_marker_missing(fake_sleep: list[float]) -> None:
    """exit_code=0 alone is NOT enough — the agent must explicitly
    signal completion via :data:`COMPLETION_MARKER`. Otherwise we treat
    it as a graceful agent giveup and mark FAILED."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed = _make_task("T-2", status=TaskStatus.CLAIMED, version=1)
    running = replace(claimed, status=TaskStatus.IN_PROGRESS, version=2)
    failed = replace(running, status=TaskStatus.FAILED, version=3)

    repo.claim_results.append(claimed)
    repo.start_results.append(running)
    repo.fail_results.append(failed)

    async def runner(task: Task, prompt: str) -> AgentResult:
        # Notice: exit 0 but is_complete=False (no marker in output).
        return AgentResult(output="I cannot proceed", exit_code=0, is_complete=False)

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=1)  # type: ignore[arg-type]

    assert stats.failed == 1
    assert repo.complete_calls == []
    assert repo.fail_calls[0][2] == "exit_code=0: I cannot proceed"


async def test_fails_task_truncates_long_output_in_reason(fake_sleep: list[float]) -> None:
    """A megabyte stdout must not bloat the audit row."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed = _make_task("T-big", status=TaskStatus.CLAIMED, version=1)
    running = replace(claimed, status=TaskStatus.IN_PROGRESS, version=2)
    failed = replace(running, status=TaskStatus.FAILED, version=3)
    repo.claim_results.append(claimed)
    repo.start_results.append(running)
    repo.fail_results.append(failed)

    huge = "X" * 5000

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output=huge, exit_code=1)

    await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=1)  # type: ignore[arg-type]

    reason = repo.fail_calls[0][2]
    # Reason carries the prefix + cap + ellipsis; never the full 5000 chars.
    assert reason.startswith("exit_code=1: ")
    assert reason.endswith("…")
    assert len(reason) < len(huge)


# --------------------------------------------------------------------------- #
# Idle path — claim returns None → sleep → poll
# --------------------------------------------------------------------------- #


async def test_idle_loop_sleeps_and_continues(fake_sleep: list[float]) -> None:
    """Three idle iterations should sleep three times with the configured
    ``idle_wait`` and never call ``start_task`` / ``complete_task`` /
    ``fail_task``."""
    repo = FakeRepo()
    plan = _make_plan()
    repo.claim_results.extend([None, None, None])

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover — never invoked
        raise AssertionError("runner must not be called when no task was claimed")

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0.5, max_iterations=3)  # type: ignore[arg-type]

    assert stats == WorkerStats(iterations=3, completed=0, failed=0, idle_polls=3)
    assert fake_sleep == [0.5, 0.5, 0.5]
    assert repo.start_calls == []
    assert repo.complete_calls == []
    assert repo.fail_calls == []


async def test_mixed_idle_and_work_iterations(fake_sleep: list[float]) -> None:
    """Iter 1: idle. Iter 2: claim + complete. Iter 3: idle. Verifies
    counters increment in the right buckets across mixed iterations."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed = _make_task("T-3", status=TaskStatus.CLAIMED, version=1)
    running = replace(claimed, status=TaskStatus.IN_PROGRESS, version=2)
    done = replace(running, status=TaskStatus.DONE, version=3)

    repo.claim_results.extend([None, claimed, None])
    repo.start_results.append(running)
    repo.complete_results.append(done)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=3)  # type: ignore[arg-type]

    assert stats == WorkerStats(iterations=3, completed=1, failed=0, idle_polls=2)


# --------------------------------------------------------------------------- #
# Version-conflict handling — log + continue, never crash
# --------------------------------------------------------------------------- #


def _conflict(task_id: TaskId) -> VersionConflictError:
    """Build a ``"version moved"`` conflict — the most common race."""
    return VersionConflictError(
        task_id=task_id,
        expected_version=1,
        actual_version=99,
        actual_status=TaskStatus.PENDING,
    )


async def test_start_task_conflict_drops_and_continues(fake_sleep: list[float]) -> None:
    """When ``start_task`` loses the race we abandon the task and move on
    — we do NOT call complete or fail (we never owned the IN_PROGRESS
    state, so writing FAILED would corrupt audit history)."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed = _make_task("T-3", status=TaskStatus.CLAIMED, version=1)
    repo.claim_results.extend([claimed, None])
    repo.start_results.append(_conflict("T-3"))

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("runner must not run when start_task lost the race")

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=2)  # type: ignore[arg-type]

    assert stats.completed == 0
    assert stats.failed == 0
    assert stats.idle_polls == 1
    assert repo.complete_calls == []
    assert repo.fail_calls == []


async def test_complete_task_conflict_logs_and_continues(fake_sleep: list[float]) -> None:
    """A conflict at complete time is logged but doesn't abort the loop —
    the next iteration must keep working."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed1 = _make_task("T-1", status=TaskStatus.CLAIMED, version=1)
    running1 = replace(claimed1, status=TaskStatus.IN_PROGRESS, version=2)
    claimed2 = _make_task("T-2", status=TaskStatus.CLAIMED, version=1)
    running2 = replace(claimed2, status=TaskStatus.IN_PROGRESS, version=2)
    done2 = replace(running2, status=TaskStatus.DONE, version=3)

    repo.claim_results.extend([claimed1, claimed2])
    repo.start_results.extend([running1, running2])
    repo.complete_results.append(_conflict("T-1"))
    repo.complete_results.append(done2)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=2)  # type: ignore[arg-type]

    assert stats.completed == 1  # T-2 survived; T-1 lost the race
    assert stats.failed == 0
    assert repo.complete_calls == [("T-1", 2, 0.0), ("T-2", 2, 0.0)]


async def test_fail_task_conflict_logs_and_continues(fake_sleep: list[float]) -> None:
    """Same shape as the complete-conflict test, but on the fail path."""
    repo = FakeRepo()
    plan = _make_plan()

    claimed1 = _make_task("T-1", status=TaskStatus.CLAIMED, version=1)
    running1 = replace(claimed1, status=TaskStatus.IN_PROGRESS, version=2)
    claimed2 = _make_task("T-2", status=TaskStatus.CLAIMED, version=1)
    running2 = replace(claimed2, status=TaskStatus.IN_PROGRESS, version=2)
    failed2 = replace(running2, status=TaskStatus.FAILED, version=3)

    repo.claim_results.extend([claimed1, claimed2])
    repo.start_results.extend([running1, running2])
    repo.fail_results.append(_conflict("T-1"))
    repo.fail_results.append(failed2)

    async def runner(task: Task, prompt: str) -> AgentResult:
        return AgentResult(output="oops", exit_code=1)

    stats = await run_local_worker(repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=2)  # type: ignore[arg-type]

    assert stats.completed == 0
    assert stats.failed == 1
    assert len(repo.fail_calls) == 2  # both attempts recorded
