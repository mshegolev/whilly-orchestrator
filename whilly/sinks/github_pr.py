"""GitHub PR creation sink.

After a task is marked done, push the worktree commits to a feature branch and
open a pull request via the `gh` CLI. Designed to *never* break the main loop:
on any failure we return a `PRResult` with `ok=False` and let the caller log it.

Programmatic:
    from whilly.sinks import open_pr_for_task
    result = open_pr_for_task(task, worktree_path=Path("..."), base="main")
    if not result.ok:
        log.warning("PR sink failed for %s: %s", task.id, result.reason)
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from whilly.task_manager import Task

log = logging.getLogger("whilly")


# ── Public spec ────────────────────────────────────────────────────────────────


DEFAULT_GH_BIN = "gh"
DEFAULT_GIT_BIN = "git"
DEFAULT_BASE_BRANCH = "main"

_ISSUE_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/issues/(\d+)", re.IGNORECASE)


@dataclass
class PRResult:
    """Outcome of a PR creation attempt."""

    ok: bool
    pr_url: str = ""
    branch: str = ""
    reason: str = ""  # populated on failure


@dataclass
class GitHubPRSink:
    """Configuration for the PR sink."""

    base_branch: str = DEFAULT_BASE_BRANCH
    draft: bool = False
    branch_prefix: str = "whilly"
    gh_bin: str = DEFAULT_GH_BIN
    git_bin: str = DEFAULT_GIT_BIN

    def open(self, task: Task, worktree_path: Path, **extras) -> PRResult:
        """Open a PR for `task` using the working tree at `worktree_path`."""
        return open_pr_for_task(
            task=task,
            worktree_path=worktree_path,
            base=self.base_branch,
            draft=self.draft,
            branch_prefix=self.branch_prefix,
            gh_bin=self.gh_bin,
            git_bin=self.git_bin,
            **extras,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _strip_env_tokens() -> dict:
    """Return os.environ copy with GH/GITHUB tokens removed (prefer keyring auth)."""
    import os as _os

    env = dict(_os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return env


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a CLI inside `cwd`, return CompletedProcess (no raise)."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_strip_env_tokens(),
        check=False,
    )


def _branch_name(task: Task, prefix: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", task.id)
    return f"{prefix}/{safe_id}"


def _short_title(task: Task, max_len: int = 60) -> str:
    """Return a one-line title prefix for the PR. Falls back to task.id."""
    raw = (task.description or "").strip().splitlines()[0] if task.description else ""
    if not raw:
        return task.id
    if len(raw) > max_len:
        raw = raw[: max_len - 1].rstrip() + "…"
    return f"{task.id}: {raw}"


def _extract_issue_number(prd_requirement: str) -> int | None:
    """If prd_requirement is a GH issue URL, return the issue number."""
    if not prd_requirement:
        return None
    m = _ISSUE_URL_RE.search(prd_requirement)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def render_pr_body(
    task: Task,
    cost_usd: float = 0.0,
    duration_s: float = 0.0,
    log_file: str = "",
) -> str:
    """Render the markdown body for the PR. Pure function, no IO."""
    lines: list[str] = []

    issue_n = _extract_issue_number(task.prd_requirement)
    if issue_n is not None:
        lines.append(f"Closes #{issue_n}.")
        lines.append("")
    elif task.prd_requirement:
        lines.append(f"Implements [{task.id}]({task.prd_requirement}).")
        lines.append("")
    else:
        lines.append(f"Implements task `{task.id}`.")
        lines.append("")

    lines.append("### Description")
    lines.append((task.description or "(no description)").strip())
    lines.append("")

    if task.acceptance_criteria:
        lines.append("### Acceptance criteria")
        for ac in task.acceptance_criteria:
            lines.append(f"- {ac}")
        lines.append("")

    if task.test_steps:
        lines.append("### Validation")
        for ts in task.test_steps:
            lines.append(f"- {ts}")
        lines.append("")

    lines.append("### Whilly run")
    lines.append(f"- task_id: `{task.id}`")
    lines.append(f"- cost: ${cost_usd:.4f}")
    lines.append(f"- duration: {duration_s:.1f}s")
    if log_file:
        lines.append(f"- agent log: `{log_file}`")
    lines.append("- JSONL events: `whilly_logs/whilly_events.jsonl`")
    lines.append("")
    lines.append("---")
    lines.append("🤖 Opened by [whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator).")
    lines.append("Human review required before merge.")
    return "\n".join(lines)


# ── Top-level entry point ─────────────────────────────────────────────────────


def open_pr_for_task(
    task: Task,
    worktree_path: Path,
    base: str = DEFAULT_BASE_BRANCH,
    draft: bool = False,
    branch_prefix: str = "whilly",
    cost_usd: float = 0.0,
    duration_s: float = 0.0,
    log_file: str = "",
    push_timeout: int = 60,
    pr_timeout: int = 60,
    gh_bin: str = DEFAULT_GH_BIN,
    git_bin: str = DEFAULT_GIT_BIN,
) -> PRResult:
    """Push the worktree branch and open a PR.

    Steps:
    1. `git push origin HEAD:{branch} --force-with-lease`
    2. `gh pr create --base {base} --head {branch} --title ... --body-file ...`

    Never raises — failures populate PRResult.reason.
    """
    branch = _branch_name(task, branch_prefix)
    title = _short_title(task)
    body = render_pr_body(task, cost_usd=cost_usd, duration_s=duration_s, log_file=log_file)

    if not worktree_path.exists():
        return PRResult(ok=False, branch=branch, reason=f"worktree not found: {worktree_path}")

    # 1) push
    push_cmd = [git_bin, "push", "origin", f"HEAD:{branch}", "--force-with-lease"]
    try:
        push_proc = _run(push_cmd, cwd=worktree_path, timeout=push_timeout)
    except subprocess.TimeoutExpired:
        return PRResult(ok=False, branch=branch, reason="git push timeout")
    if push_proc.returncode != 0:
        msg = (push_proc.stderr or push_proc.stdout or "").strip().splitlines()[-1:] or ["unknown"]
        return PRResult(ok=False, branch=branch, reason=f"git push failed: {msg[0]}")

    # 2) gh pr create
    pr_cmd = [
        gh_bin,
        "pr",
        "create",
        "--base",
        base,
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        pr_cmd.append("--draft")

    try:
        pr_proc = _run(pr_cmd, cwd=worktree_path, timeout=pr_timeout)
    except subprocess.TimeoutExpired:
        return PRResult(ok=False, branch=branch, reason="gh pr create timeout")

    if pr_proc.returncode != 0:
        # If a PR already exists we treat that as ok and try to extract its URL via gh pr view.
        stderr = (pr_proc.stderr or "").strip()
        if "already exists" in stderr.lower():
            view_proc = _run([gh_bin, "pr", "view", branch, "--json", "url"], cwd=worktree_path)
            if view_proc.returncode == 0:
                try:
                    import json

                    url = json.loads(view_proc.stdout).get("url", "")
                    return PRResult(ok=True, pr_url=url, branch=branch, reason="pr already existed")
                except Exception:  # noqa: BLE001
                    return PRResult(ok=True, pr_url="", branch=branch, reason="pr already existed")
        msg = stderr.splitlines()[-1:] or ["unknown"]
        return PRResult(ok=False, branch=branch, reason=f"gh pr create failed: {msg[0]}")

    pr_url = (pr_proc.stdout or "").strip().splitlines()[-1] if pr_proc.stdout else ""
    return PRResult(ok=True, pr_url=pr_url, branch=branch)
