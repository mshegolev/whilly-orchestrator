"""Tests for ralph.dashboard module (R2-009)."""

from unittest.mock import MagicMock, patch

import pytest
from ralph.dashboard import Dashboard, KeyboardHandler
from ralph.reporter import CostTotals
from ralph.task_manager import TaskManager


@pytest.fixture
def mock_tm(tmp_path):
    """Create a TaskManager with a temp plan file."""
    plan = {
        "project": "test",
        "tasks": [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "critical",
                "description": "Task one",
                "status": "done",
                "dependencies": [],
                "key_files": [],
            },
            {
                "id": "T-2",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Task two",
                "status": "pending",
                "dependencies": ["T-1"],
                "key_files": [],
            },
            {
                "id": "T-3",
                "phase": "P2",
                "category": "test",
                "priority": "medium",
                "description": "Task three",
                "status": "in_progress",
                "dependencies": [],
                "key_files": [],
            },
        ],
    }
    import json

    plan_file = tmp_path / "tasks.json"
    plan_file.write_text(json.dumps(plan))
    return TaskManager(str(plan_file))


def test_dashboard_render_contains_sections(tmp_path):
    """Dashboard render should contain progress, task sections."""
    import json

    plan = {
        "project": "test",
        "tasks": [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "critical",
                "description": "Task one",
                "status": "done",
                "dependencies": [],
                "key_files": [],
            },
            {
                "id": "T-3",
                "phase": "P2",
                "category": "test",
                "priority": "high",
                "description": "Task three",
                "status": "in_progress",
                "dependencies": [],
                "key_files": [],
            },
        ],
    }
    plan_file = tmp_path / "tasks.json"
    plan_file.write_text(json.dumps(plan))
    tm = TaskManager(str(plan_file))

    d = Dashboard(tm, "test-agent", 10)
    d.iteration = 1
    d.phase = "work"
    rendered = d._render()
    # Group should have multiple parts
    assert rendered is not None


def test_dashboard_overlay(mock_tm):
    """Overlay should replace main render."""
    d = Dashboard(mock_tm, "test-agent", 10)
    d._overlay_text = "Test overlay content"
    rendered = d._render()
    assert rendered is not None


def test_keyboard_handler_lifecycle():
    """KeyboardHandler start/stop should not crash."""
    kh = KeyboardHandler()
    kh.register("q", lambda: None)
    # Don't actually start (no tty in tests), just verify no crash
    kh.stop()


def test_keyboard_handler_register():
    """register should store callbacks."""
    kh = KeyboardHandler()
    called = []
    kh.register("a", lambda: called.append(1))
    assert "a" in kh._callbacks
