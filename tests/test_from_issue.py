"""Unit tests for `--from-issue owner/repo#N` (issue #164).

Exercises the parser in :func:`whilly.sources.github_issues.parse_issue_ref`
and a stubbed end-to-end ``fetch_single_issue`` run that never talks to the
real ``gh`` CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whilly.sources import fetch_single_issue, parse_issue_ref


# ─── parse_issue_ref ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("alice/myrepo#42", ("alice/myrepo", 42)),
        ("  alice/myrepo#42  ", ("alice/myrepo", 42)),  # whitespace tolerated
        ("alice/myrepo/42", ("alice/myrepo", 42)),  # slash variant
        ("https://github.com/alice/myrepo/issues/42", ("alice/myrepo", 42)),
        ("https://github.com/alice/myrepo/issues/42/", ("alice/myrepo", 42)),  # trailing slash
        (
            "https://github.com/alice/myrepo/issues/42?source=email",
            ("alice/myrepo", 42),
        ),
        (
            "https://github.com/alice/myrepo/issues/42#issuecomment-9",
            ("alice/myrepo", 42),
        ),
        ("org-name/repo.with.dots#7", ("org-name/repo.with.dots", 7)),
    ],
)
def test_parse_issue_ref_accepts_canonical_forms(ref, expected):
    assert parse_issue_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        None,
        "just-a-word",
        "alice/myrepo",  # missing number
        "alice#42",  # missing repo
        "alice/myrepo#abc",  # non-numeric
        "https://github.com/alice/myrepo/pull/42",  # PR, not issue
        "https://gitlab.com/alice/myrepo/issues/42",  # non-github host
        "/leading-slash/repo#1",
    ],
)
def test_parse_issue_ref_rejects_bad_input(ref):
    with pytest.raises(ValueError):
        parse_issue_ref(ref)


# ─── fetch_single_issue (stubbed gh) ──────────────────────────────────────────


_FAKE_ISSUE = {
    "number": 42,
    "title": "[feature] the single-issue demo",
    "body": "## Problem\nreal body here.\n\n## Acceptance\n- thing works\n- other thing",
    "labels": [{"name": "whilly:ready"}],
    "url": "https://github.com/alice/myrepo/issues/42",
    "createdAt": "2024-01-01T00:00:00Z",
    "updatedAt": "2024-01-02T00:00:00Z",
    "state": "OPEN",
}


def test_fetch_single_issue_writes_plan_file(tmp_path, monkeypatch):
    from whilly.sources import github_issues as gi

    class _Proc:
        returncode = 0
        stdout = json.dumps(_FAKE_ISSUE)
        stderr = ""

    monkeypatch.setattr(gi, "_run_gh", lambda args, timeout=30: _Proc())

    out_path = tmp_path / "tasks-issue.json"
    plan_path, stats = fetch_single_issue("alice/myrepo#42", out_path=out_path)

    assert plan_path == out_path.resolve()
    assert out_path.is_file()
    assert stats.new == 1
    assert stats.updated == 0

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(data["tasks"]) == 1
    task = data["tasks"][0]
    assert task["id"] == "GH-42"
    assert task["status"] == "pending"
    assert "the single-issue demo" in task["description"]
    assert task["acceptance_criteria"] == ["thing works", "other thing"]


def test_fetch_single_issue_idempotent_on_rerun(tmp_path, monkeypatch):
    from whilly.sources import github_issues as gi

    class _Proc:
        returncode = 0
        stdout = json.dumps(_FAKE_ISSUE)
        stderr = ""

    monkeypatch.setattr(gi, "_run_gh", lambda args, timeout=30: _Proc())

    out_path = tmp_path / "tasks-issue.json"
    _, first = fetch_single_issue("alice/myrepo#42", out_path=out_path)
    _, second = fetch_single_issue("alice/myrepo#42", out_path=out_path)

    assert first.new == 1 and first.updated == 0
    assert second.new == 0 and second.updated == 1


def test_fetch_single_issue_propagates_gh_errors(tmp_path, monkeypatch):
    from whilly.sources import github_issues as gi

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "gh: issue not found"

    monkeypatch.setattr(gi, "_run_gh", lambda args, timeout=30: _Proc())

    with pytest.raises(RuntimeError, match="gh issue view"):
        fetch_single_issue("alice/myrepo#999", out_path=tmp_path / "plan.json")


def test_fetch_single_issue_accepts_url_reference(tmp_path, monkeypatch):
    from whilly.sources import github_issues as gi

    class _Proc:
        returncode = 0
        stdout = json.dumps(_FAKE_ISSUE)
        stderr = ""

    captured: list[list[str]] = []

    def fake_run(args, timeout=30):
        captured.append(args)
        return _Proc()

    monkeypatch.setattr(gi, "_run_gh", fake_run)

    fetch_single_issue(
        "https://github.com/alice/myrepo/issues/42",
        out_path=tmp_path / "plan.json",
    )
    # Ensure we called gh with the right owner/repo and number.
    args = captured[0]
    assert "issue" in args and "view" in args
    assert "42" in args
    assert "alice/myrepo" in args


def test_fetch_single_issue_warns_on_closed_issue(tmp_path, monkeypatch, caplog):
    from whilly.sources import github_issues as gi

    closed = dict(_FAKE_ISSUE, state="CLOSED")

    class _Proc:
        returncode = 0
        stdout = json.dumps(closed)
        stderr = ""

    monkeypatch.setattr(gi, "_run_gh", lambda args, timeout=30: _Proc())

    with caplog.at_level("WARNING", logger="whilly"):
        plan_path, stats = fetch_single_issue("alice/myrepo#42", out_path=tmp_path / "plan.json")

    assert stats.new == 1
    assert any("not open" in rec.message for rec in caplog.records)
    # Plan still written — the merge logic decides whether to skip-on-next-run.
    assert Path(plan_path).is_file()
