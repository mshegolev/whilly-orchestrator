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
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ralph.worktree")

_WORKTREE_BASE = ".ralph_worktrees"
_PLAN_WORKSPACE_BASE = ".ralph_workspaces"


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


# ─── Plan-level workspace ──────────────────────────────────────────────────
#
# Изолирует ВЕСЬ план в отдельный git worktree, чтобы несколько агентов могли
# работать в одной репе параллельно без шансов затереть друг другу файлы.
# В отличие от WorktreeManager (per-task), здесь — один workspace на весь план.


@dataclass
class PlanWorkspace:
    """Worktree для целого плана (не per-task)."""

    slug: str
    branch: str
    path: Path
    reused: bool = False


def plan_slug(plan_data: dict, plan_file: Path) -> str:
    """Вывести безопасный slug из плана.

    Приоритет: plan["project"] → имя файла без расширения.
    Транслитерирует кириллицу, оставляет [a-z0-9-], обрезает до 48 символов.
    """
    source = str(plan_data.get("project") or "").strip() or plan_file.stem
    source = source.lower()
    source = re.sub(r"(^prd[-_])|([-_]?tasks?$)", "", source)
    translit = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    source = source.translate(translit)
    source = re.sub(r"[^a-z0-9]+", "-", source).strip("-")
    return source[:48] or "plan"


def workspace_path(slug: str, base_dir: str | Path = _PLAN_WORKSPACE_BASE) -> Path:
    """Каноничный путь workspace для slug."""
    return Path(base_dir) / slug


def find_existing_workspace(slug: str, base_dir: str | Path = _PLAN_WORKSPACE_BASE) -> Path | None:
    """Проверить что worktree с таким slug уже зарегистрирован в git и физически существует.

    Если worktree числится в ``git worktree list``, но директория удалена
    (stale) — делаем ``git worktree prune`` и возвращаем None, чтобы вызвавший
    мог пересоздать workspace с нуля.
    """
    target = workspace_path(slug, base_dir).resolve()
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):].strip()).resolve()
        if path == target or str(path).endswith(f"/{slug}"):
            if path.exists():
                return path
            # Stale worktree entry — директория удалена руками/cleanup'ом.
            log.warning("Stale worktree registration for %s (path %s missing), pruning", slug, path)
            subprocess.run(["git", "worktree", "prune"], capture_output=True, timeout=10)
            return None
    return None


def create_plan_workspace(
    slug: str,
    base_dir: str | Path = _PLAN_WORKSPACE_BASE,
    base_branch: str = "HEAD",
    allow_reuse: bool = True,
) -> PlanWorkspace:
    """Создать или переиспользовать plan-level worktree.

    Args:
        slug: Имя workspace (из plan_slug()).
        base_dir: Корень для всех workspaces (``.ralph_workspaces``).
        base_branch: От какой ветки создавать (default — текущий HEAD).
        allow_reuse: Если True и workspace уже есть — вернуть как reused.
            Если False — поднять RuntimeError (защита от коллизии агентов).

    Returns:
        PlanWorkspace с reused=True если подхватили существующий.

    Raises:
        RuntimeError: workspace уже существует и allow_reuse=False.
    """
    branch = f"ralph/workspace/{slug}"
    wt_path = workspace_path(slug, base_dir)

    existing = find_existing_workspace(slug, base_dir)
    if existing is not None:
        if not allow_reuse:
            raise RuntimeError(
                f"Workspace '{slug}' уже существует: {existing}. "
                f"Возможно другой агент уже работает. "
                f"Запусти с allow_reuse=True или выбери другой slug."
            )
        log.info("Reusing existing workspace: %s", existing)
        return PlanWorkspace(slug=slug, branch=branch, path=existing, reused=True)

    Path(base_dir).mkdir(parents=True, exist_ok=True)

    branch_check = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True, timeout=5,
    )
    if branch_check.returncode != 0:
        create_cmd = ["git", "worktree", "add", "-b", branch, str(wt_path), base_branch]
    else:
        create_cmd = ["git", "worktree", "add", str(wt_path), branch]

    result = subprocess.run(create_cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    log.info("Создан workspace '%s' на ветке %s: %s", slug, branch, wt_path)
    return PlanWorkspace(slug=slug, branch=branch, path=wt_path, reused=False)


def remove_plan_workspace(slug: str, base_dir: str | Path = _PLAN_WORKSPACE_BASE,
                          delete_branch: bool = False) -> bool:
    """Удалить plan workspace (worktree + опционально ветку).

    Returns:
        True если удалили, False если его не было.
    """
    existing = find_existing_workspace(slug, base_dir)
    if existing is None:
        return False
    subprocess.run(["git", "worktree", "remove", "--force", str(existing)],
                   capture_output=True, timeout=30)
    if delete_branch:
        subprocess.run(["git", "branch", "-D", f"ralph/workspace/{slug}"],
                       capture_output=True, timeout=10)
    log.info("Удалён workspace: %s", existing)
    return True
