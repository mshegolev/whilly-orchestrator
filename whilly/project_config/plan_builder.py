"""Generate Whilly plan JSON from universal project configs."""

from __future__ import annotations

import re
from typing import Any

from whilly.pipeline.sinks import (
    CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX,
    GITHUB_PR_SINK_TYPE,
    PROFILE_APPROVED_PR_SINK_MARKER,
)
from whilly.project_config.models import PipelineStepConfig, ProjectConfig, RepositoryConfig, TaskSourceConfig

_SLUG_RE = re.compile(r"[^A-Za-z0-9._:/-]+")


def build_plan_payload(config: ProjectConfig, *, plan_id: str | None = None) -> dict[str, Any]:
    """Build canonical v4 plan JSON for a project config."""

    resolved_plan_id = plan_id or f"config-{_slug(config.name).lower()}"
    step_task_ids = {step.id: _task_id(index, step) for index, step in enumerate(config.pipeline, start=1)}
    repo_targets = [_repo_target(repo) for repo in config.repositories if repo.is_repo_target()]
    repo_by_role = _repo_by_role(config.repositories)

    tasks: list[dict[str, Any]] = []
    for step in config.pipeline:
        repo = repo_by_role.get(step.repo_role)
        task: dict[str, Any] = {
            "id": step_task_ids[step.id],
            "status": "PENDING",
            "dependencies": [step_task_ids[dep] for dep in step.depends_on],
            "key_files": _key_files(config, repo),
            "priority": _priority(step),
            "description": _description(config, step, repo),
            "acceptance_criteria": _acceptance_criteria(config, step),
            "test_steps": _test_steps(step),
            "prd_requirement": f"Configured {config.project_type} pipeline step: {step.id}",
        }
        if repo is not None and repo.is_repo_target():
            task["repo_target_id"] = repo.repo_target_id()
        tasks.append(task)

    tasks.extend(_sink_tasks(config, step_task_ids=step_task_ids, repo_by_role=repo_by_role))

    return {
        "plan_id": resolved_plan_id,
        "project": config.name,
        "origin": {
            "system": "project_config",
            "ref": config.name,
            "title": config.description or config.name,
            "decomposition_mode": f"configured:{config.project_type}",
        },
        "repo_targets": repo_targets,
        "tasks": tasks,
    }


def _sink_tasks(
    config: ProjectConfig,
    *,
    step_task_ids: dict[str, str],
    repo_by_role: dict[str, RepositoryConfig],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    leaf_task_ids = _leaf_task_ids(config, step_task_ids)
    for index, sink in enumerate((sink for sink in config.sinks if sink.type == GITHUB_PR_SINK_TYPE), start=1):
        sink_config = sink.config or {}
        stage_id = sink_config.get("stage_id", "") or "open-github-pr"
        repo = _sink_repo(sink_config, repo_by_role)
        profile_approved = _github_pr_sink_has_profile_approval(sink_config)
        step = PipelineStepConfig(
            id=stage_id,
            kind="sink",
            title="Open GitHub pull request",
            description="Configured sink: github_pr\nOpen a pull request after configured work and gates complete.",
            depends_on=(),
            repo_role=repo.role if repo is not None else "",
            human_gate=not profile_approved,
            acceptance_criteria=_github_pr_sink_acceptance_criteria(profile_approved=profile_approved),
            test_steps=(
                "Verify WHILLY_AUTO_OPEN_PR=1 is set before PR creation is attempted.",
                "Verify PR review feedback is handled by manual one-shot polling; bounded repair is future work.",
            ),
        )
        task: dict[str, Any] = {
            "id": f"CFG-SINK-{index:03d}-{_slug(stage_id).upper()}",
            "status": "PENDING",
            "dependencies": leaf_task_ids,
            "key_files": _key_files(config, repo),
            "priority": "medium",
            "description": _description(config, step, repo),
            "acceptance_criteria": _acceptance_criteria(config, step),
            "test_steps": _test_steps(step),
            "prd_requirement": f"{CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX} {stage_id}",
        }
        if repo is not None and repo.is_repo_target():
            task["repo_target_id"] = repo.repo_target_id()
        tasks.append(task)
    return tasks


def _repo_target(repo: RepositoryConfig) -> dict[str, Any]:
    out = {
        "id": repo.repo_target_id(),
        "provider": repo.provider,
        "repo_full_name": repo.repo_full_name,
    }
    if repo.clone_url:
        out["clone_url"] = repo.clone_url
    if repo.default_branch:
        out["default_branch"] = repo.default_branch
    return out


def _repo_by_role(repositories: tuple[RepositoryConfig, ...]) -> dict[str, RepositoryConfig]:
    out: dict[str, RepositoryConfig] = {}
    for repo in repositories:
        out.setdefault(repo.role, repo)
    return out


def _sink_repo(
    sink_config: dict[str, str],
    repo_by_role: dict[str, RepositoryConfig],
) -> RepositoryConfig | None:
    repo_role = (sink_config.get("repo_role", "") or "").strip().lower()
    if repo_role:
        return repo_by_role.get(repo_role)
    for repo in repo_by_role.values():
        if repo.is_repo_target():
            return repo
    return None


def _leaf_task_ids(config: ProjectConfig, step_task_ids: dict[str, str]) -> list[str]:
    dependency_ids = {dep for step in config.pipeline for dep in step.depends_on}
    leaf_ids = [step_task_ids[step.id] for step in config.pipeline if step.id not in dependency_ids]
    return leaf_ids or list(step_task_ids.values())[-1:]


def _task_id(index: int, step: PipelineStepConfig) -> str:
    return f"CFG-{index:03d}-{_slug(step.id).upper()}"


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip()).strip("-._:/")
    return slug or "default"


def _priority(step: PipelineStepConfig) -> str:
    priority = step.priority.lower()
    if priority not in {"critical", "high", "medium", "low"}:
        return "medium"
    return priority


def _github_pr_sink_acceptance_criteria(*, profile_approved: bool) -> tuple[str, ...]:
    criteria = (
        "Pull request creation runs only after configured upstream gates complete.",
        "PR review feedback is handled by manual one-shot polling until bounded repair is implemented.",
    )
    if profile_approved:
        return (*criteria, PROFILE_APPROVED_PR_SINK_MARKER)
    return criteria


def _github_pr_sink_has_profile_approval(config: dict[str, str]) -> bool:
    approval = (config.get("approval", "") or "").strip().lower()
    profile_approved = (config.get("profile_approved", "") or "").strip().lower()
    return approval == "profile" or profile_approved in {"1", "true", "yes", "on"}


def _key_files(config: ProjectConfig, repo: RepositoryConfig | None) -> list[str]:
    paths: list[str] = []
    if repo is not None and repo.path:
        paths.append(repo.path)
    for candidate in (config.outputs or {}).values():
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def _description(config: ProjectConfig, step: PipelineStepConfig, repo: RepositoryConfig | None) -> str:
    lines = [
        f"Project type: {config.project_type}",
        f"Pipeline step: {step.id} ({step.kind})",
        f"Title: {step.title}",
    ]
    if config.description:
        lines.append(f"Project description: {config.description}")
    if config.environment:
        lines.append(f"Target environment: {config.environment}")
    if step.description:
        lines.append("")
        lines.append(step.description)
    if config.task_sources:
        lines.append("")
        lines.append("Task sources:")
        for source in config.task_sources:
            lines.append(f"- {_source_label(source)}")
    if repo is not None:
        lines.append("")
        lines.append("Repository binding:")
        lines.append(f"- role={repo.role} id={repo.id}")
        if repo.repo_full_name:
            lines.append(f"- repo={repo.provider}:{repo.repo_full_name}")
        if repo.path:
            lines.append(f"- local_path={repo.path}")
        if repo.ref:
            lines.append(f"- ref={repo.ref_type or 'ref'}:{repo.ref}")
        if repo.suite:
            lines.append(f"- suite={repo.suite}")
    if step.commands:
        lines.append("")
        lines.append("Configured commands:")
        for command in step.commands:
            lines.append(f"- {command}")
    if step.outputs:
        lines.append("")
        lines.append("Expected outputs:")
        for output in step.outputs:
            lines.append(f"- {output}")
    if step.human_gate or step.kind == "human_gate":
        lines.append("")
        lines.append("HUMAN-IN-THE-LOOP CHECKPOINT:")
        lines.append("- Stop before irreversible or externally visible action.")
        lines.append("- Record human approval, requested changes, or rejection in the task result.")
        if config.human_loop.approval_channel:
            lines.append(f"- Approval channel: {config.human_loop.approval_channel}")
        if config.human_loop.instructions:
            lines.append(f"- Instructions: {config.human_loop.instructions}")
    return "\n".join(lines)


def _acceptance_criteria(config: ProjectConfig, step: PipelineStepConfig) -> list[str]:
    criteria = list(step.acceptance_criteria)
    if not criteria:
        criteria.append(
            f"Step {step.id} completes its {step.kind} responsibility for project type {config.project_type}."
        )
    if step.human_gate or step.kind == "human_gate":
        criteria.append("Human approval or decision is explicitly recorded before the pipeline proceeds.")
    if config.release_policy:
        target = config.release_policy.get("success_state") or config.release_policy.get("target_state")
        if target and step.kind == "release_decision":
            criteria.append(f"On success, release/task source moves to target state {target!r}.")
    return criteria


def _test_steps(step: PipelineStepConfig) -> list[str]:
    steps = list(step.test_steps)
    steps.extend(step.commands)
    if step.human_gate or step.kind == "human_gate":
        steps.append("Verify human approval evidence is present before marking this task complete.")
    return steps


def _source_label(source: TaskSourceConfig) -> str:
    details = [source.kind]
    if source.ref:
        details.append(f"ref={source.ref}")
    if source.query:
        details.append(f"query={source.query}")
    if source.url:
        details.append(f"url={source.url}")
    if source.filters:
        details.append(f"filters={source.filters}")
    return " ".join(details)
