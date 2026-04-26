"""Tests for TaskManager.reload() resilience against partial-write races.

The dashboard re-reads the plan file every render frame, while agent
subprocesses (Claude CLI) may be writing the same file non-atomically.
``reload()`` must tolerate a transient JSONDecodeError without crashing
the orchestrator.
"""

import json

import pytest
from whilly.task_manager import TaskManager


def _write_plan(path, status="pending"):
    plan = {
        "project": "race-test",
        "tasks": [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Task one",
                "status": status,
                "dependencies": [],
                "key_files": [],
            }
        ],
    }
    path.write_text(json.dumps(plan))


def test_reload_first_load_propagates_error(tmp_path):
    """First reload (no prior snapshot) must surface real errors."""
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        TaskManager(str(bad))


def test_reload_keeps_snapshot_on_partial_write(tmp_path):
    """After a successful load, a corrupted file should not crash reload()."""
    plan_file = tmp_path / "tasks.json"
    _write_plan(plan_file, status="done")
    tm = TaskManager(str(plan_file))
    assert tm.tasks[0].status == "done"

    # Simulate an in-flight non-atomic write: file contains a truncated
    # JSON object (writer hasn't closed the string/object yet).
    plan_file.write_text('{"project": "race-test", "tasks": [{"id": "T-1", "status": "pen')

    tm.reload()  # must not raise
    assert tm.tasks, "should keep last good snapshot when current read is partial"
    assert tm.tasks[0].status == "done"


def test_reload_recovers_after_writer_finishes(tmp_path):
    """Once the writer commits a valid file, the next reload picks it up."""
    plan_file = tmp_path / "tasks.json"
    _write_plan(plan_file, status="pending")
    tm = TaskManager(str(plan_file))
    assert tm.tasks[0].status == "pending"

    # Corrupt -> reload keeps prior snapshot
    plan_file.write_text("{garbage")
    tm.reload()
    assert tm.tasks[0].status == "pending"

    # Writer finishes -> next reload sees the new data
    _write_plan(plan_file, status="done")
    tm.reload()
    assert tm.tasks[0].status == "done"
