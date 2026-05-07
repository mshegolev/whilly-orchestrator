"""Post-COMPLETE PR opener hook for the M2 PR-review feedback loop.

Wires :func:`whilly.sinks.github_pr.open_pr_for_task` into the local
worker's main loop. Fired once per task that transitions to ``DONE``,
gated by:

1. ``WHILLY_AUTO_OPEN_PR=1`` in the environment, AND
2. The plan has a legacy ``github_issue_ref`` or the completed task carries
   a structured ``repo_target_id``. Project-config plans are stricter: they
   must complete an explicit configured ``github_pr`` sink task with a repo
   target plus human-review or profile-approval evidence.

On success the helper persists one row in the ``pull_requests`` table
and emits a ``pr.opened`` audit event to both Postgres and the JSONL
mirror (VAL-PR-005, VAL-PR-006). On ``git push`` failure or
``gh pr create`` failure / timeout, no ``pr.opened`` event is emitted
and no successful row is persisted; instead a single
``pr.open_failed`` warning event is recorded across both sinks with
``task_id`` plus the relevant exit code and ``failure_mode`` in
``detail`` (VAL-PR-022, VAL-PR-023). The COMPLETE state transition is
preserved regardless of the hook's outcome — the hook never raises
back into the worker loop.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from whilly.adapters.db.repository import (
    PR_OPEN_FAILED_EVENT_TYPE,
    PR_OPENED_EVENT_TYPE,
    TaskRepository,
)
from whilly.pipeline.sinks import PlanPRContext, should_open_pr_for_completed_task
from whilly.sinks.github_pr import PRResult, open_pr_for_task

logger = logging.getLogger(__name__)

#: Environment variable that opts a deployment into the post-COMPLETE
#: PR opener hook. ``"1"`` enables; anything else (unset, ``"0"``,
#: empty string) keeps the legacy script-only behaviour.
AUTO_OPEN_PR_ENV: str = "WHILLY_AUTO_OPEN_PR"


PROpenerCallable = Callable[..., PRResult]


def is_auto_open_pr_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """Return ``True`` iff ``WHILLY_AUTO_OPEN_PR`` is the literal ``"1"``."""
    source: dict[str, str] | os._Environ[str] = env if env is not None else os.environ
    return source.get(AUTO_OPEN_PR_ENV, "").strip() == "1"


async def run_post_complete_pr_hook(
    repo: TaskRepository,
    *,
    plan_id: str,
    task: Any,
    worktree_path: Path,
    opener: PROpenerCallable | None = None,
    env: dict[str, str] | os._Environ[str] | None = None,
) -> PRResult | None:
    """Maybe open a PR for ``task`` after a successful COMPLETE.

    Returns ``None`` when the hook is gated off (env unset, or the
    task/plan has no PR-routable GitHub context); otherwise returns the
    :class:`PRResult` produced by ``opener`` so callers can log /
    introspect.

    The hook never raises — every failure mode is converted into a
    structured ``pr.open_failed`` event so the COMPLETE transition
    stays the source of truth for the worker loop.
    """
    if not is_auto_open_pr_enabled(env):
        return None

    context = await _get_plan_pr_context(repo, plan_id)
    decision = should_open_pr_for_completed_task(context, task)
    if not decision.allowed:
        logger.debug(
            "post_complete_pr_hook: skipping plan=%s task=%s reason=%s",
            plan_id,
            getattr(task, "id", "<unknown>"),
            decision.reason,
        )
        return None

    effective_opener: PROpenerCallable = opener if opener is not None else open_pr_for_task

    task_id = getattr(task, "id", "<unknown>")

    try:
        result: PRResult = effective_opener(task=task, worktree_path=worktree_path)
    except Exception as exc:  # noqa: BLE001 — hook must never raise into the worker.
        logger.warning(
            "post_complete_pr_hook: opener raised for task=%s plan=%s: %s",
            task_id,
            plan_id,
            exc,
        )
        await _emit_failure_event(
            repo,
            plan_id=plan_id,
            task_id=task_id,
            failure_mode="opener_exception",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        return None

    if result.ok:
        await _record_success(repo, plan_id=plan_id, task=task, result=result)
        return result

    await _record_failure(repo, plan_id=plan_id, task_id=task_id, result=result)
    return result


async def _get_plan_pr_context(repo: TaskRepository, plan_id: str) -> PlanPRContext:
    getter = getattr(repo, "get_plan_pr_context", None)
    if getter is not None:
        return await getter(plan_id)
    issue_ref = await repo.get_plan_github_issue_ref(plan_id)
    return PlanPRContext(github_issue_ref=issue_ref)


async def _record_success(
    repo: TaskRepository,
    *,
    plan_id: str,
    task: Any,
    result: PRResult,
) -> None:
    task_id = getattr(task, "id", "<unknown>")
    repo_target_id = getattr(task, "repo_target_id", "") or None
    pr_number = result.pr_number if result.pr_number is not None else 0
    try:
        await repo.insert_pull_request(
            plan_id=plan_id,
            task_id=task_id,
            repo_target_id=repo_target_id,
            pr_number=pr_number,
            pr_url=result.pr_url,
            branch=result.branch,
            head_sha=result.head_sha,
            state="open",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "post_complete_pr_hook: failed to persist pull_requests row for task=%s plan=%s: %s",
            task_id,
            plan_id,
            exc,
        )
        await _emit_failure_event(
            repo,
            plan_id=plan_id,
            task_id=task_id,
            failure_mode="db_insert_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        return

    detail: dict[str, Any] = {
        "pr_url": result.pr_url,
        "pr_number": pr_number,
        "branch": result.branch,
        "head_sha": result.head_sha,
        "task_id": task_id,
    }
    if repo_target_id is not None:
        detail["repo_target_id"] = repo_target_id
    try:
        await repo.emit_pr_event(
            PR_OPENED_EVENT_TYPE,
            plan_id=plan_id,
            task_id=task_id,
            payload=detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "post_complete_pr_hook: failed to emit pr.opened for task=%s plan=%s: %s",
            task_id,
            plan_id,
            exc,
        )


async def _record_failure(
    repo: TaskRepository,
    *,
    plan_id: str,
    task_id: str,
    result: PRResult,
) -> None:
    extra: dict[str, Any] = {"reason": result.reason}
    if result.push_exit_code is not None:
        extra["push_exit_code"] = result.push_exit_code
    if result.gh_exit_code is not None:
        extra["gh_exit_code"] = result.gh_exit_code
    if result.branch:
        extra["branch"] = result.branch
    await _emit_failure_event(
        repo,
        plan_id=plan_id,
        task_id=task_id,
        failure_mode=result.failure_mode or "unknown",
        extra=extra,
    )


async def _emit_failure_event(
    repo: TaskRepository,
    *,
    plan_id: str,
    task_id: str,
    failure_mode: str,
    extra: dict[str, Any] | None = None,
) -> None:
    detail: dict[str, Any] = {"task_id": task_id, "failure_mode": failure_mode}
    if extra:
        detail.update(extra)
    try:
        await repo.emit_pr_event(
            PR_OPEN_FAILED_EVENT_TYPE,
            plan_id=plan_id,
            task_id=task_id,
            payload=detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "post_complete_pr_hook: failed to emit pr.open_failed for task=%s plan=%s: %s",
            task_id,
            plan_id,
            exc,
        )


PostCompleteHook = Callable[[Any], Awaitable[None]]


def make_post_complete_hook(
    repo: TaskRepository,
    *,
    plan_id: str,
    worktree_path: Path,
    opener: PROpenerCallable | None = None,
    env: dict[str, str] | os._Environ[str] | None = None,
) -> PostCompleteHook:
    """Build a closure suitable for ``run_local_worker``'s ``post_complete_hook`` arg.

    The closure forwards the just-completed task plus the bound
    ``repo`` / ``plan_id`` / ``worktree_path`` to
    :func:`run_post_complete_pr_hook`. Mirrors the existing
    composition-root pattern in :mod:`whilly.cli.run` — the CLI
    composes I/O dependencies once and passes a callable down into
    :mod:`whilly.worker.local`.
    """

    async def _hook(task: Any) -> None:
        await run_post_complete_pr_hook(
            repo,
            plan_id=plan_id,
            task=task,
            worktree_path=worktree_path,
            opener=opener,
            env=env,
        )

    return _hook


__all__ = [
    "AUTO_OPEN_PR_ENV",
    "PROpenerCallable",
    "PostCompleteHook",
    "is_auto_open_pr_enabled",
    "make_post_complete_hook",
    "run_post_complete_pr_hook",
]
