"""Tests for whilly.doctor — the read-only diagnostic."""

from __future__ import annotations

import json
from pathlib import Path

from whilly.doctor import (
    _extract_gh_issue_nums,
    diagnose_plan,
    format_report,
    run_doctor,
)


def _write_plan(path: Path, tasks: list[dict], **extra: object) -> Path:
    payload = {"project": "test", "tasks": tasks, **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_clean_directory_has_no_findings(tmp_path: Path) -> None:
    report = run_doctor(cwd=tmp_path, check_gh=False)
    assert report.findings == []
    assert "All clean" in format_report(report, color=False)


def test_ghost_plan_all_done(tmp_path: Path) -> None:
    _write_plan(tmp_path / "tasks-old.json", [{"id": "T1", "status": "done"}])
    d = diagnose_plan(tmp_path / "tasks-old.json")
    assert d.kind == "ghost"
    assert "resolved" in d.detail


def test_ghost_plan_empty_tasks(tmp_path: Path) -> None:
    _write_plan(tmp_path / "tasks-empty.json", [])
    d = diagnose_plan(tmp_path / "tasks-empty.json")
    assert d.kind == "ghost"
    assert "0 tasks" in d.detail


def test_invalid_name_flagged(tmp_path: Path) -> None:
    p = tmp_path / "github-https:--example.com-tasks.json"
    _write_plan(p, [{"id": "T1", "status": "pending"}])
    d = diagnose_plan(p)
    assert d.kind == "invalid_name"


def test_healthy_plan(tmp_path: Path) -> None:
    p = _write_plan(tmp_path / "tasks-live.json", [{"id": "T1", "status": "pending"}])
    d = diagnose_plan(p)
    assert d.kind == "healthy"


def test_ghost_when_all_linked_issues_closed(tmp_path: Path) -> None:
    tasks = [
        {"id": "GH-101", "status": "pending"},
        {"id": "GH-102", "status": "pending"},
    ]
    p = _write_plan(tmp_path / "tasks-backlog.json", tasks)
    d = diagnose_plan(p, issues_state={101: "CLOSED", 102: "CLOSED"})
    assert d.kind == "ghost"
    assert "CLOSED" in d.detail


def test_stale_when_some_issues_closed(tmp_path: Path) -> None:
    tasks = [
        {"id": "GH-101", "status": "pending"},
        {"id": "GH-102", "status": "pending"},
    ]
    p = _write_plan(tmp_path / "tasks-mixed.json", tasks)
    d = diagnose_plan(p, issues_state={101: "CLOSED", 102: "OPEN"})
    assert d.kind == "stale"
    assert "1/2" in d.detail


def test_extract_gh_nums_from_prd_requirement_url() -> None:
    tasks = [
        {"id": "T1", "prd_requirement": "https://github.com/owner/repo/issues/42"},
        {"id": "T2", "prd_requirement": "https://github.com/owner/repo/issues/7#issuecomment-1"},
        {"id": "gh-99-slug"},
    ]
    assert _extract_gh_issue_nums(tasks) == [7, 42, 99]


def test_tasks_json_canonical_name_is_not_orphan(tmp_path: Path) -> None:
    # tasks.json is the canonical default plan, never reported as orphan.
    _write_plan(tmp_path / "tasks.json", [{"id": "T1", "status": "pending"}])
    report = run_doctor(cwd=tmp_path, check_gh=False)
    assert report.plans == []
    assert report.findings == []


def test_stale_state_file_detected(tmp_path: Path) -> None:
    (tmp_path / ".whilly_state.json").write_text(
        json.dumps({"plan_file": str(tmp_path / "nonexistent.json")}),
        encoding="utf-8",
    )
    report = run_doctor(cwd=tmp_path, check_gh=False)
    assert report.stale_state_file is not None
    assert "stale_state" in report.findings


def test_orphan_workspaces_detected(tmp_path: Path) -> None:
    ws = tmp_path / ".whilly_workspaces" / "some-slug"
    ws.mkdir(parents=True)
    report = run_doctor(cwd=tmp_path, check_gh=False)
    assert len(report.orphan_workspaces) == 1
    assert any(f.startswith("workspaces:") for f in report.findings)


def test_exit_code_via_findings(tmp_path: Path) -> None:
    # ghost plan ⇒ has findings ⇒ CLI should exit 1
    _write_plan(tmp_path / "tasks-zombie.json", [])
    report = run_doctor(cwd=tmp_path, check_gh=False)
    assert bool(report.findings) is True
