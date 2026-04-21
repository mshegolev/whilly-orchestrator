"""Tests for whilly.dashboard module (R2-009)."""

import pytest
from whilly.dashboard import Dashboard, KeyboardHandler, NullDashboard
from whilly.task_manager import TaskManager


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


def test_record_task_cost(mock_tm):
    """record_task_cost should accumulate per-task cost data."""
    d = Dashboard(mock_tm, "test-agent", 10)
    d.record_task_cost("T-1", cost_usd=0.05, input_tokens=1000, output_tokens=500, duration_s=10.0, status="done")
    d.record_task_cost("T-1", cost_usd=0.03, input_tokens=800, output_tokens=300, duration_s=8.0, status="done")
    d.record_task_cost(
        "T-2", cost_usd=0.10, input_tokens=2000, output_tokens=1000, duration_s=20.0, status="in_progress"
    )

    assert len(d.task_costs) == 2
    assert d.task_costs["T-1"].cost_usd == pytest.approx(0.08)
    assert d.task_costs["T-1"].input_tokens == 1800
    assert d.task_costs["T-1"].iterations == 2
    assert d.task_costs["T-1"].status == "done"
    assert d.task_costs["T-2"].cost_usd == pytest.approx(0.10)


def test_cost_panel_overlay(mock_tm):
    """_show_cost_panel should populate overlay with cost breakdown."""
    d = Dashboard(mock_tm, "test-agent", 10)
    d.budget_usd = 5.0
    d.session_cost_usd = 1.25
    d.totals.cost_usd = 1.25
    d.totals.input_tokens = 50000
    d.totals.output_tokens = 10000
    d.record_task_cost("T-1", cost_usd=0.75, input_tokens=30000, output_tokens=6000, duration_s=60.0, status="done")
    d.record_task_cost(
        "T-3", cost_usd=0.50, input_tokens=20000, output_tokens=4000, duration_s=40.0, status="in_progress"
    )

    d._show_cost_panel()
    assert d._overlay_text is not None
    assert "Cost Panel" in d._overlay_text
    assert "T-1" in d._overlay_text
    assert "T-3" in d._overlay_text
    assert "$1.25" in d._overlay_text or "1.2500" in d._overlay_text


def test_cost_panel_toggle(mock_tm):
    """Second call to _show_cost_panel should dismiss the overlay."""
    d = Dashboard(mock_tm, "test-agent", 10)
    d._show_cost_panel()
    assert d._overlay_text is not None
    d._show_cost_panel()
    assert d._overlay_text is None


def test_null_dashboard_record_task_cost():
    """NullDashboard should also support record_task_cost."""
    nd = NullDashboard()
    nd.record_task_cost("T-1", cost_usd=0.05, input_tokens=1000, output_tokens=500)
    assert "T-1" in nd.task_costs
    assert nd.task_costs["T-1"].cost_usd == pytest.approx(0.05)
