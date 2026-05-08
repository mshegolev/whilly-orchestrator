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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from whilly.rollback.models import PreflightReport
from whilly.rollback.service import build_preflight_report
from whilly.security.prompt_sanitizer import sanitize_external_text, sanitize_title_slot
from whilly.task_manager import Task

log = logging.getLogger("whilly")


# ── Public spec ────────────────────────────────────────────────────────────────


DEFAULT_GH_BIN = "gh"
DEFAULT_GIT_BIN = "git"
DEFAULT_BASE_BRANCH = "main"

_ISSUE_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/issues/(\d+)", re.IGNORECASE)
_PR_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/pull/(\d+)", re.IGNORECASE)


def _extract_pr_number_from_url(url: str) -> int | None:
    """Return the integer PR number from a ``…/pull/<n>`` URL, or ``None``."""
    if not url:
        return None
    m = _PR_URL_RE.search(url)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


@dataclass
class PRResult:
    """Outcome of a PR creation attempt."""

    ok: bool
    pr_url: str = ""
    branch: str = ""
    reason: str = ""  # populated on failure
    pr_number: int | None = None
    head_sha: str | None = None
    failure_mode: str = ""  # e.g. "rollback_preflight_failed", "git_push_failed", "gh_pr_create_failed"
    push_exit_code: int | None = None
    gh_exit_code: int | None = None


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
    """Return a subprocess env prepared by the centralised whilly helper.

    See :mod:`whilly.gh_utils` for how `WHILLY_GH_TOKEN` / `WHILLY_GH_PREFER_KEYRING`
    / ambient `GITHUB_TOKEN` interact.
    """
    from whilly.gh_utils import gh_subprocess_env

    return gh_subprocess_env()


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
    """Return a one-line title prefix for the PR. Falls back to task.id.

    The result is suitable for direct use as a ``gh pr create --title`` argv
    slot: control bytes (NUL, ANSI escapes, ``\\n``, ``\\t``) are stripped,
    secret patterns are redacted, and the entire string is capped at
    ``max_len`` characters (default 60).
    """
    raw = (task.description or "").strip().splitlines()[0] if task.description else ""
    raw = sanitize_title_slot(raw, max_chars=max_len) if raw else ""
    if not raw:
        return sanitize_title_slot(task.id, max_chars=max_len)
    prefix = f"{task.id}: "
    available = max_len - len(prefix)
    if available <= 0:
        return sanitize_title_slot(task.id, max_chars=max_len)
    if len(raw) > available:
        raw = raw[: available - 1].rstrip() + "…"
    return f"{prefix}{raw}"


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
        safe_prd = sanitize_external_text(task.prd_requirement, scope="pr_body_prd_requirement")
        lines.append(f"Implements [{task.id}]({safe_prd}).")
        lines.append("")
    else:
        lines.append(f"Implements task `{task.id}`.")
        lines.append("")

    lines.append("### Description")
    raw_desc = (task.description or "").strip()
    if raw_desc:
        lines.append(sanitize_external_text(raw_desc, scope="pr_body_description"))
    else:
        lines.append("(no description)")
    lines.append("")

    if task.acceptance_criteria:
        lines.append("### Acceptance criteria")
        for ac in task.acceptance_criteria:
            lines.append(f"- {sanitize_external_text(ac, scope='pr_body_acceptance')}")
        lines.append("")

    if task.test_steps:
        lines.append("### Validation")
        for ts in task.test_steps:
            lines.append(f"- {sanitize_external_text(ts, scope='pr_body_test_step')}")
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
    preflight_builder: Callable[..., PreflightReport] | None = None,
) -> PRResult:
    """Push the worktree branch and open a PR.

    Steps:
    1. Build rollback preflight evidence for the push target.
    2. `git push origin HEAD:{branch} --force-with-lease`
    3. `gh pr create --base {base} --head {branch} --title ... --body-file ...`

    Never raises — failures populate PRResult.reason.
    """
    branch = _branch_name(task, branch_prefix)
    title = _short_title(task)
    body = render_pr_body(task, cost_usd=cost_usd, duration_s=duration_s, log_file=log_file)

    if not worktree_path.exists():
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"worktree not found: {worktree_path}",
            failure_mode="worktree_missing",
        )

    builder = preflight_builder or build_preflight_report
    try:
        report = builder(worktree_path, operation="push", target_ref=branch)
    except Exception as exc:  # noqa: BLE001 - PR sink must not raise into the worker loop.
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"rollback preflight failed: {type(exc).__name__}: {exc}",
            failure_mode="rollback_preflight_failed",
        )
    if not report.ok:
        return PRResult(
            ok=False,
            branch=branch,
            reason="rollback preflight failed: " + "; ".join(report.blockers),
            failure_mode="rollback_preflight_failed",
        )

    # 1) push
    push_cmd = [git_bin, "push", "origin", f"HEAD:{branch}", "--force-with-lease"]
    try:
        push_proc = _run(push_cmd, cwd=worktree_path, timeout=push_timeout)
    except subprocess.TimeoutExpired:
        return PRResult(
            ok=False,
            branch=branch,
            reason="git push timeout",
            failure_mode="git_push_timeout",
        )
    if push_proc.returncode != 0:
        msg = (push_proc.stderr or push_proc.stdout or "").strip().splitlines()[-1:] or ["unknown"]
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"git push failed: {msg[0]}",
            failure_mode="git_push_failed",
            push_exit_code=int(push_proc.returncode),
        )

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
        return PRResult(
            ok=False,
            branch=branch,
            reason="gh pr create timeout",
            failure_mode="gh_pr_create_timeout",
        )

    if pr_proc.returncode != 0:
        # If a PR already exists we treat that as ok and try to extract its URL via gh pr view.
        stderr = (pr_proc.stderr or "").strip()
        if "already exists" in stderr.lower():
            view_proc = _run(
                [gh_bin, "pr", "view", branch, "--json", "url,number,headRefOid"],
                cwd=worktree_path,
            )
            if view_proc.returncode == 0:
                try:
                    import json

                    parsed = json.loads(view_proc.stdout)
                    return PRResult(
                        ok=True,
                        pr_url=parsed.get("url", ""),
                        branch=branch,
                        reason="pr already existed",
                        pr_number=int(parsed["number"]) if parsed.get("number") is not None else None,
                        head_sha=parsed.get("headRefOid") or None,
                    )
                except Exception:  # noqa: BLE001
                    return PRResult(ok=True, pr_url="", branch=branch, reason="pr already existed")
        msg = stderr.splitlines()[-1:] or ["unknown"]
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"gh pr create failed: {msg[0]}",
            failure_mode="gh_pr_create_failed",
            gh_exit_code=int(pr_proc.returncode),
        )

    pr_url = (pr_proc.stdout or "").strip().splitlines()[-1] if pr_proc.stdout else ""
    pr_number = _extract_pr_number_from_url(pr_url)
    return PRResult(ok=True, pr_url=pr_url, branch=branch, pr_number=pr_number)
