"""The report must retain recorded timing and stable attempt identity for Gantt."""

from datetime import datetime, timezone

from whilly.swarm.runtime import _attempt_view


def test_report_attempt_retains_recorded_timing():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    view = _attempt_view(
        {"attempt": 1, "status": "running", "task_id": "task-1", "started_at": start, "finished_at": None},
        full=True,
    )
    assert view.get("started_at") == start
    assert view.get("task_id") == "task-1"
    assert view["finished_at"] is None
