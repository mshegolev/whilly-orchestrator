"""Pure policy helpers for configured result sinks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from whilly.pipeline.human_review import requires_human_review

GITHUB_PR_SINK_TYPE = "github_pr"
CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX = "Configured github_pr sink stage:"
PROFILE_APPROVED_PR_SINK_MARKER = "PROFILE-APPROVED EXTERNAL ACTION: github_pr"


@dataclass(frozen=True, slots=True)
class PlanPRContext:
    """Plan-level routing metadata used by the post-complete PR policy."""

    github_issue_ref: str | None = None
    origin_system: str = ""
    origin_ref: str = ""
    decomposition_mode: str = ""


@dataclass(frozen=True, slots=True)
class PRGateDecision:
    """Decision for whether a completed task may trigger PR creation."""

    allowed: bool
    reason: str
    repo_target_id: str | None = None
    mode: str = "legacy"


def is_project_config_plan(context: PlanPRContext) -> bool:
    """Return whether the plan came from the project-config profile path."""

    return context.origin_system.strip().lower() == "project_config"


def task_repo_target_id(task: Any) -> str:
    """Return a normalized task-level repo target id."""

    return str(_field(task, "repo_target_id", "") or "").strip()


def is_configured_github_pr_sink_task(task: Any) -> bool:
    """Return whether ``task`` is the explicit project-config GitHub PR sink stage."""

    return _text_field(task, "prd_requirement").startswith(CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX)


def task_declares_pr_sink_approval(task: Any) -> bool:
    """Return whether the task carries a human-review or profile approval guard."""

    if requires_human_review(task=task):
        return True
    return PROFILE_APPROVED_PR_SINK_MARKER in _combined_task_text(task)


def should_open_pr_for_completed_task(context: PlanPRContext, task: Any) -> PRGateDecision:
    """Return whether the post-complete PR hook may run for ``task``.

    Legacy plans keep the historical behavior: a plan-level GitHub issue ref
    or task repo target is enough, with ``WHILLY_AUTO_OPEN_PR=1`` checked by
    the caller. Project-config plans are stricter because multiple configured
    pipeline tasks may target the same repository; only an explicit GitHub PR
    sink task with a repo target and approval guard may trigger an externally
    visible PR action.
    """

    repo_target_id = task_repo_target_id(task)
    issue_ref = str(context.github_issue_ref or "").strip()
    if is_project_config_plan(context):
        if not repo_target_id:
            return PRGateDecision(
                allowed=False,
                reason="project_config_missing_repo_target",
                mode="project_config",
            )
        if not is_configured_github_pr_sink_task(task):
            return PRGateDecision(
                allowed=False,
                reason="project_config_not_pr_sink_stage",
                repo_target_id=repo_target_id,
                mode="project_config",
            )
        if not task_declares_pr_sink_approval(task):
            return PRGateDecision(
                allowed=False,
                reason="project_config_pr_sink_missing_approval_guard",
                repo_target_id=repo_target_id,
                mode="project_config",
            )
        return PRGateDecision(
            allowed=True,
            reason="project_config_pr_sink",
            repo_target_id=repo_target_id,
            mode="project_config",
        )

    if issue_ref or repo_target_id:
        return PRGateDecision(
            allowed=True,
            reason="legacy_pr_context",
            repo_target_id=repo_target_id or None,
            mode="legacy",
        )
    return PRGateDecision(allowed=False, reason="legacy_missing_pr_context", mode="legacy")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text_field(value: Any, name: str) -> str:
    return str(_field(value, name, "") or "").strip()


def _combined_task_text(task: Any) -> str:
    parts = [
        _text_field(task, "prd_requirement"),
        _text_field(task, "description"),
        *_string_items(_field(task, "acceptance_criteria", ())),
        *_string_items(_field(task, "test_steps", ())),
    ]
    return "\n".join(part for part in parts if part)


def _string_items(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return (str(value),)


__all__ = [
    "CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX",
    "GITHUB_PR_SINK_TYPE",
    "PROFILE_APPROVED_PR_SINK_MARKER",
    "PRGateDecision",
    "PlanPRContext",
    "is_configured_github_pr_sink_task",
    "is_project_config_plan",
    "should_open_pr_for_completed_task",
    "task_declares_pr_sink_approval",
    "task_repo_target_id",
]
