"""Git worktree isolation for parallel Ralph agents.

Each agent gets an isolated copy of the repo via ``git worktree add``.
After task completion the changes are cherry-picked back to the main branch.

Usage:
    from ralph.worktree_runner import WorktreeManager
    wm = WorktreeManager()
    wt = wm.create("TASK-001")        # creates worktree + branch
    # ... agent works in wt.path ...
    merged = wm.merge_back("TASK-001") # cherry-pick commits
    wm.cleanup("TASK-001")            # remove worktree
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("ralph.worktree")

_WORKTREE_BASE = ".ralph_worktrees"


@dataclass
class Worktree:
    """Represents a git worktree for a task."""

    task_id: str
    branch: str
    path: Path
    created: bool = False


@dataclass
class MergeResult:
    """Result of merging worktree changes back."""

    success: bool
    commits_merged: int = 0
    conflict: bool = False
    error: str = ""


class WorktreeManager:
    """Manages git worktrees for parallel agent isolation."""

    def __init__(self, base_dir: str = _WORKTREE_BASE):
        self._base = Path(base_dir)
        self._worktrees: dict[str, Worktree] = {}

    def create(self, task_id: str) -> Worktree:
        """Create a new git worktree for a task.

        Creates branch ``ralph/{task_id}`` and worktree at
        ``{base_dir}/{task_id}``.

        Args:
            task_id: Task identifier (e.g., "TASK-001").

        Returns:
            Worktree with path to the isolated repo copy.
        """
        branch = f"ralph/{task_id}"
        wt_path = self._base / task_id

        # Cleanup stale worktree if exists
        if wt_path.exists():
            self._remove_worktree(wt_path)

        self._base.mkdir(parents=True, exist_ok=True)

        # Create branch from HEAD
        subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True,
            timeout=10,
        )  # ignore error if branch doesn't exist

        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree for {task_id}: {result.stderr}")

        wt = Worktree(task_id=task_id, branch=branch, path=wt_path, created=True)
        self._worktrees[task_id] = wt
        log.info("Created worktree for %s at %s (branch: %s)", task_id, wt_path, branch)
        return wt

    def merge_back(self, task_id: str) -> MergeResult:
        """Cherry-pick commits from worktree branch back to current branch.

        Args:
            task_id: Task identifier.

        Returns:
            MergeResult with success status and commit count.
        """
        wt = self._worktrees.get(task_id)
        if not wt:
            return MergeResult(success=False, error=f"No worktree for {task_id}")

        # Count commits on branch since divergence
        result = subprocess.run(
            ["git", "log", "--oneline", f"HEAD..{wt.branch}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return MergeResult(success=False, error=f"Failed to list commits: {result.stderr}")

        commits = [line for line in result.stdout.strip().split("\n") if line.strip()]
        if not commits:
            log.info("%s: no commits to merge from worktree", task_id)
            return MergeResult(success=True, commits_merged=0)

        # Cherry-pick all commits
        cherry = subprocess.run(
            ["git", "cherry-pick", f"HEAD..{wt.branch}", "--no-commit"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if cherry.returncode != 0:
            # Conflict detected
            subprocess.run(["git", "cherry-pick", "--abort"], capture_output=True, timeout=10)
            log.warning("%s: merge conflict from worktree: %s", task_id, cherry.stderr[:200])
            return MergeResult(success=False, conflict=True, error=cherry.stderr[:200])

        # Commit the cherry-picked changes
        subprocess.run(
            ["git", "commit", "-m", f"Ralph merge: {task_id} ({len(commits)} commits)"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        log.info("%s: merged %d commits from worktree", task_id, len(commits))
        return MergeResult(success=True, commits_merged=len(commits))

    def cleanup(self, task_id: str) -> None:
        """Remove worktree and branch for a task."""
        wt = self._worktrees.pop(task_id, None)
        if wt and wt.path.exists():
            self._remove_worktree(wt.path)
            # Remove branch
            subprocess.run(
                ["git", "branch", "-D", wt.branch],
                capture_output=True,
                timeout=10,
            )
            log.info("Cleaned up worktree for %s", task_id)

    def cleanup_all(self) -> int:
        """Remove all ralph worktrees."""
        cleaned = 0
        for task_id in list(self._worktrees.keys()):
            self.cleanup(task_id)
            cleaned += 1

        # Also clean orphaned worktrees
        if self._base.exists():
            for wt_dir in self._base.iterdir():
                if wt_dir.is_dir():
                    self._remove_worktree(wt_dir)
                    cleaned += 1
            # Remove base dir if empty
            try:
                self._base.rmdir()
            except OSError:
                pass

        return cleaned

    def get_path(self, task_id: str) -> Path | None:
        """Get worktree path for a task."""
        wt = self._worktrees.get(task_id)
        return wt.path if wt else None

    @staticmethod
    def _remove_worktree(path: Path) -> None:
        """Remove a git worktree safely."""
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            # Fallback: just delete the directory
            shutil.rmtree(path, ignore_errors=True)
