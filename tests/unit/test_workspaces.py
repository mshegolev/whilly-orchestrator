"""Unit tests for repository-target workspace preparation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from whilly.core.models import Plan, Priority, RepoTarget, Task, TaskStatus
from whilly.workspaces import (
    RepoTargetWorkspaceResolver,
    ResolvedWorkspace,
    WORKSPACE_PREPARED_EVENT_TYPE,
    prepare_git_workspace,
)


def _task(task_id: str = "TASK-1", *, repo_target_id: str = "") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        description="test",
        repo_target_id=repo_target_id,
    )


def _plan(plan_id: str = "PLAN-1") -> Plan:
    return Plan(id=plan_id, name="Test plan")


def _target(*, clone_url: str = "") -> RepoTarget:
    return RepoTarget(
        id="github:owner/repo",
        provider="github",
        repo_full_name="owner/repo",
        clone_url=clone_url,
        default_branch="main",
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc


def _seed_source_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "checkout", "-b", "main")
    _git(path, "config", "user.email", "whilly-tests@example.invalid")
    _git(path, "config", "user.name", "Whilly Tests")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial commit")
    return path


class _FakeRepo:
    def __init__(self, target: RepoTarget | None = None) -> None:
        self.target = target
        self.events: list[dict[str, Any]] = []

    async def get_repo_target(self, repo_target_id: str) -> RepoTarget | None:
        if self.target is not None and self.target.id == repo_target_id:
            return self.target
        return None

    async def record_task_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "task_id": task_id,
                "event_type": event_type,
                "payload": payload,
                "detail": detail,
            }
        )


async def test_resolver_reuses_current_cwd_for_legacy_task(tmp_path: Path) -> None:
    repo = _FakeRepo()
    resolver = RepoTargetWorkspaceResolver(repo, current_cwd=tmp_path)

    workspace = await resolver.prepare(_task(repo_target_id=""), _plan())

    assert workspace.path == tmp_path.resolve()
    assert workspace.reused_current_cwd is True
    assert workspace.repo_target_id == ""
    assert repo.events == []


async def test_resolver_records_workspace_event_for_repo_target(tmp_path: Path) -> None:
    source = _seed_source_repo(tmp_path / "source")
    target = _target(clone_url=str(source))
    repo = _FakeRepo(target)
    expected_workspace = ResolvedWorkspace(
        path=(tmp_path / "workspaces" / "github-owner-repo" / "PLAN-1" / "TASK-1").resolve(),
        repo_target_id=target.id,
        repo_full_name=target.repo_full_name,
        branch="whilly/PLAN-1/TASK-1",
    )

    resolver = RepoTargetWorkspaceResolver(repo, base_dir=tmp_path / "workspaces")

    workspace = await resolver.prepare(_task(repo_target_id=target.id), _plan())

    assert workspace == expected_workspace
    assert (workspace.path / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert repo.events == [
        {
            "task_id": "TASK-1",
            "event_type": WORKSPACE_PREPARED_EVENT_TYPE,
            "payload": {
                "repo_target_id": target.id,
                "repo_full_name": target.repo_full_name,
                "branch": "whilly/PLAN-1/TASK-1",
            },
            "detail": {
                "repo_target_id": target.id,
                "repo_full_name": target.repo_full_name,
                "workspace_path": str(expected_workspace.path),
                "branch": "whilly/PLAN-1/TASK-1",
                "reused_current_cwd": False,
            },
        }
    ]


async def test_resolver_fails_when_repo_target_is_missing(tmp_path: Path) -> None:
    resolver = RepoTargetWorkspaceResolver(_FakeRepo(), base_dir=tmp_path)

    with pytest.raises(RuntimeError, match="not registered"):
        await resolver.prepare(_task(repo_target_id="github:missing/repo"), _plan())


def test_prepare_git_workspace_clones_local_repo_and_checks_out_task_branch(tmp_path: Path) -> None:
    source = _seed_source_repo(tmp_path / "source")
    workspace = prepare_git_workspace(
        _target(clone_url=str(source)),
        plan_id="PLAN/1",
        task_id="TASK:1",
        base_dir=tmp_path / "work",
    )

    assert workspace.path.exists()
    assert workspace.repo_target_id == "github:owner/repo"
    assert workspace.branch == "whilly/PLAN-1/TASK-1"
    assert (workspace.path / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert _git(workspace.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == workspace.branch


def test_prepare_git_workspace_rejects_dirty_existing_checkout(tmp_path: Path) -> None:
    source = _seed_source_repo(tmp_path / "source")
    target = _target(clone_url=str(source))
    workspace = prepare_git_workspace(target, plan_id="PLAN-1", task_id="TASK-1", base_dir=tmp_path / "work")
    (workspace.path / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        prepare_git_workspace(target, plan_id="PLAN-1", task_id="TASK-1", base_dir=tmp_path / "work")


def test_prepare_git_workspace_rejects_existing_non_git_path(tmp_path: Path) -> None:
    expected_path = tmp_path / "work" / "github-owner-repo" / "PLAN-1" / "TASK-1"
    expected_path.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="not a git checkout"):
        prepare_git_workspace(
            _target(clone_url=str(tmp_path)), plan_id="PLAN-1", task_id="TASK-1", base_dir=tmp_path / "work"
        )
