"""Unit tests for whilly.sinks.github_pr."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


from whilly.sinks import github_pr as gp
from whilly.sinks.github_pr import (
    GitHubPRSink,
    PRResult,
    _branch_name,
    _extract_issue_number,
    _short_title,
    open_pr_for_task,
    render_pr_body,
)
from whilly.task_manager import Task


def _make_task(**overrides) -> Task:
    base = dict(
        id="GH-42",
        phase="GH-Issues",
        category="github-issue",
        priority="medium",
        description="Add /health endpoint returning ok",
        status="done",
        dependencies=[],
        key_files=["app/server.py"],
        acceptance_criteria=["GET /health returns 200"],
        test_steps=["curl -s localhost/health"],
        prd_requirement="https://github.com/foo/bar/issues/42",
    )
    base.update(overrides)
    return Task(**base)


# ── pure helpers ───────────────────────────────────────────────────────────────


class TestBranchName:
    def test_clean_id(self):
        assert _branch_name(_make_task(id="TASK-001"), "whilly") == "whilly/TASK-001"

    def test_unsafe_chars_replaced(self):
        assert _branch_name(_make_task(id="GH 42!"), "whilly") == "whilly/GH-42-"

    def test_custom_prefix(self):
        assert _branch_name(_make_task(id="X"), "agent") == "agent/X"


class TestShortTitle:
    def test_short_first_line(self):
        t = _make_task(description="Add foo")
        assert _short_title(t) == "GH-42: Add foo"

    def test_long_truncated(self):
        long_desc = "a" * 200
        title = _short_title(_make_task(description=long_desc))
        assert title.startswith("GH-42: ")
        assert title.endswith("…")
        assert len(title) <= 80

    def test_empty_description_falls_back_to_id(self):
        t = _make_task(description="")
        assert _short_title(t) == "GH-42"


class TestIssueExtraction:
    def test_extracts_number(self):
        assert _extract_issue_number("https://github.com/foo/bar/issues/42") == 42

    def test_no_url(self):
        assert _extract_issue_number("") is None
        assert _extract_issue_number("not a url") is None

    def test_other_url(self):
        assert _extract_issue_number("https://github.com/foo/bar/pull/3") is None


# ── PR body rendering ─────────────────────────────────────────────────────────


class TestRenderBody:
    def test_with_issue_link(self):
        body = render_pr_body(_make_task(), cost_usd=0.42, duration_s=12.0, log_file="logs/x.log")
        assert "Closes #42." in body
        assert "GET /health returns 200" in body
        assert "$0.4200" in body
        assert "12.0s" in body
        assert "logs/x.log" in body
        assert "🤖 Opened by" in body

    def test_without_issue_link_with_prd_url(self):
        t = _make_task(prd_requirement="https://example.com/spec/123")
        body = render_pr_body(t)
        assert "Closes #" not in body
        assert "Implements [GH-42](https://example.com/spec/123)" in body

    def test_without_any_url(self):
        t = _make_task(prd_requirement="")
        body = render_pr_body(t)
        assert "Implements task `GH-42`." in body

    def test_omits_empty_sections(self):
        t = _make_task(acceptance_criteria=[], test_steps=[])
        body = render_pr_body(t)
        assert "Acceptance criteria" not in body
        assert "Validation" not in body


# ── open_pr_for_task with mocked subprocess ────────────────────────────────────


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestOpenPRForTask:
    def test_happy_path(self, tmp_path: Path):
        worktree = tmp_path
        push = _Proc(0, "")
        pr = _Proc(0, "https://github.com/foo/bar/pull/77\n")

        with patch.object(gp, "_run", side_effect=[push, pr]) as mock_run:
            result = open_pr_for_task(_make_task(), worktree_path=worktree)

        assert result.ok is True
        assert result.pr_url == "https://github.com/foo/bar/pull/77"
        assert result.branch == "whilly/GH-42"
        # First call is git push, second is gh pr create.
        first_cmd = mock_run.call_args_list[0].args[0]
        assert first_cmd[0] == "git"
        assert "push" in first_cmd

    def test_missing_worktree(self, tmp_path: Path):
        result = open_pr_for_task(_make_task(), worktree_path=tmp_path / "missing")
        assert result.ok is False
        assert "worktree not found" in result.reason

    def test_push_failure(self, tmp_path: Path):
        push = _Proc(1, "", "fatal: permission denied\n")
        with patch.object(gp, "_run", return_value=push):
            result = open_pr_for_task(_make_task(), worktree_path=tmp_path)
        assert result.ok is False
        assert "git push failed" in result.reason

    def test_gh_pr_create_failure(self, tmp_path: Path):
        push = _Proc(0)
        pr = _Proc(1, "", "validation failed\n")
        with patch.object(gp, "_run", side_effect=[push, pr]):
            result = open_pr_for_task(_make_task(), worktree_path=tmp_path)
        assert result.ok is False
        assert "gh pr create failed" in result.reason

    def test_pr_already_exists_returns_ok_with_url(self, tmp_path: Path):
        push = _Proc(0)
        pr_fail = _Proc(1, "", "a pull request already exists for this branch")
        view = _Proc(0, json.dumps({"url": "https://github.com/foo/bar/pull/12"}))
        with patch.object(gp, "_run", side_effect=[push, pr_fail, view]):
            result = open_pr_for_task(_make_task(), worktree_path=tmp_path)
        assert result.ok is True
        assert result.pr_url == "https://github.com/foo/bar/pull/12"

    def test_draft_flag_passed(self, tmp_path: Path):
        push = _Proc(0)
        pr = _Proc(0, "https://x/pr/1\n")
        with patch.object(gp, "_run", side_effect=[push, pr]) as mock_run:
            open_pr_for_task(_make_task(), worktree_path=tmp_path, draft=True)
        gh_cmd = mock_run.call_args_list[1].args[0]
        assert "--draft" in gh_cmd


class TestGitHubPRSinkClass:
    def test_open_delegates_to_module_function(self, tmp_path: Path):
        sink = GitHubPRSink(base_branch="develop", draft=True)
        with patch.object(gp, "open_pr_for_task", return_value=PRResult(ok=True, pr_url="x")) as m:
            res = sink.open(_make_task(), worktree_path=tmp_path)
        assert res.ok
        assert m.call_args.kwargs["base"] == "develop"
        assert m.call_args.kwargs["draft"] is True
        assert m.call_args.kwargs["branch_prefix"] == "whilly"
