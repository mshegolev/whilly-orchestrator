"""Unit tests for rollback safety-net contracts."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from whilly.rollback.git_ops import GitClient
from whilly.rollback.models import PreflightReport, ProtectionSignal, RollbackPoint, WorktreeState
from whilly.rollback.service import (
    RollbackError,
    build_preflight_report,
    confirmation_phrase,
    create_rollback_point,
    list_rollback_points,
    restore_to_ref,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc


def _seed_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "checkout", "-b", "main")
    _git(path, "config", "user.email", "whilly-tests@example.invalid")
    _git(path, "config", "user.name", "Whilly Tests")
    _commit_file(path, "README.md", "initial\n", "initial commit")
    return path


def _commit_file(repo: Path, relative_path: str, content: str, message: str) -> str:
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", message)
    return _head(repo)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _two_commit_repo(path: Path) -> tuple[Path, str, str]:
    repo = _seed_repo(path)
    first_sha = _head(repo)
    second_sha = _commit_file(repo, "README.md", "second\n", "second commit")
    return repo, first_sha, second_sha


def test_preflight_report_to_dict_includes_auditable_fields() -> None:
    created_at = datetime(2026, 5, 8, 16, 45, 0, tzinfo=UTC)
    rollback_point = RollbackPoint(
        name="whilly/rollback/main/20260508T164500Z-abcdef123456",
        target_sha="abcdef1234567890",
        branch="main",
        created_at=created_at,
        message="Whilly rollback point before push",
    )
    protection = ProtectionSignal(provider="github", status="unknown", reason="not requested")
    worktree = WorktreeState(
        repo_root=Path("/repo"),
        branch="main",
        head_sha="abcdef1234567890",
        upstream="origin/main",
        dirty=True,
        dirty_entries=(" M whilly/file.py", "?? scratch.txt"),
    )
    report = PreflightReport(
        operation="push",
        worktree=worktree,
        backup_points=(rollback_point,),
        protection=protection,
        blockers=("dirty worktree",),
        warnings=("branch protection unknown",),
    )

    assert report.ok is False
    assert report.to_dict() == {
        "operation": "push",
        "ok": False,
        "repo_root": "/repo",
        "branch": "main",
        "head_sha": "abcdef1234567890",
        "upstream": "origin/main",
        "dirty": True,
        "dirty_entries": [" M whilly/file.py", "?? scratch.txt"],
        "backup_points": [
            {
                "name": "whilly/rollback/main/20260508T164500Z-abcdef123456",
                "target_sha": "abcdef1234567890",
                "branch": "main",
                "created_at": "2026-05-08T16:45:00Z",
                "message": "Whilly rollback point before push",
            }
        ],
        "protection": {
            "provider": "github",
            "status": "unknown",
            "reason": "not requested",
        },
        "blockers": ["dirty worktree"],
        "warnings": ["branch protection unknown"],
    }


def test_preflight_report_ok_when_no_blockers() -> None:
    report = PreflightReport(
        operation="merge",
        worktree=WorktreeState(
            repo_root="/repo",
            branch="main",
            head_sha="abcdef1234567890",
            upstream=None,
            dirty=False,
            dirty_entries=(),
        ),
        backup_points=(),
        protection=ProtectionSignal(provider="", status="unknown", reason="not requested"),
        blockers=(),
        warnings=(),
    )

    assert report.ok is True


def test_git_client_uses_list_argv_and_cwd(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"argv": argv, **kwargs})
        return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

    import whilly.rollback.git_ops as git_ops

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    result = GitClient(tmp_path).run("branch", "--show-current", timeout=12.5)

    assert result.argv == ("git", "branch", "--show-current")
    assert result.returncode == 0
    assert result.stdout == "main\n"
    assert calls == [
        {
            "argv": ["git", "branch", "--show-current"],
            "cwd": tmp_path,
            "capture_output": True,
            "text": True,
            "timeout": 12.5,
            "check": False,
        }
    ]
    assert "shell" not in calls[0]


def test_create_backup_tag_uses_whilly_prefix(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path / "repo")
    head_sha = _head(repo)

    point = create_rollback_point(repo, operation="push", now=datetime(2026, 5, 8, 17, 0, 0, tzinfo=UTC))

    assert point.name == f"whilly/rollback/main/20260508T170000Z-{head_sha[:12]}"
    assert point.target_sha == head_sha
    assert point.branch == "main"
    assert _git(repo, "cat-file", "-t", point.name).stdout.strip() == "tag"


def test_create_backup_tag_passes_custom_message_to_annotated_tag(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path / "repo")

    point = create_rollback_point(
        repo,
        operation="merge",
        message="custom rollback evidence",
        now=datetime(2026, 5, 8, 17, 1, 0, tzinfo=UTC),
    )

    tag_body = _git(repo, "for-each-ref", f"refs/tags/{point.name}", "--format=%(contents)").stdout
    assert "custom rollback evidence" in tag_body


def test_list_rollback_points_returns_json_ready_points(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path / "repo")
    first = create_rollback_point(repo, operation="push", now=datetime(2026, 5, 8, 17, 0, 0, tzinfo=UTC))
    second = create_rollback_point(repo, operation="merge", now=datetime(2026, 5, 8, 17, 2, 0, tzinfo=UTC))

    points = list_rollback_points(repo)

    assert [point.name for point in points] == sorted([first.name, second.name])
    assert points[0].to_dict()["name"].startswith("whilly/rollback/main/")
    assert points[0].to_dict()["target_sha"] == _head(repo)
    assert set(points[0].to_dict()) == {"name", "target_sha", "branch", "created_at", "message"}


def test_preflight_report_contains_auditable_git_state(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path / "repo")
    point = create_rollback_point(repo, operation="push", now=datetime(2026, 5, 8, 17, 3, 0, tzinfo=UTC))
    (repo / "README.md").write_text("dirty tracked\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("dirty untracked\n", encoding="utf-8")

    report = build_preflight_report(repo, operation="push")
    data = report.to_dict()

    assert data["operation"] == "push"
    assert data["repo_root"] == str(repo.resolve())
    assert data["branch"] == "main"
    assert data["head_sha"] == _head(repo)
    assert data["upstream"] is None
    assert data["dirty"] is True
    assert data["dirty_entries"] == [" M README.md", "?? scratch.txt"]
    assert data["backup_points"] == [point.to_dict()]
    assert data["protection"] == {"provider": "", "status": "unknown", "reason": "not requested"}
    assert any("dirty worktree" in blocker for blocker in data["blockers"])
    assert any("branch protection unknown" in warning for warning in data["warnings"])


def test_preflight_reports_protection_unknown_not_unprotected(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path / "repo")

    report = build_preflight_report(repo, operation="push")

    assert report.protection == ProtectionSignal(provider="", status="unknown", reason="not requested")
    assert report.protection.status != "unprotected"
    assert report.ok is True
    assert any("branch protection unknown" in warning for warning in report.warnings)


def test_preflight_blocks_confirmed_protected_branch(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path / "repo")
    seen_keys: list[str] = []

    def probe(repo_root: Path, branch: str) -> ProtectionSignal:
        assert repo_root == repo.resolve()
        seen_keys.append(branch)
        return ProtectionSignal(provider="test", status="protected", reason="locked")

    reports = [
        build_preflight_report(repo, operation=operation, target_ref="main", protection_probe=probe)
        for operation in ("push", "merge", "restore")
    ]

    assert seen_keys == ["main", "main", "main"]
    assert all(report.protection.status == "protected" for report in reports)
    assert all(not report.ok for report in reports)
    assert all(any("protected" in blocker for blocker in report.blockers) for report in reports)


def test_restore_refuses_dirty_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo, first_sha, second_sha = _two_commit_repo(tmp_path / "repo")
    (repo / "README.md").write_text("dirty tracked\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    original_run = GitClient.run

    def record_run(self: GitClient, *args: str, timeout: float = 30.0):
        calls.append(args)
        return original_run(self, *args, timeout=timeout)

    monkeypatch.setattr(GitClient, "run", record_run)
    report = build_preflight_report(repo, operation="restore", target_ref=first_sha)

    with pytest.raises(RollbackError, match="dirty worktree"):
        restore_to_ref(repo, first_sha, confirm=confirmation_phrase(report, first_sha))

    assert _head(repo) == second_sha
    assert ("reset", "--hard", first_sha) not in calls


def test_restore_requires_exact_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo, first_sha, second_sha = _two_commit_repo(tmp_path / "repo")
    calls: list[tuple[str, ...]] = []
    original_run = GitClient.run

    def record_run(self: GitClient, *args: str, timeout: float = 30.0):
        calls.append(args)
        return original_run(self, *args, timeout=timeout)

    monkeypatch.setattr(GitClient, "run", record_run)

    with pytest.raises(RollbackError, match="confirmation"):
        restore_to_ref(repo, first_sha, confirm="restore the wrong thing")

    assert _head(repo) == second_sha
    assert ("reset", "--hard", first_sha) not in calls


def test_restore_dry_run_does_not_reset_head(tmp_path: Path) -> None:
    repo, first_sha, second_sha = _two_commit_repo(tmp_path / "repo")
    report = build_preflight_report(repo, operation="restore", target_ref=first_sha)

    result = restore_to_ref(repo, first_sha, confirm=confirmation_phrase(report, first_sha), dry_run=True)

    assert result.ok is True
    assert result.dry_run is True
    assert result.reset_performed is False
    assert result.target_sha == first_sha
    assert _head(repo) == second_sha


def test_restore_never_calls_git_clean_stash_or_checkout_dot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo, first_sha, _second_sha = _two_commit_repo(tmp_path / "repo")
    calls: list[tuple[str, ...]] = []
    original_run = GitClient.run

    def record_run(self: GitClient, *args: str, timeout: float = 30.0):
        calls.append(args)
        return original_run(self, *args, timeout=timeout)

    monkeypatch.setattr(GitClient, "run", record_run)
    report = build_preflight_report(repo, operation="restore", target_ref=first_sha)

    result = restore_to_ref(repo, first_sha, confirm=confirmation_phrase(report, first_sha))

    assert result.reset_performed is True
    assert _head(repo) == first_sha
    assert any(call == ("reset", "--hard", first_sha) for call in calls)
    assert all(call[0] not in {"clean", "stash"} for call in calls)
    assert ("checkout", ".") not in calls
