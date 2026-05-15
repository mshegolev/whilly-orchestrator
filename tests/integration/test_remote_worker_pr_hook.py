"""Integration tests for the remote-worker PR/MR hook (WHILLY_AUTO_OPEN_PR).

What we cover
-------------
1. Happy path (gitlab): WHILLY_AUTO_OPEN_PR=1 + WHILLY_PR_PROVIDER=gitlab → opens MR,
   records pr.opened on the server, task lands DONE.
2. Gating off: WHILLY_AUTO_OPEN_PR unset → no record_pull_request call, no pr.opened,
   task still DONE.
3. Opener returns ok=False → task still DONE, no record_pull_request call, no exception.
4. record_pull_request raises → worker loop survives, task still DONE, no exception leaks.

Test strategy
-------------
All tests drive the *real* ``run_remote_worker`` loop against a hand-rolled
:class:`FakeRemoteClient` (same pattern as ``tests/unit/test_remote_worker.py``) so
we never need a running FastAPI server or database. The monkeypatch fixture substitutes
the sink functions and the record_pull_request method so we stay purely in-memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.adapters.transport.client import VersionConflictError
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus, WorkerId
from whilly.sinks.github_pr import PRResult
from whilly.worker.remote import RemoteWorkerStats, run_remote_worker

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

WORKER_ID: WorkerId = "w-pr-hook-test"
PLAN_ID = "PLAN-PR-HOOK-TEST"

_GITLAB_MR_URL = "https://gitlab.example.com/foo/bar/-/merge_requests/1"
_GITHUB_PR_URL = "https://github.com/foo/bar/pull/2"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_task(task_id: str = "T-PR-1", *, status: TaskStatus = TaskStatus.CLAIMED, version: int = 1) -> Task:
    return Task(
        id=task_id,
        status=status,
        priority=Priority.MEDIUM,
        description=f"description for {task_id}",
        version=version,
    )


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="PR Hook Test Plan")


def _make_conflict(
    *,
    task_id: TaskId | None,
    expected_version: int,
    actual_version: int | None,
    actual_status: TaskStatus | None,
) -> VersionConflictError:
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


def _ok_result() -> AgentResult:
    """Agent result that carries the COMPLETE signal."""
    return AgentResult(
        is_complete=True,
        exit_code=0,
        output="<promise>COMPLETE</promise>",
        usage=AgentUsage(input_tokens=10, output_tokens=5, cost_usd=0.001),
    )


# --------------------------------------------------------------------------- #
# Fake client
# --------------------------------------------------------------------------- #


@dataclass
class FakeRemoteClient:
    """Minimal in-memory stand-in for :class:`RemoteWorkerClient`.

    Mirrors the fixture from ``tests/unit/test_remote_worker.py`` but adds
    ``record_pull_request`` tracking for the PR hook assertions.
    """

    claim_results: list[Task | None] = field(default_factory=list)
    complete_results: list[Task | VersionConflictError] = field(default_factory=list)
    fail_results: list[Task | VersionConflictError] = field(default_factory=list)
    release_results: list[Task | VersionConflictError] = field(default_factory=list)
    control_state_results: list[bool] = field(default_factory=list)

    claim_calls: list[tuple[str, str]] = field(default_factory=list)
    complete_calls: list[tuple[TaskId, str, int, object]] = field(default_factory=list)
    fail_calls: list[tuple[TaskId, str, int, str]] = field(default_factory=list)
    event_calls: list[tuple[TaskId, str, str, dict[str, object], dict[str, object] | None]] = field(
        default_factory=list
    )
    list_task_events_calls: list[tuple[TaskId, str | None]] = field(default_factory=list)
    # PR hook recording
    record_pull_request_calls: list[dict[str, Any]] = field(default_factory=list)
    record_pull_request_side_effect: Exception | None = None

    async def control_state(self) -> object:
        paused = self.control_state_results.pop(0) if self.control_state_results else False
        return SimpleNamespace(paused=paused)

    async def claim(self, worker_id: str, plan_id: str) -> Task | None:
        self.claim_calls.append((worker_id, plan_id))
        if not self.claim_results:
            raise AssertionError("FakeRemoteClient.claim called more than scripted")
        return self.claim_results.pop(0)

    async def complete(
        self,
        task_id: TaskId,
        worker_id: str,
        version: int,
        cost_usd: object = None,
    ) -> object:
        self.complete_calls.append((task_id, worker_id, version, cost_usd))
        if not self.complete_results:
            raise AssertionError("FakeRemoteClient.complete called more than scripted")
        result = self.complete_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def fail(
        self,
        task_id: TaskId,
        worker_id: str,
        version: int,
        reason: str,
        *,
        detail: dict[str, object] | None = None,
    ) -> object:
        self.fail_calls.append((task_id, worker_id, version, reason))
        if not self.fail_results:
            raise AssertionError("FakeRemoteClient.fail called more than scripted")
        result = self.fail_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def release(
        self,
        task_id: TaskId,
        worker_id: str,
        version: int,
        reason: str,
    ) -> object:
        self.release_calls: list[object] = []
        if not self.release_results:
            raise AssertionError("FakeRemoteClient.release called more than scripted")
        result = self.release_results.pop(0)
        if isinstance(result, VersionConflictError):
            raise result
        return result

    async def heartbeat(self, worker_id: str) -> object:
        return SimpleNamespace(ok=True)

    async def record_event(
        self,
        task_id: TaskId,
        worker_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        detail: dict[str, object] | None = None,
    ) -> object:
        self.event_calls.append((task_id, worker_id, event_type, payload or {}, detail))
        return object()

    async def list_task_events(self, task_id: TaskId, event_prefix: str | None = None) -> tuple[object, ...]:
        self.list_task_events_calls.append((task_id, event_prefix))
        return ()

    async def record_pull_request(self, **kwargs: Any) -> dict[str, Any]:
        self.record_pull_request_calls.append(kwargs)
        if self.record_pull_request_side_effect is not None:
            raise self.record_pull_request_side_effect
        return {"id": "pr-row-1", **kwargs}

    async def request_repair(
        self,
        task_id: TaskId,
        worker_id: str,
        version: int,
        repair_task: dict[str, object],
        event: dict[str, object],
    ) -> str:
        return str(repair_task.get("id", "repair-1"))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Suppress asyncio.sleep in the worker loop so tests run fast."""
    sleeps: list[float] = []

    async def _fake(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    yield sleeps


def _script_single_completion(client: FakeRemoteClient, task_id: str = "T-PR-1") -> Task:
    """Script one claim→complete cycle on *client* and return the claimed task."""
    claimed = _make_task(task_id, status=TaskStatus.CLAIMED, version=1)
    done = replace(claimed, status=TaskStatus.DONE, version=2)
    client.claim_results.append(claimed)
    client.complete_results.append(done)
    return claimed


async def _run_one(client: FakeRemoteClient, monkeypatch: pytest.MonkeyPatch) -> RemoteWorkerStats:
    """Drive the worker for exactly 1 processed task via a canned runner."""

    async def _runner(task: Task, prompt: str) -> AgentResult:
        return _ok_result()

    return await run_remote_worker(client, _runner, _make_plan(), WORKER_ID, max_processed=1)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_hook_gitlab_happy_path(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """Happy path: WHILLY_AUTO_OPEN_PR=1 + WHILLY_PR_PROVIDER=gitlab opens MR and records it."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv("WHILLY_PR_PROVIDER", "gitlab")

    mr_result = PRResult(
        ok=True,
        pr_url=_GITLAB_MR_URL,
        branch="whilly/test-task",
        pr_number=1,
        head_sha="abc123",
        failure_mode="",
        reason="",
    )
    monkeypatch.setattr(
        "whilly.sinks.gitlab_mr.open_mr_for_task",
        lambda **_kwargs: mr_result,
    )

    client = FakeRemoteClient()
    _script_single_completion(client)

    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert stats.failed == 0
    # record_pull_request was called exactly once
    assert len(client.record_pull_request_calls) == 1
    call = client.record_pull_request_calls[0]
    assert call["pr_url"] == _GITLAB_MR_URL
    assert call["pr_number"] == 1
    assert call["provider"] == "gitlab"
    assert call["branch"] == "whilly/test-task"
    assert call["head_sha"] == "abc123"
    assert call["task_id"] == "T-PR-1"
    assert call["plan_id"] == PLAN_ID
    assert call["worker_id"] == WORKER_ID
    # The record_pull_request mock acts as a server — we don't emit pr.opened ourselves;
    # the server endpoint does. We only verify the RPC was called correctly.
    # (The server-side pr.opened assertion belongs to server-side tests.)


@pytest.mark.asyncio
async def test_pr_hook_gating_off_when_env_unset(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """WHILLY_AUTO_OPEN_PR unset → no record_pull_request call, task still DONE."""
    monkeypatch.delenv("WHILLY_AUTO_OPEN_PR", raising=False)

    client = FakeRemoteClient()
    _script_single_completion(client)

    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert client.record_pull_request_calls == []


@pytest.mark.asyncio
async def test_pr_hook_gating_off_when_env_zero(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """WHILLY_AUTO_OPEN_PR=0 → no record_pull_request call, task still DONE."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "0")

    client = FakeRemoteClient()
    _script_single_completion(client)

    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert client.record_pull_request_calls == []


@pytest.mark.asyncio
async def test_pr_hook_opener_fails_gracefully(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """Opener returns ok=False → task DONE, no record_pull_request, no exception."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv("WHILLY_PR_PROVIDER", "gitlab")

    fail_result = PRResult(
        ok=False,
        pr_url="",
        branch="whilly/test-task",
        pr_number=None,
        head_sha=None,
        failure_mode="git_push_failed",
        reason="permission denied",
    )
    monkeypatch.setattr(
        "whilly.sinks.gitlab_mr.open_mr_for_task",
        lambda **_kwargs: fail_result,
    )

    client = FakeRemoteClient()
    _script_single_completion(client)

    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert client.record_pull_request_calls == []


@pytest.mark.asyncio
async def test_pr_hook_opener_crash_is_swallowed(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """Opener raises an exception → task DONE, no record_pull_request, loop survives."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv("WHILLY_PR_PROVIDER", "github")

    monkeypatch.setattr(
        "whilly.sinks.github_pr.open_pr_for_task",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("subprocess died")),
    )

    client = FakeRemoteClient()
    _script_single_completion(client)

    # Must not raise
    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert client.record_pull_request_calls == []


@pytest.mark.asyncio
async def test_pr_hook_server_record_fails_gracefully(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """record_pull_request raises → worker loop survives, task still DONE."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv("WHILLY_PR_PROVIDER", "gitlab")

    mr_result = PRResult(
        ok=True,
        pr_url=_GITLAB_MR_URL,
        branch="whilly/test-task",
        pr_number=1,
        head_sha="abc123",
        failure_mode="",
        reason="",
    )
    monkeypatch.setattr(
        "whilly.sinks.gitlab_mr.open_mr_for_task",
        lambda **_kwargs: mr_result,
    )

    client = FakeRemoteClient()
    client.record_pull_request_side_effect = RuntimeError("server unavailable")
    _script_single_completion(client)

    # Must not raise despite the server record failure
    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    # The call was attempted (once) even though it raised
    assert len(client.record_pull_request_calls) == 1


@pytest.mark.asyncio
async def test_pr_hook_unknown_provider_skipped(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """WHILLY_PR_PROVIDER=bitbucket → warning logged, no opener called, task DONE."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv("WHILLY_PR_PROVIDER", "bitbucket")

    client = FakeRemoteClient()
    _script_single_completion(client)

    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert client.record_pull_request_calls == []


@pytest.mark.asyncio
async def test_pr_hook_github_happy_path(monkeypatch: pytest.MonkeyPatch, fake_sleep: list[float]) -> None:
    """WHILLY_PR_PROVIDER=github (or default) → open_pr_for_task called, record sent."""
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv("WHILLY_PR_PROVIDER", "github")

    pr_result = PRResult(
        ok=True,
        pr_url=_GITHUB_PR_URL,
        branch="whilly/test-task",
        pr_number=2,
        head_sha="def456",
        failure_mode="",
        reason="",
    )
    monkeypatch.setattr(
        "whilly.sinks.github_pr.open_pr_for_task",
        lambda **_kwargs: pr_result,
    )

    client = FakeRemoteClient()
    _script_single_completion(client)

    stats = await _run_one(client, monkeypatch)

    assert stats.completed == 1
    assert len(client.record_pull_request_calls) == 1
    call = client.record_pull_request_calls[0]
    assert call["pr_url"] == _GITHUB_PR_URL
    assert call["pr_number"] == 2
    assert call["provider"] == "github"
