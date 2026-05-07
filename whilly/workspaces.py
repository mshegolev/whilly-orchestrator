"""Repository-target workspace preparation for local Whilly runs."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from whilly.adapters.db.repository import TaskRepository
from whilly.core.models import Plan, RepoTarget, Task

log = logging.getLogger(__name__)

WORKSPACE_BASE_ENV: Final[str] = "WHILLY_WORKSPACE_BASE"
DEFAULT_WORKSPACE_BASE: Final[str] = ".whilly_workspaces/repos"
WORKSPACE_PREPARED_EVENT_TYPE: Final[str] = "workspace.prepared"
WORKSPACE_FAILED_EXIT_CODE: Final[int] = -4


@dataclass(frozen=True)
class ResolvedWorkspace:
    """Prepared directory where a task's agent process should run."""

    path: Path
    repo_target_id: str = ""
    repo_full_name: str = ""
    branch: str = ""
    reused_current_cwd: bool = False

    def event_payload(self) -> dict[str, object]:
        return {
            "repo_target_id": self.repo_target_id,
            "repo_full_name": self.repo_full_name,
            "workspace_path": str(self.path),
            "branch": self.branch,
            "reused_current_cwd": self.reused_current_cwd,
        }


class RepoTargetWorkspaceResolver:
    """Resolve a task's ``repo_target_id`` into a local git workspace."""

    def __init__(
        self,
        repo: TaskRepository,
        *,
        base_dir: str | Path | None = None,
        current_cwd: Path | None = None,
    ) -> None:
        self._repo = repo
        self._base_dir = Path(base_dir or os.environ.get(WORKSPACE_BASE_ENV) or DEFAULT_WORKSPACE_BASE)
        self._current_cwd = (current_cwd or Path.cwd()).resolve()

    async def prepare(self, task: Task, plan: Plan) -> ResolvedWorkspace:
        """Return a workspace for ``task``.

        Tasks without ``repo_target_id`` keep legacy behaviour and run in
        the process cwd. Repo-targeted tasks are cloned/fetched in a
        deterministic workspace path under ``WHILLY_WORKSPACE_BASE``.
        """
        if not task.repo_target_id:
            return ResolvedWorkspace(path=self._current_cwd, reused_current_cwd=True)

        target = await self._repo.get_repo_target(task.repo_target_id)
        if target is None:
            raise RuntimeError(f"repo target {task.repo_target_id!r} is not registered")

        workspace = await asyncio.to_thread(
            prepare_git_workspace,
            target,
            plan_id=plan.id,
            task_id=task.id,
            base_dir=self._base_dir,
        )
        await self._record_workspace_event(task, workspace)
        return workspace

    async def _record_workspace_event(self, task: Task, workspace: ResolvedWorkspace) -> None:
        recorder = getattr(self._repo, "record_task_event", None)
        if recorder is None:
            return
        try:
            await recorder(
                task.id,
                WORKSPACE_PREPARED_EVENT_TYPE,
                {
                    "repo_target_id": workspace.repo_target_id,
                    "repo_full_name": workspace.repo_full_name,
                    "branch": workspace.branch,
                },
                detail=workspace.event_payload(),
            )
        except Exception:  # noqa: BLE001 - observability must not block execution
            log.warning("workspace event append failed: task=%s", task.id, exc_info=True)


def prepare_git_workspace(
    target: RepoTarget,
    *,
    plan_id: str,
    task_id: str,
    base_dir: str | Path = DEFAULT_WORKSPACE_BASE,
) -> ResolvedWorkspace:
    """Clone/fetch and checkout a task branch for ``target``."""
    clone_url = _clone_url(target)
    if not clone_url:
        raise RuntimeError(f"repo target {target.id!r} has no clone_url and no known provider fallback")

    repo_slug = _safe_path_part(target.id)
    plan_slug = _safe_path_part(plan_id)
    task_slug = _safe_path_part(task_id)
    path = Path(base_dir) / repo_slug / plan_slug / task_slug
    branch = f"whilly/{plan_slug}/{task_slug}"

    if path.exists():
        if not (path / ".git").exists():
            raise RuntimeError(f"workspace path exists but is not a git checkout: {path}")
        _ensure_clean_workspace(path)
        _git(path, "fetch", "origin", "--prune")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(path.parent, "clone", clone_url, path.name)

    base_ref = _base_ref(path, target.default_branch)
    _git(path, "checkout", "-B", branch, base_ref)
    log.info("prepared workspace task=%s target=%s path=%s branch=%s", task_id, target.id, path, branch)
    return ResolvedWorkspace(
        path=path.resolve(),
        repo_target_id=target.id,
        repo_full_name=target.repo_full_name,
        branch=branch,
    )


def _clone_url(target: RepoTarget) -> str:
    if target.clone_url:
        return target.clone_url
    if target.provider == "github" and target.repo_full_name:
        return f"https://github.com/{target.repo_full_name}.git"
    return ""


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned[:96] or "default"


def _base_ref(path: Path, default_branch: str) -> str:
    if default_branch:
        candidate = f"origin/{default_branch}"
        if _git_ok(path, "rev-parse", "--verify", candidate):
            return candidate
    if _git_ok(path, "rev-parse", "--verify", "origin/HEAD"):
        return "origin/HEAD"
    return "HEAD"


def _ensure_clean_workspace(path: Path) -> None:
    proc = _git(path, "status", "--porcelain")
    if proc.stdout.strip():
        raise RuntimeError(f"workspace has uncommitted changes: {path}")


def _git(cwd: Path, *args: object) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *(str(arg) for arg in args)]
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "no output").strip()
        raise RuntimeError(f"{' '.join(cmd)} failed in {cwd}: {msg}")
    return proc


def _git_ok(cwd: Path, *args: object) -> bool:
    cmd = ["git", *(str(arg) for arg in args)]
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=30, check=False)
    return proc.returncode == 0
