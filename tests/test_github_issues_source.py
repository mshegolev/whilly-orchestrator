"""Unit tests for whilly.sources.github_issues."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from whilly.sources import github_issues as gh
from whilly.sources.github_issues import (
    DEFAULT_LABEL,
    GitHubIssuesSource,
    FetchStats,
    _detect_secrets,
    _extract_inline_field,
    _extract_section,
    _priority_from_labels,
    fetch_github_issues,
    issue_to_task,
    merge_into_plan,
)


# ── source spec parsing ────────────────────────────────────────────────────────


class TestSpecParsing:
    def test_parse_with_gh_prefix_default_label(self):
        spec = GitHubIssuesSource.parse("gh:foo/bar")
        assert spec.owner == "foo"
        assert spec.repo == "bar"
        assert spec.label == DEFAULT_LABEL
        assert spec.repo_full == "foo/bar"
        assert spec.project_name == "github-foo-bar"

    def test_parse_without_gh_prefix(self):
        spec = GitHubIssuesSource.parse("foo/bar")
        assert spec.owner == "foo"
        assert spec.repo == "bar"

    def test_parse_with_custom_label(self):
        spec = GitHubIssuesSource.parse("gh:foo/bar:my-label")
        assert spec.label == "my-label"

    def test_parse_invalid_no_slash(self):
        with pytest.raises(ValueError, match="owner/repo"):
            GitHubIssuesSource.parse("gh:foobar")

    def test_parse_invalid_empty_owner(self):
        with pytest.raises(ValueError, match="owner/repo"):
            GitHubIssuesSource.parse("gh:/bar")


# ── label parsing ──────────────────────────────────────────────────────────────


class TestPriorityFromLabels:
    def test_default_medium(self):
        assert _priority_from_labels([]) == "medium"

    def test_priority_critical(self):
        labels = [{"name": "priority:critical"}, {"name": "bug"}]
        assert _priority_from_labels(labels) == "critical"

    def test_priority_low(self):
        assert _priority_from_labels([{"name": "Priority:Low"}]) == "low"

    def test_unknown_labels_default_medium(self):
        assert _priority_from_labels([{"name": "bug"}, {"name": "feature"}]) == "medium"


# ── secret detection ───────────────────────────────────────────────────────────


class TestSecretDetection:
    def test_aws_key_detected(self):
        text = "Use this credential: AKIAIOSFODNN7EXAMPLE"
        hits = _detect_secrets(text)
        assert any("AKIA" in h for h in hits)

    def test_github_pat_detected(self):
        text = "token=ghp_" + "A" * 40
        hits = _detect_secrets(text)
        assert any("ghp_" in h for h in hits)

    def test_clean_text(self):
        assert _detect_secrets("just a normal description") == []


# ── inline / section extraction ────────────────────────────────────────────────


class TestExtractors:
    def test_inline_field_files(self):
        body = "**Files:** src/a.py, src/b.py , tests/c.py\nrest"
        assert _extract_inline_field(body, "Files") == ["src/a.py", "src/b.py", "tests/c.py"]

    def test_inline_field_missing(self):
        assert _extract_inline_field("nothing here", "Files") == []

    def test_section_acceptance(self):
        body = "Some intro\n## Acceptance\n- returns 200\n- body matches schema\n\n## Test\n- curl localhost\n"
        assert _extract_section(body, "Acceptance") == ["returns 200", "body matches schema"]
        assert _extract_section(body, "Test") == ["curl localhost"]

    def test_section_case_insensitive(self):
        body = "## acceptance\n- one"
        assert _extract_section(body, "Acceptance") == ["one"]

    def test_section_missing(self):
        assert _extract_section("# Other\n- x", "Acceptance") == []


# ── issue → task ───────────────────────────────────────────────────────────────


class TestIssueToTask:
    def test_basic_conversion(self):
        issue = {
            "number": 42,
            "title": "Add /health endpoint",
            "body": (
                "We need a healthcheck.\n\n"
                "**Files:** app/server.py\n"
                "## Acceptance\n"
                "- GET /health -> 200\n"
                "## Test\n"
                "- curl -s localhost/health\n"
            ),
            "labels": [{"name": "priority:high"}],
            "url": "https://github.com/foo/bar/issues/42",
        }
        task, secrets = issue_to_task(issue)
        assert task.id == "GH-42"
        assert task.phase == "GH-Issues"
        assert task.category == "github-issue"
        assert task.priority == "high"
        assert task.status == "pending"
        assert task.key_files == ["app/server.py"]
        assert task.acceptance_criteria == ["GET /health -> 200"]
        assert task.test_steps == ["curl -s localhost/health"]
        assert task.prd_requirement == "https://github.com/foo/bar/issues/42"
        assert "/health endpoint" in task.description
        assert secrets == []

    def test_empty_body(self):
        issue = {"number": 5, "title": "Quick fix", "body": "", "labels": [], "url": ""}
        task, secrets = issue_to_task(issue)
        assert task.id == "GH-5"
        assert task.description == "Quick fix"
        assert task.acceptance_criteria == []
        assert task.priority == "medium"

    def test_secret_warning(self):
        issue = {
            "number": 1,
            "title": "x",
            "body": "key=AKIAIOSFODNN7EXAMPLE more text",
            "labels": [],
            "url": "",
        }
        _, secrets = issue_to_task(issue)
        assert secrets

    def test_long_body_truncated(self):
        body = "Header\n" + ("line\n" * 200)
        issue = {"number": 1, "title": "T", "body": body, "labels": [], "url": ""}
        task, _ = issue_to_task(issue)
        assert "…" in task.description
        assert len(task.description) < 600


# ── merge_into_plan idempotency ────────────────────────────────────────────────


class TestMergeIntoPlan:
    @pytest.fixture
    def source(self) -> GitHubIssuesSource:
        return GitHubIssuesSource(owner="foo", repo="bar", label=DEFAULT_LABEL)

    def test_merge_into_empty_file_creates_plan(self, tmp_path: Path, source: GitHubIssuesSource):
        plan = tmp_path / "tasks.json"
        issues = [
            {"number": 1, "title": "first", "body": "", "labels": [], "url": ""},
            {"number": 2, "title": "second", "body": "", "labels": [], "url": ""},
        ]
        stats = merge_into_plan(issues, source, plan)
        assert stats.new == 2
        assert stats.updated == 0
        data = json.loads(plan.read_text())
        ids = [t["id"] for t in data["tasks"]]
        assert ids == ["GH-1", "GH-2"]
        assert data["source"]["repo"] == "foo/bar"
        assert data["project"] == "github-foo-bar"

    def test_re_fetch_preserves_status(self, tmp_path: Path, source: GitHubIssuesSource):
        plan = tmp_path / "tasks.json"
        # First fetch.
        merge_into_plan(
            [{"number": 1, "title": "T", "body": "old", "labels": [], "url": ""}],
            source,
            plan,
        )
        # Mutate a task's status as if loop ran.
        data = json.loads(plan.read_text())
        data["tasks"][0]["status"] = "in_progress"
        plan.write_text(json.dumps(data))

        # Re-fetch with updated body.
        stats = merge_into_plan(
            [{"number": 1, "title": "T new", "body": "fresh body", "labels": [], "url": ""}],
            source,
            plan,
        )
        assert stats.new == 0
        assert stats.updated == 1
        data2 = json.loads(plan.read_text())
        assert data2["tasks"][0]["status"] == "in_progress"  # preserved
        assert "fresh body" in data2["tasks"][0]["description"]  # refreshed

    def test_externally_closed_issue_marked_skipped(self, tmp_path: Path, source: GitHubIssuesSource):
        plan = tmp_path / "tasks.json"
        merge_into_plan(
            [
                {"number": 1, "title": "A", "body": "", "labels": [], "url": ""},
                {"number": 2, "title": "B", "body": "", "labels": [], "url": ""},
            ],
            source,
            plan,
        )
        # Re-fetch with #2 missing (closed externally).
        stats = merge_into_plan(
            [{"number": 1, "title": "A", "body": "", "labels": [], "url": ""}],
            source,
            plan,
        )
        assert stats.closed_externally == 1
        data = json.loads(plan.read_text())
        statuses = {t["id"]: t["status"] for t in data["tasks"]}
        assert statuses["GH-1"] == "pending"
        assert statuses["GH-2"] == "skipped"

    def test_done_status_not_overridden_when_issue_disappears(self, tmp_path: Path, source: GitHubIssuesSource):
        plan = tmp_path / "tasks.json"
        merge_into_plan(
            [{"number": 1, "title": "A", "body": "", "labels": [], "url": ""}],
            source,
            plan,
        )
        data = json.loads(plan.read_text())
        data["tasks"][0]["status"] = "done"
        plan.write_text(json.dumps(data))

        merge_into_plan([], source, plan)  # issue gone
        data = json.loads(plan.read_text())
        # done status preserved — only pending/in_progress get auto-skipped.
        assert data["tasks"][0]["status"] == "done"


# ── fetch_github_issues end-to-end (gh mocked) ────────────────────────────────


class TestFetchEndToEnd:
    def test_fetch_invokes_gh_and_writes_plan(self, tmp_path: Path):
        plan = tmp_path / "tasks.json"
        gh_payload = json.dumps(
            [
                {
                    "number": 7,
                    "title": "Add CONTRIBUTING badge",
                    "body": "Easy one.\n\n## Acceptance\n- README has the badge\n",
                    "labels": [{"name": "priority:low"}],
                    "url": "https://github.com/owner/repo/issues/7",
                }
            ]
        )

        class _CompletedProc:
            returncode = 0
            stdout = gh_payload
            stderr = ""

        with patch.object(gh, "_run_gh", return_value=_CompletedProc()) as mock_run:
            path, stats = fetch_github_issues("owner/repo", out_path=plan)
        mock_run.assert_called_once()
        assert path == plan.resolve()
        assert stats.new == 1
        loaded = json.loads(plan.read_text())
        assert loaded["tasks"][0]["id"] == "GH-7"
        assert loaded["tasks"][0]["priority"] == "low"

    def test_fetch_propagates_gh_failure(self, tmp_path: Path):
        plan = tmp_path / "tasks.json"

        class _FailedProc:
            returncode = 1
            stdout = ""
            stderr = "auth failed"

        with patch.object(gh, "_run_gh", return_value=_FailedProc()):
            with pytest.raises(RuntimeError, match="gh issue list failed"):
                fetch_github_issues("owner/repo", out_path=plan)

    def test_fetch_invalid_repo_format(self, tmp_path: Path):
        with pytest.raises(ValueError, match="owner/repo"):
            fetch_github_issues("just-a-name", out_path=tmp_path / "x.json")

    def test_stats_dataclass_defaults(self):
        s = FetchStats()
        assert s.new == 0
        assert s.updated == 0
        assert s.closed_externally == 0
        assert s.secret_warnings == []
