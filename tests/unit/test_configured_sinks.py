"""Configured sink policy and project-config plan generation tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from whilly.adapters.filesystem.plan_io import parse_plan_dict
from whilly.pipeline.sinks import (
    CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX,
    PROFILE_APPROVED_PR_SINK_MARKER,
    PlanPRContext,
    is_configured_github_pr_sink_task,
    should_open_pr_for_completed_task,
)
from whilly.project_config import ProjectConfigError, build_plan_payload, project_config_from_dict


def _project_config(
    *, sink_config: dict[str, object] | None = None, human_loop: dict[str, object] | None = None
) -> dict:
    return {
        "name": "Configured PR sink",
        "project_type": "python_backend",
        "repositories": [
            {
                "id": "app",
                "role": "code",
                "provider": "github",
                "repo_full_name": "foo/bar",
                "clone_url": "https://github.com/foo/bar.git",
            }
        ],
        "pipeline": [
            {"id": "implement", "type": "development", "title": "Implement feature", "repo_role": "code"},
            {
                "id": "verify",
                "type": "quality_gate",
                "title": "Verify feature",
                "depends_on": ["implement"],
            },
        ],
        "sinks": [{"type": "github_pr", "config": sink_config or {"repo_role": "code"}}],
        "human_loop": human_loop or {"enabled": True, "approval_channel": "slack:#release"},
    }


def _task(
    *,
    repo_target_id: str = "",
    prd_requirement: str = "",
    acceptance_criteria: tuple[str, ...] = (),
    test_steps: tuple[str, ...] = (),
    description: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="T-SINK-1",
        repo_target_id=repo_target_id,
        prd_requirement=prd_requirement,
        acceptance_criteria=acceptance_criteria,
        test_steps=test_steps,
        description=description,
    )


def test_build_plan_payload_adds_human_gated_github_pr_sink_stage() -> None:
    config = project_config_from_dict(
        _project_config(
            sink_config={
                "repo_role": "code",
                "stage_id": "publish-pr",
                "base_branch": "develop",
                "branch_prefix": "whilly/configured",
            }
        )
    )

    payload = build_plan_payload(config, plan_id="configured-pr")
    _plan, tasks = parse_plan_dict(payload)

    assert len(tasks) == 3
    sink_task = payload["tasks"][-1]
    assert sink_task["id"] == "CFG-SINK-001-PUBLISH-PR"
    assert sink_task["dependencies"] == ["CFG-002-VERIFY"]
    assert sink_task["repo_target_id"] == "github:foo/bar"
    assert sink_task["prd_requirement"] == "Configured github_pr sink stage: publish-pr"
    assert "Configured sink: github_pr" in sink_task["description"]
    assert "HUMAN-IN-THE-LOOP CHECKPOINT" in sink_task["description"]
    assert (
        "Human approval or decision is explicitly recorded before the pipeline proceeds."
        in sink_task["acceptance_criteria"]
    )


def test_profile_approved_github_pr_sink_stage_does_not_require_human_loop() -> None:
    config = project_config_from_dict(
        _project_config(
            sink_config={"repo_role": "code", "stage_id": "publish-pr", "approval": "profile"},
            human_loop={"enabled": False},
        )
    )

    payload = build_plan_payload(config)
    sink_task = payload["tasks"][-1]

    assert sink_task["prd_requirement"] == "Configured github_pr sink stage: publish-pr"
    assert PROFILE_APPROVED_PR_SINK_MARKER in sink_task["acceptance_criteria"]
    assert "HUMAN-IN-THE-LOOP CHECKPOINT" not in sink_task["description"]


def test_project_config_rejects_github_pr_sink_without_human_loop_or_profile_approval() -> None:
    with pytest.raises(ProjectConfigError, match="github_pr sink requires human_loop.enabled"):
        project_config_from_dict(_project_config(human_loop={"enabled": False}))


def test_project_config_rejects_github_pr_sink_unknown_repo_role() -> None:
    with pytest.raises(ProjectConfigError, match="github_pr sink references unknown repo_role"):
        project_config_from_dict(_project_config(sink_config={"repo_role": "missing"}))


def test_project_config_rejects_github_pr_sink_without_resolvable_repo_target() -> None:
    data = _project_config()
    data["repositories"] = [{"id": "local", "role": "code", "path": "src", "writable": True}]

    with pytest.raises(ProjectConfigError, match="github_pr sink requires a provider repo target"):
        project_config_from_dict(data)


def test_project_config_pr_policy_allows_only_configured_sink_stage_with_approval_guard() -> None:
    context = PlanPRContext(github_issue_ref="foo/bar/42", origin_system="project_config")
    ordinary_task = _task(
        repo_target_id="github:foo/bar",
        prd_requirement="Configured python_backend pipeline step: implement",
    )
    sink_without_approval = _task(
        repo_target_id="github:foo/bar",
        prd_requirement=f"{CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX} publish-pr",
    )
    sink_with_human_gate = _task(
        repo_target_id="github:foo/bar",
        prd_requirement=f"{CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX} publish-pr",
        acceptance_criteria=("Human approval is recorded before opening the pull request.",),
    )

    assert should_open_pr_for_completed_task(context, ordinary_task).allowed is False
    assert should_open_pr_for_completed_task(context, sink_without_approval).allowed is False
    decision = should_open_pr_for_completed_task(context, sink_with_human_gate)
    assert decision.allowed is True
    assert decision.mode == "project_config"
    assert decision.repo_target_id == "github:foo/bar"


def test_legacy_pr_policy_preserves_issue_ref_and_repo_target_fallbacks() -> None:
    legacy_issue_context = PlanPRContext(github_issue_ref="foo/bar/42")
    legacy_empty_context = PlanPRContext()

    assert should_open_pr_for_completed_task(legacy_issue_context, _task()).allowed is True
    assert (
        should_open_pr_for_completed_task(legacy_empty_context, _task(repo_target_id="github:foo/bar")).allowed is True
    )
    assert should_open_pr_for_completed_task(legacy_empty_context, _task()).allowed is False


def test_profile_approval_marker_allows_configured_pr_sink_without_human_gate() -> None:
    context = PlanPRContext(origin_system="project_config")
    task = _task(
        repo_target_id="github:foo/bar",
        prd_requirement=f"{CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX} publish-pr",
        acceptance_criteria=(PROFILE_APPROVED_PR_SINK_MARKER,),
    )

    assert is_configured_github_pr_sink_task(task)
    assert should_open_pr_for_completed_task(context, task).allowed is True
