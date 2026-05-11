"""Tests for Jira work classification and readiness routing."""

from __future__ import annotations

from pathlib import Path

from whilly.jira_work import (
    classify_jira_work,
    jira_context_hashes,
    parse_whilly_comment_command,
    probe_code_readiness,
    release_context_repo_targets,
)
from whilly.qa_release.models import GitRepoHint, ReleaseContext


def test_classify_bug_hotfix_with_reproduction_and_rollback_signals() -> None:
    issue = {
        "key": "ABC-123",
        "fields": {
            "summary": "Checkout fails in production",
            "description": "Actual error on checkout. Expected order. Steps to reproduce. Rollback plan. Smoke test.",
            "issuetype": {"name": "Bug"},
            "priority": {"name": "Highest"},
            "labels": ["incident"],
        },
    }

    classification = classify_jira_work(issue)

    assert classification.kind == "bug"
    assert classification.urgency == "hotfix"
    assert classification.recommended_flow == "hotfix_bug"
    assert "jira_type:bug" in classification.signals
    assert "reproduction_steps" not in classification.missing_context
    assert "rollback_plan" not in classification.missing_context


def test_classify_feature_plan_requires_acceptance_criteria_when_missing() -> None:
    plan = {
        "name": "Add ETL export wizard",
        "tasks": [
            {
                "id": "JIRA-ABC-124",
                "description": "Implement a new ETL export wizard for analysts.",
            }
        ],
    }

    classification = classify_jira_work(plan)

    assert classification.kind == "feature"
    assert classification.urgency == "normal"
    assert classification.recommended_flow == "feature_prd"
    assert "acceptance_criteria" in classification.missing_context


def test_parse_whilly_comment_commands() -> None:
    classify = parse_whilly_comment_command("Looks good\n/whilly classify devops")
    urgency = parse_whilly_comment_command("/whilly urgency hotfix")
    run = parse_whilly_comment_command("/whilly run")

    assert classify is not None
    assert classify.action == "classify"
    assert classify.value == "devops"
    assert urgency is not None
    assert urgency.action == "urgency"
    assert urgency.value == "hotfix"
    assert run is not None
    assert run.action == "run"
    assert run.value == ""
    assert parse_whilly_comment_command("plain Jira discussion") is None


def test_jira_context_hashes_are_stable_for_link_order_and_sensitive_to_description() -> None:
    issue = {"summary": "A", "description": "B"}

    first = jira_context_hashes(issue, ["https://gitlab/acme/repo/-/merge_requests/2", "https://jira/ABC-1"])
    second = jira_context_hashes(issue, ["https://jira/ABC-1", "https://gitlab/acme/repo/-/merge_requests/2"])
    changed = jira_context_hashes({"summary": "A", "description": "C"}, first["links"])

    assert first == second
    assert changed["summary_hash"] == first["summary_hash"]
    assert changed["description_hash"] != first["description_hash"]


def test_probe_code_readiness_detects_python_unit_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_etl.py").write_text("def test_etl():\n    assert True\n", encoding="utf-8")

    result = probe_code_readiness(tmp_path)

    assert result.verdict == "ready_for_testing"
    assert "python3 -m pytest -q tests/unit" in result.test_commands
    assert "tests/unit/test_etl.py" in result.test_files


def test_probe_code_readiness_requires_tests_even_when_command_exists(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")

    result = probe_code_readiness(tmp_path)

    assert result.verdict == "needs_test_plan"
    assert "unit_tests" in result.missing_context
    assert "npm test" in result.test_commands


def test_release_context_repo_targets_reuses_gitlab_hints() -> None:
    context = ReleaseContext(
        root_key="ABC-123",
        root_summary="Release",
        root_url="https://jira/browse/ABC-123",
        linked_issues=(),
        links=(),
        repo_hints=(
            GitRepoHint(
                provider="gitlab",
                repo_full_name="platform/etl",
                url="https://gitlab.company/platform/etl/-/merge_requests/7",
                clone_url="https://gitlab.company/platform/etl.git",
                ref="feature/etl",
                ref_type="merge_request",
            ),
        ),
    )

    targets = release_context_repo_targets(context)

    assert targets == [
        {
            "id": "gitlab:platform/etl",
            "provider": "gitlab",
            "repo_full_name": "platform/etl",
            "clone_url": "https://gitlab.company/platform/etl.git",
            "default_branch": "",
        }
    ]
