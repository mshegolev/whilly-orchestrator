from __future__ import annotations

import subprocess
from pathlib import Path

from whilly.feedback import FeedbackKind, build_feedback_body, create_github_issue, default_labels


def test_default_labels_include_kind_and_whilly() -> None:
    assert default_labels(FeedbackKind.BUG) == ("bug", "whilly")
    assert default_labels(FeedbackKind.IDEA) == ("idea", "whilly")


def test_build_feedback_body_includes_context_and_redacts_secret() -> None:
    body = build_feedback_body(
        kind=FeedbackKind.BUG,
        title="Update failed",
        message="token ghp_1234567890abcdefghijklmnopqrstuvwxyz leaked in logs",
        command="whilly update check",
    )

    assert "## Report" in body
    assert "- kind: `bug`" in body
    assert "- command: `whilly update check`" in body
    assert "## Environment" in body
    assert "ghp_1234567890" not in body
    assert "[REDACTED:" in body


def test_create_github_issue_dry_run_returns_command_without_runner(tmp_path: Path) -> None:
    called = False

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner should not be called")

    result = create_github_issue(
        repo="owner/repo",
        title="Bug report",
        body="details",
        labels=("bug", "whilly"),
        dry_run=True,
        gh_bin="gh",
        runner=runner,
    )

    assert called is False
    assert result.ok is True
    assert result.dry_run is True
    assert result.command[:5] == ("gh", "issue", "create", "--repo", "owner/repo")
    assert "--body-file" in result.command


def test_create_github_issue_invokes_gh_with_body_file(tmp_path: Path) -> None:
    captured: list[tuple[tuple[str, ...], str]] = []

    def runner(args: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        body_file = Path(args[args.index("--body-file") + 1])
        captured.append((args, body_file.read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/repo/issues/7\n", stderr="")

    result = create_github_issue(
        repo="owner/repo",
        title="Bug report",
        body="details",
        labels=("bug", "whilly"),
        dry_run=False,
        gh_bin="gh",
        runner=runner,
    )

    assert result.ok is True
    assert result.issue_url == "https://github.com/owner/repo/issues/7"
    assert result.command[:9] == (
        "gh",
        "issue",
        "create",
        "--repo",
        "owner/repo",
        "--title",
        "Bug report",
        "--label",
        "bug,whilly",
    )
    assert result.command[-2] == "--body-file"
    assert captured == [(result.command, "details")]


def test_create_github_issue_reports_gh_failure() -> None:
    def runner(args: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 4, stdout="", stderr="auth failed")

    result = create_github_issue(
        repo="owner/repo",
        title="Bug report",
        body="details",
        labels=("bug",),
        gh_bin="gh",
        runner=runner,
    )

    assert result.ok is False
    assert result.returncode == 4
    assert "auth failed" in result.reason
