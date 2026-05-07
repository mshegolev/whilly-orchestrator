from __future__ import annotations

from datetime import UTC, datetime, timedelta

from whilly.operator_views import (
    ComplianceSummary,
    EventRow,
    OperatorSnapshot,
    OperatorSurface,
    OperatorTaskRow,
    ReviewGap,
    WorkerRow,
    build_operator_snapshot,
    filter_snapshot,
)


def test_build_operator_snapshot_summarizes_operator_surfaces() -> None:
    now = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)

    snapshot = build_operator_snapshot(
        tasks=[
            {
                "id": "T-ready",
                "plan_id": "P-1",
                "status": "PENDING",
                "priority": "high",
                "claimed_by": None,
                "claimed_at": None,
                "updated_at": now,
                "acceptance_criteria": ["done"],
                "test_steps": ["pytest"],
            },
            {
                "id": "T-human",
                "plan_id": "P-1",
                "status": "IN_PROGRESS",
                "priority": "critical",
                "claimed_by": "worker-a",
                "claimed_at": now - timedelta(minutes=12),
                "updated_at": now - timedelta(minutes=5),
                "acceptance_criteria": [],
                "test_steps": ["human approval required"],
            },
            {
                "id": "T-review",
                "plan_id": "P-2",
                "status": "DONE",
                "priority": "medium",
                "claimed_by": "worker-b",
                "claimed_at": now - timedelta(hours=1),
                "updated_at": now - timedelta(minutes=2),
                "acceptance_criteria": ["merged"],
                "test_steps": [],
            },
        ],
        workers=[
            {
                "worker_id": "worker-a",
                "hostname": "alpha.local",
                "owner_email": "ops@example.com",
                "status": "online",
                "last_heartbeat": now,
            }
        ],
        events=[
            {
                "id": 7,
                "task_id": "T-human",
                "plan_id": "P-1",
                "event_type": "START",
                "created_at": now,
                "detail": {"worker_id": "worker-a"},
            }
        ],
        rendered_at=now,
    )

    assert snapshot.rendered_at == now
    assert snapshot.summary.total_tasks == 3
    assert snapshot.summary.tasks_by_status["IN_PROGRESS"] == 1
    assert snapshot.summary.workers_online == 1
    assert snapshot.summary.open_review_gaps == 3
    assert [surface.value for surface in OperatorSurface] == [
        "overview",
        "compliance",
        "plans_tasks",
        "workers",
        "events",
    ]
    assert snapshot.review_gaps == (
        ReviewGap(task_id="T-human", plan_id="P-1", reason="missing acceptance criteria"),
        ReviewGap(task_id="T-ready", plan_id="P-1", reason="pending verification evidence"),
        ReviewGap(task_id="T-review", plan_id="P-2", reason="pending verification evidence"),
    )


def test_filter_snapshot_keeps_matching_rows_across_surfaces() -> None:
    now = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
    snapshot = OperatorSnapshot(
        rendered_at=now,
        summary=ComplianceSummary(
            total_tasks=2,
            tasks_by_status={"PENDING": 1, "DONE": 1},
            workers_online=1,
            workers_total=2,
            failed_tasks=0,
            open_review_gaps=1,
        ),
        tasks=(
            OperatorTaskRow(
                task_id="T-alpha",
                plan_id="P-alpha",
                status="PENDING",
                priority="high",
                claimed_by=None,
                started_at=None,
                updated_at=now,
                acceptance_criteria=("done",),
                test_steps=("pytest",),
            ),
            OperatorTaskRow(
                task_id="T-beta",
                plan_id="P-beta",
                status="DONE",
                priority="low",
                claimed_by="worker-beta",
                started_at=now,
                updated_at=now,
                acceptance_criteria=("done",),
                test_steps=("pytest",),
            ),
        ),
        workers=(
            WorkerRow(
                worker_id="worker-alpha",
                hostname="alpha.local",
                owner_email="ops@example.com",
                status="online",
                last_heartbeat=now,
            ),
        ),
        events=(
            EventRow(
                event_id=3,
                task_id="T-beta",
                plan_id="P-beta",
                event_type="COMPLETE",
                created_at=now,
                detail={},
            ),
        ),
        review_gaps=(ReviewGap(task_id="T-alpha", plan_id="P-alpha", reason="pending verification evidence"),),
    )

    filtered = filter_snapshot(snapshot, "alpha")

    assert [row.task_id for row in filtered.tasks] == ["T-alpha"]
    assert [row.worker_id for row in filtered.workers] == ["worker-alpha"]
    assert filtered.events == ()
    assert filtered.review_gaps == snapshot.review_gaps
    assert filtered.summary == snapshot.summary
