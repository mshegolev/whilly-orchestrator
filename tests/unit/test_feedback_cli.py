from __future__ import annotations

from whilly.cli.feedback import run_feedback_command
from whilly.feedback import GitHubIssueResult


def test_feedback_dry_run_prints_gh_command(capsys) -> None:
    called = False

    def creator(**kwargs: object) -> GitHubIssueResult:
        nonlocal called
        called = True
        assert kwargs["dry_run"] is True
        assert kwargs["repo"] == "owner/repo"
        assert kwargs["title"] == "Update failed"
        assert kwargs["labels"] == ("bug", "whilly")
        assert "Cannot reach PyPI" in str(kwargs["body"])
        return GitHubIssueResult(
            ok=True,
            command=("gh", "issue", "create", "--repo", "owner/repo", "--title", "Update failed"),
            dry_run=True,
        )

    rc = run_feedback_command(
        [
            "--kind",
            "bug",
            "--repo",
            "owner/repo",
            "--title",
            "Update failed",
            "--body",
            "Cannot reach PyPI",
            "--dry-run",
        ],
        creator=creator,
    )

    assert called is True
    assert rc == 0
    assert "Would create GitHub issue:" in capsys.readouterr().out


def test_feedback_create_prints_issue_url(capsys) -> None:
    def creator(**_kwargs: object) -> GitHubIssueResult:
        return GitHubIssueResult(ok=True, issue_url="https://github.com/owner/repo/issues/7")

    rc = run_feedback_command(
        ["--kind", "idea", "--repo", "owner/repo", "--title", "Add GitLab support", "--body", "please"],
        creator=creator,
    )

    assert rc == 0
    assert "Created GitHub issue: https://github.com/owner/repo/issues/7" in capsys.readouterr().out


def test_feedback_failure_prints_reason(capsys) -> None:
    def creator(**_kwargs: object) -> GitHubIssueResult:
        return GitHubIssueResult(ok=False, returncode=2, reason="gh auth required")

    rc = run_feedback_command(
        ["--kind", "bug", "--repo", "owner/repo", "--title", "Broken", "--body", "details"],
        creator=creator,
    )

    assert rc == 2
    assert "gh auth required" in capsys.readouterr().err


def test_feedback_body_file_is_used(tmp_path, capsys) -> None:
    body_file = tmp_path / "report.md"
    body_file.write_text("from file", encoding="utf-8")

    def creator(**kwargs: object) -> GitHubIssueResult:
        assert "from file" in str(kwargs["body"])
        return GitHubIssueResult(ok=True, issue_url="https://github.com/owner/repo/issues/8")

    rc = run_feedback_command(
        ["--repo", "owner/repo", "--title", "Broken", "--body-file", str(body_file)],
        creator=creator,
    )

    assert rc == 0
    assert "issues/8" in capsys.readouterr().out
