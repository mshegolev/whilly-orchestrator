# tests/test_operator_snapshot_codec.py
from datetime import datetime, timezone

from whilly.operator_snapshot_codec import snapshot_from_dict, snapshot_to_dict
from whilly.operator_views import (
    ComplianceSummary,
    EventRow,
    HumanReviewState,
    OperatorControlState,
    OperatorSnapshot,
    OperatorTaskRow,
    ReviewGap,
    WorkerRow,
)


def _sample() -> OperatorSnapshot:
    ts = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    return OperatorSnapshot(
        rendered_at=ts,
        summary=ComplianceSummary(
            total_tasks=3,
            tasks_by_status={"PENDING": 2, "DONE": 1},
            workers_online=1,
            workers_total=2,
            failed_tasks=0,
            open_review_gaps=1,
        ),
        tasks=(
            OperatorTaskRow(
                task_id="t1",
                plan_id="p1",
                status="IN_PROGRESS",
                priority="P1",
                claimed_by="w1",
                started_at=ts,
                updated_at=ts,
                acceptance_criteria=("ac1",),
                test_steps=("ts1",),
                human_review=HumanReviewState(required=True, decision=None, stage_id="s1"),
                version=2,
                description="d",
                key_files=("a.py",),
                dependencies=("t0",),
            ),
        ),
        workers=(WorkerRow(worker_id="w1", hostname="h", owner_email=None, status="online", last_heartbeat=ts),),
        events=(
            EventRow(event_id=7, task_id="t1", plan_id="p1", event_type="claimed", created_at=ts, detail={"k": "v"}),
        ),
        review_gaps=(ReviewGap(task_id="t1", plan_id="p1", reason="needs review", stage_id="s1", actionable=True),),
        control_state=OperatorControlState(paused=False),
    )


def test_snapshot_round_trips():
    snap = _sample()
    assert snapshot_from_dict(snapshot_to_dict(snap)) == snap


def test_unknown_keys_are_ignored():
    payload = snapshot_to_dict(_sample())
    payload["future_field"] = 123
    payload["tasks"][0]["future_task_field"] = "x"
    assert snapshot_from_dict(payload) == _sample()


def test_missing_required_key_raises():
    payload = snapshot_to_dict(_sample())
    del payload["summary"]
    import pytest

    with pytest.raises(KeyError):
        snapshot_from_dict(payload)
