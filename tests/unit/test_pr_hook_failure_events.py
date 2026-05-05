"""Failure-path coverage for the post-COMPLETE PR opener hook (VAL-PR-022, VAL-PR-023).

Pins the contract that:

* On ``gh pr create`` non-zero exit OR ``subprocess.TimeoutExpired``:
  the hook emits exactly one ``pr.open_failed`` event whose ``detail``
  carries ``task_id`` plus ``gh_exit_code`` plus ``failure_mode``;
  no ``pr.opened`` event; no successful ``pull_requests`` row.

* On ``git push --force-with-lease`` non-zero exit: the hook
  short-circuits before ``gh pr create``, emits exactly one
  ``pr.open_failed`` event whose ``detail`` carries ``task_id`` plus
  ``push_exit_code`` plus ``failure_mode``; no ``pull_requests``
  row; no ``pr.opened`` event.

The repository is faked as an in-memory recorder so the test does
not need a real Postgres pool — the hook contract is pure dispatch
to ``insert_pull_request`` / ``emit_pr_event``.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from whilly.adapters.db.repository import (
    PR_OPEN_FAILED_EVENT_TYPE,
    PR_OPENED_EVENT_TYPE,
)
from whilly.sinks import github_pr as gp
from whilly.sinks.github_pr import open_pr_for_task
from whilly.sinks.post_complete_pr_hook import run_post_complete_pr_hook
from whilly.task_manager import Task

PLAN_ID = "PLAN-PR-HOOK-FAIL"
ISSUE_REF = "foo/bar/42"


@dataclass
class _FakeRepo:
    """In-memory stand-in for ``TaskRepository`` covering the hook's surface."""

    github_issue_ref: str | None = ISSUE_REF
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    async def get_plan_github_issue_ref(self, plan_id: str) -> str | None:  # noqa: ARG002
        return self.github_issue_ref

    async def insert_pull_request(self, **kwargs: Any) -> int:
        self.pull_requests.append(dict(kwargs))
        return 1

    async def emit_pr_event(
        self,
        event_type: str,
        *,
        plan_id: str,
        task_id: str,
        payload: dict[str, Any],
    ) -> int:
        self.events.append(
            {
                "event_type": event_type,
                "plan_id": plan_id,
                "task_id": task_id,
                "payload": dict(payload),
            }
        )
        return len(self.events)


def _make_task() -> Task:
    return Task(
        id="T-PR-HOOK-FAIL-1",
        phase="GH-Issues",
        category="github-issue",
        priority="medium",
        description="Add /health endpoint returning ok",
        status="done",
        dependencies=[],
        key_files=[],
        acceptance_criteria=["GET /health returns 200"],
        test_steps=[],
        prd_requirement="https://github.com/foo/bar/issues/42",
    )


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# git push failure → short-circuit, no gh pr create, single failure event
# ---------------------------------------------------------------------------


def test_git_push_failure_short_circuits_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    repo = _FakeRepo()
    push = _Proc(128, "", "fatal: protected branch\n")
    captured: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured.append(list(cmd))
        return push

    with patch.object(gp, "_run", side_effect=fake_run):
        asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert captured, "expected at least one subprocess invocation"
    assert all(cmd[0] == "git" for cmd in captured), f"gh pr create was invoked despite push failure: {captured!r}"
    gh_invocations = [cmd for cmd in captured if cmd[0] == "gh"]
    assert gh_invocations == [], f"unexpected gh invocations: {gh_invocations!r}"

    assert repo.pull_requests == []
    failure_events = [e for e in repo.events if e["event_type"] == PR_OPEN_FAILED_EVENT_TYPE]
    success_events = [e for e in repo.events if e["event_type"] == PR_OPENED_EVENT_TYPE]
    assert len(failure_events) == 1, f"expected exactly one failure event, got {repo.events!r}"
    assert success_events == []
    payload = failure_events[0]["payload"]
    assert payload["task_id"] == "T-PR-HOOK-FAIL-1"
    assert payload["push_exit_code"] == 128
    assert payload["failure_mode"] == "git_push_failed"


# ---------------------------------------------------------------------------
# gh pr create failure → push happened, single failure event with gh exit code
# ---------------------------------------------------------------------------


def test_gh_pr_create_failure_emits_warning_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    repo = _FakeRepo()
    push = _Proc(0)
    pr = _Proc(2, "", "validation failed\n")

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        return push if cmd[0] == "git" else pr

    with patch.object(gp, "_run", side_effect=fake_run):
        asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert repo.pull_requests == []
    failure_events = [e for e in repo.events if e["event_type"] == PR_OPEN_FAILED_EVENT_TYPE]
    success_events = [e for e in repo.events if e["event_type"] == PR_OPENED_EVENT_TYPE]
    assert len(failure_events) == 1
    assert success_events == []
    payload = failure_events[0]["payload"]
    assert payload["task_id"] == "T-PR-HOOK-FAIL-1"
    assert payload["gh_exit_code"] == 2
    assert payload["failure_mode"] == "gh_pr_create_failed"


# ---------------------------------------------------------------------------
# gh pr create timeout → single failure event with failure_mode=*timeout
# ---------------------------------------------------------------------------


def test_gh_pr_create_timeout_emits_warning_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    repo = _FakeRepo()
    push = _Proc(0)

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        if cmd[0] == "git":
            return push
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with patch.object(gp, "_run", side_effect=fake_run):
        asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert repo.pull_requests == []
    failure_events = [e for e in repo.events if e["event_type"] == PR_OPEN_FAILED_EVENT_TYPE]
    assert len(failure_events) == 1, f"got {repo.events!r}"
    payload = failure_events[0]["payload"]
    assert payload["task_id"] == "T-PR-HOOK-FAIL-1"
    assert payload["failure_mode"] == "gh_pr_create_timeout"


# ---------------------------------------------------------------------------
# Env-var unset → hook is a no-op (no events, no rows, no subprocess)
# ---------------------------------------------------------------------------


def test_hook_skipped_when_env_var_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHILLY_AUTO_OPEN_PR", raising=False)
    repo = _FakeRepo()
    captured: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured.append(list(cmd))
        return _Proc(0)

    with patch.object(gp, "_run", side_effect=fake_run):
        result = asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert result is None
    assert captured == []
    assert repo.events == []
    assert repo.pull_requests == []


def test_hook_skipped_when_env_var_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "0")
    repo = _FakeRepo()
    captured: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured.append(list(cmd))
        return _Proc(0)

    with patch.object(gp, "_run", side_effect=fake_run):
        result = asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert result is None
    assert captured == []
    assert repo.events == []


# ---------------------------------------------------------------------------
# github_issue_ref NULL → hook skipped, no warning event
# ---------------------------------------------------------------------------


def test_hook_skipped_when_plan_has_no_issue_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    repo = _FakeRepo(github_issue_ref=None)
    captured: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured.append(list(cmd))
        return _Proc(0)

    with patch.object(gp, "_run", side_effect=fake_run):
        result = asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert result is None
    assert captured == []
    assert repo.events == [], f"warning event leaked when github_issue_ref is NULL: {repo.events!r}"
    assert repo.pull_requests == []


# ---------------------------------------------------------------------------
# Happy path → row + pr.opened event with documented detail keys
# ---------------------------------------------------------------------------


def test_hook_success_persists_row_and_emits_pr_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    repo = _FakeRepo()
    push = _Proc(0)
    pr = _Proc(0, "https://github.com/foo/bar/pull/77\n")

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        return push if cmd[0] == "git" else pr

    with patch.object(gp, "_run", side_effect=fake_run):
        result = asyncio.run(
            run_post_complete_pr_hook(
                repo,
                plan_id=PLAN_ID,
                task=_make_task(),
                worktree_path=tmp_path,
                opener=open_pr_for_task,
            )
        )

    assert result is not None and result.ok
    assert len(repo.pull_requests) == 1
    row = repo.pull_requests[0]
    assert row["pr_number"] == 77
    assert row["pr_url"] == "https://github.com/foo/bar/pull/77"
    assert row["task_id"] == "T-PR-HOOK-FAIL-1"
    assert row["state"] == "open"

    success_events = [e for e in repo.events if e["event_type"] == PR_OPENED_EVENT_TYPE]
    failure_events = [e for e in repo.events if e["event_type"] == PR_OPEN_FAILED_EVENT_TYPE]
    assert len(success_events) == 1, f"expected one pr.opened, got {repo.events!r}"
    assert failure_events == []
    payload = success_events[0]["payload"]
    for key in ("pr_url", "pr_number", "branch", "head_sha", "task_id"):
        assert key in payload, f"detail missing key {key!r}: {payload!r}"
    assert payload["pr_url"] == "https://github.com/foo/bar/pull/77"
    assert payload["pr_number"] == 77
    assert payload["task_id"] == "T-PR-HOOK-FAIL-1"
