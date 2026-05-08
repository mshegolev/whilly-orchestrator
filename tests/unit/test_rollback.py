"""Unit tests for rollback safety-net contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from whilly.rollback.git_ops import GitClient
from whilly.rollback.models import PreflightReport, ProtectionSignal, RollbackPoint, WorktreeState


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
