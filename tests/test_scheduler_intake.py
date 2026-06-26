"""Tests for scheduler issue→task intake (scheduler-dispatch-issues-to-tasks).

These cover the pure builder/resolver in ``whilly/scheduler/intake.py`` (no DB)
and the ``SchedulerWorker`` recording of the issues-found callback return value
into ``cycle.created_plans``.
"""

from __future__ import annotations

import pytest

from whilly.core.models import Priority, TaskStatus
from whilly.scheduler.intake import (
    build_plan_from_issues,
    plan_id_for_rule,
    resolve_repo_target,
)
from whilly.scheduler.models import SchedulerRule


def _issue(key: str, *, summary: str = "Do the thing", priority: str = "High") -> dict:
    """A JQL-search-shaped Jira issue dict (has ``key``, ``self``, ``fields``)."""
    return {
        "key": key,
        "self": "https://jira.example.com/rest/api/3/issue/10001",
        "fields": {
            "summary": summary,
            "description": (
                "Background text.\n\n## Acceptance\n- user can log in\n\n## Test\n- click the login button\n"
            ),
            "labels": ["bug"],
            "priority": {"name": priority},
        },
    }


def _rule(**overrides) -> SchedulerRule:
    base = dict(
        id="demo-plan",
        name="My DEMO tasks",
        jira_project_key="DEMO",
        jql_filter="assignee = currentUser()",
    )
    base.update(overrides)
    return SchedulerRule(**base)


class TestPlanIdForRule:
    def test_defaults_to_rule_id(self) -> None:
        assert plan_id_for_rule(_rule()) == "demo-plan"

    def test_custom_metadata_plan_id_wins(self) -> None:
        rule = _rule(custom_metadata={"plan_id": "override-plan"})
        assert plan_id_for_rule(rule) == "override-plan"


class TestResolveRepoTarget:
    def test_none_when_absent(self) -> None:
        assert resolve_repo_target(_rule()) is None

    def test_gitlab_target(self, monkeypatch) -> None:
        # Unset so the neutral in-code default applies deterministically,
        # regardless of any WHILLY_GITLAB_SSH_HOST in the dev's shell/.env.
        monkeypatch.delenv("WHILLY_GITLAB_SSH_HOST", raising=False)
        rule = _rule(custom_metadata={"repo_target": "gitlab:group/sub/repo"})
        rt = resolve_repo_target(rule)
        assert rt is not None
        assert rt.id == "gitlab:group/sub/repo"
        assert rt.provider == "gitlab"
        assert rt.repo_full_name == "group/sub/repo"
        assert rt.clone_url == "git@gitlab.example.com:group/sub/repo.git"

    def test_gitlab_target_host_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("WHILLY_GITLAB_SSH_HOST", "git.internal.example")
        rule = _rule(custom_metadata={"repo_target": "gitlab:group/repo"})
        rt = resolve_repo_target(rule)
        assert rt is not None
        assert rt.clone_url == "git@git.internal.example:group/repo.git"

    def test_github_target(self) -> None:
        rule = _rule(custom_metadata={"repo_target": "github:my-org/my-repo"})
        rt = resolve_repo_target(rule)
        assert rt is not None
        assert rt.clone_url == "https://github.com/my-org/my-repo.git"

    def test_explicit_clone_url_honored(self) -> None:
        rule = _rule(
            custom_metadata={
                "repo_target": "gitlab:grp/repo",
                "repo_clone_url": "https://gitlab.example/grp/repo.git",
            }
        )
        rt = resolve_repo_target(rule)
        assert rt is not None
        assert rt.clone_url == "https://gitlab.example/grp/repo.git"

    def test_malformed_target_ignored(self) -> None:
        assert resolve_repo_target(_rule(custom_metadata={"repo_target": "nope"})) is None


class TestBuildPlanFromIssues:
    def test_single_issue_becomes_pending_task(self) -> None:
        plan = build_plan_from_issues(_rule(), [_issue("DEMO-123")])
        assert plan.id == "demo-plan"
        assert len(plan.tasks) == 1
        task = plan.tasks[0]
        assert task.id == "JIRA-DEMO-123"
        assert task.status is TaskStatus.PENDING
        assert task.priority is Priority.HIGH
        assert "Do the thing" in task.description

    def test_acceptance_and_test_steps_extracted(self) -> None:
        # Issue content is sanitized (UNTRUSTED-wrapped) just like the
        # single-issue ``whilly jira import`` path, so match on substring.
        task = build_plan_from_issues(_rule(), [_issue("DEMO-1")]).tasks[0]
        assert any("user can log in" in a for a in task.acceptance_criteria)
        assert any("click the login button" in t for t in task.test_steps)

    def test_priority_defaults_to_medium(self) -> None:
        task = build_plan_from_issues(_rule(), [_issue("DEMO-9", priority="")]).tasks[0]
        assert task.priority is Priority.MEDIUM

    def test_origin_marks_jira_scheduler(self) -> None:
        plan = build_plan_from_issues(_rule(), [_issue("DEMO-1")])
        assert plan.origin is not None
        assert plan.origin.system == "jira_scheduler"
        assert plan.origin.ref == "demo-plan"

    def test_repo_target_attached_to_plan_and_tasks(self) -> None:
        rule = _rule(custom_metadata={"repo_target": "gitlab:example-group/autotests/example-repo"})
        plan = build_plan_from_issues(rule, [_issue("DEMO-7")])
        assert len(plan.repo_targets) == 1
        assert plan.repo_targets[0].id == "gitlab:example-group/autotests/example-repo"
        assert plan.tasks[0].repo_target_id == "gitlab:example-group/autotests/example-repo"

    def test_no_repo_target_leaves_task_unrouted(self) -> None:
        plan = build_plan_from_issues(_rule(), [_issue("DEMO-7")])
        assert plan.repo_targets == ()
        assert plan.tasks[0].repo_target_id == ""

    def test_issues_without_key_skipped(self) -> None:
        issues = [_issue("DEMO-1"), {"fields": {"summary": "no key"}}, {"key": "  "}]
        plan = build_plan_from_issues(_rule(), issues)
        assert [t.id for t in plan.tasks] == ["JIRA-DEMO-1"]

    def test_multiple_issues_all_under_one_plan(self) -> None:
        plan = build_plan_from_issues(_rule(), [_issue("DEMO-1"), _issue("DEMO-2")])
        assert {t.id for t in plan.tasks} == {"JIRA-DEMO-1", "JIRA-DEMO-2"}
        assert plan.id == "demo-plan"


class TestDeduplicationOnRawJiraShape:
    """Raw ``execute_jql`` issues nest ``summary`` under ``fields`` — the default
    dedup fields must still hash them (regression for the dispatch dead-end)."""

    def test_default_fields_hash_nested_jira_issue(self) -> None:
        from whilly.scheduler.deduplicator import deduplicate_issues

        issues = [_issue("DEMO-1"), _issue("DEMO-2", summary="Other")]
        unique, dups = deduplicate_issues(issues)  # default ("key", "summary")
        assert len(unique) == 2
        assert dups == []

    def test_duplicate_nested_summary_suppressed(self) -> None:
        from whilly.scheduler.deduplicator import deduplicate_issues

        issues = [_issue("DEMO-1", summary="Same"), _issue("DEMO-1", summary="Same")]
        unique, dups = deduplicate_issues(issues)
        assert len(unique) == 1
        assert dups == ["DEMO-1"]


class TestWorkerRecordsCreatedPlans:
    @pytest.mark.asyncio
    async def test_callback_return_recorded_in_created_plans(self) -> None:
        from unittest.mock import patch

        from whilly.scheduler.worker import SchedulerWorker

        async def fake_jql(jql: str, max_results: int = 50) -> list[dict]:
            return [_issue("DEMO-1")]

        async def on_issues_found(rule, issues) -> list[str]:
            return ["demo-plan"]

        rule = _rule(poll_interval_seconds=0)
        worker = SchedulerWorker([rule], on_issues_found=on_issues_found)
        with patch.object(worker, "_execute_jql_async", side_effect=fake_jql):
            cycle = await worker._poll_rule(rule)

        assert cycle.created_plans == ["demo-plan"]

    @pytest.mark.asyncio
    async def test_callback_returning_none_leaves_created_plans_empty(self) -> None:
        from unittest.mock import patch

        from whilly.scheduler.worker import SchedulerWorker

        async def fake_jql(jql: str, max_results: int = 50) -> list[dict]:
            return [_issue("DEMO-1")]

        async def on_issues_found(rule, issues) -> None:
            return None

        rule = _rule(poll_interval_seconds=0)
        worker = SchedulerWorker([rule], on_issues_found=on_issues_found)
        with patch.object(worker, "_execute_jql_async", side_effect=fake_jql):
            cycle = await worker._poll_rule(rule)

        assert cycle.created_plans == []
