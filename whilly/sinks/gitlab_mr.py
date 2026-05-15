"""GitLab Merge Request creation sink.

After a task is marked done, push the worktree commits to a feature branch and
open a merge request via the `glab` CLI. Designed to *never* break the main
loop: on any failure we return a `PRResult` with `ok=False` and let the caller
log it.

This module mirrors :mod:`whilly.sinks.github_pr` exactly in public shape so
that `post_complete_pr_hook`'s `PROpenerCallable` protocol can swap providers
at the composition root.

Programmatic:
    from whilly.sinks.gitlab_mr import open_mr_for_task
    result = open_mr_for_task(task=task, worktree_path=Path("..."), base_branch="master")
    if not result.ok:
        log.warning("MR sink failed for %s: %s", task.id, result.reason)

Token discovery order:
    1. ``GITLAB_API_TOKEN`` env var (or ``WHILLY_GITLAB_API_TOKEN``)
    2. ``glab config get token -h <host>`` CLI fallback

The token is not strictly required by `glab mr create` (glab uses its own auth
config), but it is resolved and exposed so callers can validate beforehand.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from whilly.sinks.github_pr import PRResult
from whilly.task_manager import Task

log = logging.getLogger("whilly")

# ── Public constants ───────────────────────────────────────────────────────────

DEFAULT_GLAB_BIN = "glab"
DEFAULT_GIT_BIN = "git"
DEFAULT_BASE_BRANCH = "master"
DEFAULT_BRANCH_PREFIX = "whilly"

_MR_NUMBER_RE = re.compile(r"/merge_requests/(\d+)", re.IGNORECASE)
_REMOTE_HOST_RE = re.compile(r"(?:https?://|git@)([^/:]+)", re.IGNORECASE)


# ── Token / host helpers ───────────────────────────────────────────────────────


def _infer_remote_host(worktree_path: Path, git_bin: str = DEFAULT_GIT_BIN, timeout: float = 10.0) -> str:
    """Return the hostname extracted from ``git config --get remote.origin.url``.

    Falls back to ``gitlab.example.com`` if the remote URL cannot be parsed.
    """
    try:
        result = subprocess.run(
            [git_bin, "config", "--get", "remote.origin.url"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            m = _REMOTE_HOST_RE.search(url)
            if m:
                return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return "gitlab.example.com"


def _resolve_gitlab_token(host: str, env: dict[str, str] | None = None) -> str | None:
    """Return a GitLab API token for *host*, or ``None``.

    Lookup order:
    1. ``GITLAB_API_TOKEN`` in *env* (or ``os.environ`` when *env* is ``None``).
    2. ``WHILLY_GITLAB_API_TOKEN`` in the same source.
    3. ``glab config get token -h <host>`` CLI fallback.
    """
    src: dict[str, str] | os._Environ[str] = env if env is not None else os.environ
    token = src.get("GITLAB_API_TOKEN") or src.get("WHILLY_GITLAB_API_TOKEN")
    if token:
        return token.strip()
    try:
        result = subprocess.run(
            ["glab", "config", "get", "token", "-h", host],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


# ── Branch / title / body ─────────────────────────────────────────────────────


def _branch_name(task: Task, prefix: str) -> str:
    """Return ``{prefix}/{task.id}`` with unsafe chars replaced by ``-``."""
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", task.id)
    return f"{prefix}/{safe_id}"


def _mr_title(task: Task, max_len: int = 60) -> str:
    """Return a one-line MR title: ``whilly[{task.id}]: {first 60 chars of description}``."""
    raw = (task.description or "").strip().splitlines()[0] if task.description else ""
    prefix = f"whilly[{task.id}]: "
    available = max_len - len(prefix)
    if available <= 0 or not raw:
        return f"whilly[{task.id}]"
    if len(raw) > available:
        raw = raw[: available - 1].rstrip() + "…"
    return f"{prefix}{raw}"


def _mr_body(task: Task) -> str:
    """Render the markdown body for the MR. Pure function, no IO."""
    lines: list[str] = []

    plan_id = getattr(task, "plan_id", None)

    lines.append(f"## Task `{task.id}`")
    if plan_id:
        lines.append(f"- **Plan**: `{plan_id}`")
    lines.append("")

    if task.description:
        lines.append("### Description")
        lines.append(task.description.strip())
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

    lines.append("---")
    lines.append("Generated by [whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator).")
    lines.append("Human review required before merge.")
    return "\n".join(lines)


# ── Subprocess wrapper ────────────────────────────────────────────────────────


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: float = 60.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run *cmd* inside *cwd*, capturing output. Never raises."""
    run_env = env if env is not None else None  # None → inherit os.environ
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=run_env,
        check=False,
    )


def _extract_mr_number_from_url(url: str) -> int | None:
    """Return the integer MR number from a ``…/merge_requests/<n>`` URL, or ``None``."""
    if not url:
        return None
    m = _MR_NUMBER_RE.search(url)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


# ── Top-level entry point ─────────────────────────────────────────────────────


def open_mr_for_task(
    *,
    task: Task,
    worktree_path: Path,
    branch_name: str | None = None,
    base_branch: str = DEFAULT_BASE_BRANCH,
    title: str | None = None,
    body: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
    git_bin: str = DEFAULT_GIT_BIN,
    glab_bin: str = DEFAULT_GLAB_BIN,
) -> PRResult:
    """Push the worktree branch and open a GitLab MR.

    Steps:
    1. Resolve branch name (default ``whilly/{task.id}``).
    2. ``git push origin HEAD:{branch} --force-with-lease``
    3. ``glab mr create --target-branch {base} --source-branch {branch}
         --title ... --description-file ...``

    Returns a :class:`~whilly.sinks.github_pr.PRResult` on every path —
    never raises.
    """
    branch = branch_name if branch_name else _branch_name(task, DEFAULT_BRANCH_PREFIX)
    effective_title = title if title is not None else _mr_title(task)
    effective_body = body if body is not None else _mr_body(task)

    if not worktree_path.exists():
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"worktree not found: {worktree_path}",
            failure_mode="worktree_missing",
        )

    # ── 1) git push ────────────────────────────────────────────────────────────
    # Use ``--force`` for whilly's feature branches: the branch namespace is
    # owned by this worker (``whilly/<task-id>``) so non-fast-forward pushes
    # from re-runs of the same task must overwrite the previous attempt.
    # ``--force-with-lease`` requires a remote tracking ref which is absent
    # on a fresh worktree, so plain ``--force`` is the right primitive here.
    push_cmd = [git_bin, "push", "--force", "origin", f"HEAD:{branch}"]
    try:
        push_proc = _run(push_cmd, cwd=worktree_path, timeout=timeout_seconds, env=env)
    except subprocess.TimeoutExpired:
        return PRResult(
            ok=False,
            branch=branch,
            reason="git push timeout",
            failure_mode="git_push_timeout",
        )

    if push_proc.returncode != 0:
        combined = (push_proc.stderr or push_proc.stdout or "").strip()
        # "Everything up-to-date" / "nothing to push" — treat as no_diff, not a hard error
        if "up-to-date" in combined.lower() or "nothing to push" in combined.lower():
            return PRResult(
                ok=False,
                branch=branch,
                reason="git push: nothing to push (no diff)",
                failure_mode="no_diff",
            )
        # Preserve enough stderr context for triage — a single first_line
        # from git push tends to be "To <url>" or "remote:" preamble, while
        # the actual ``fatal: ...`` / ``error: ...`` lines come later.
        diagnostic_tail = combined[-600:] if len(combined) > 600 else combined
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"git push failed: {diagnostic_tail}",
            failure_mode="git_push_failed",
            push_exit_code=int(push_proc.returncode),
        )

    # ── 2) glab mr create ─────────────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="whilly_mr_body_",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(effective_body)
        body_file = tmp.name

    try:
        # glab uses --description / -d (inline string), not --description-file
        # like `gh pr create`. We keep the temp file write above so the body
        # is fully captured for debugging, then read it back for the flag.
        with open(body_file, encoding="utf-8") as fh:
            description_inline = fh.read()
        mr_cmd = [
            glab_bin,
            "mr",
            "create",
            "--target-branch",
            base_branch,
            "--source-branch",
            branch,
            "--title",
            effective_title,
            "--description",
            description_inline,
            "--yes",  # non-interactive
        ]
        try:
            mr_proc = _run(mr_cmd, cwd=worktree_path, timeout=timeout_seconds, env=env)
        except subprocess.TimeoutExpired:
            return PRResult(
                ok=False,
                branch=branch,
                reason="glab mr create timeout",
                failure_mode="mr_create_timeout",
            )
    finally:
        try:
            os.unlink(body_file)
        except OSError:
            pass

    if mr_proc.returncode != 0:
        stderr = (mr_proc.stderr or "").strip()
        stdout = (mr_proc.stdout or "").strip()
        combined_err = stderr or stdout

        # If an MR already exists, surface the URL from stdout/stderr
        if "already exists" in combined_err.lower():
            existing_url = ""
            for line in (stdout + "\n" + stderr).splitlines():
                if "/merge_requests/" in line:
                    existing_url = line.strip()
                    break
            return PRResult(
                ok=True,
                pr_url=existing_url,
                branch=branch,
                reason="mr already existed",
                pr_number=_extract_mr_number_from_url(existing_url),
            )

        first_line = combined_err.splitlines()[:1] or ["unknown"]
        return PRResult(
            ok=False,
            branch=branch,
            reason=f"glab mr create failed: {first_line[0]}",
            failure_mode="mr_create_failed",
            gh_exit_code=int(mr_proc.returncode),
        )

    # Parse the MR URL from glab stdout (typically the last non-empty line)
    mr_url = ""
    for line in reversed((mr_proc.stdout or "").splitlines()):
        stripped = line.strip()
        if stripped:
            mr_url = stripped
            break

    mr_number = _extract_mr_number_from_url(mr_url)
    return PRResult(ok=True, pr_url=mr_url, branch=branch, pr_number=mr_number)


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_BRANCH_PREFIX",
    "DEFAULT_GLAB_BIN",
    "DEFAULT_GIT_BIN",
    "_infer_remote_host",
    "_resolve_gitlab_token",
    "open_mr_for_task",
]
