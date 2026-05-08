from __future__ import annotations

from datetime import UTC, datetime, timedelta

from whilly.operator_views import (
    ComplianceSummary,
    EventRow,
    OperatorSnapshot,
    OperatorSurface,
    OperatorTable,
    OperatorTaskRow,
    ReviewGap,
    WorkerRow,
    build_operator_snapshot,
    filter_snapshot,
    operator_surface_items,
    operator_table_columns,
    operator_table_labels,
)


def test_operator_surface_items_pin_shared_order_and_labels() -> None:
    assert operator_surface_items() == (
        (OperatorSurface.OVERVIEW, "Overview"),
        (OperatorSurface.COMPLIANCE, "Compliance"),
        (OperatorSurface.PLANS_TASKS, "Plans/Tasks"),
        (OperatorSurface.WORKERS, "Workers"),
        (OperatorSurface.EVENTS, "Events"),
    )


def test_operator_task_table_contract_pins_medium_specific_columns() -> None:
    assert tuple(column.field_key for column in operator_table_columns(OperatorTable.TASKS, "wui")) == (
        "task_id",
        "plan_id",
        "status",
        "priority",
        "claimed_by",
        "human_review",
        "updated_at",
    )
    assert operator_table_labels("tasks", "wui") == (
        "Task",
        "Plan",
        "Status",
        "Priority",
        "Worker",
        "Review",
        "Updated",
    )
    assert tuple(column.field_key for column in operator_table_columns("tasks", "tui")) == (
        "task_id",
        "plan_id",
        "status",
        "priority",
        "claimed_by",
        "human_review",
    )
    assert operator_table_labels(OperatorTable.TASKS, "tui") == (
        "Task",
        "Plan",
        "Status",
        "Priority",
        "Worker",
        "Review",
    )
    updated_column = next(column for column in operator_table_columns("tasks", "wui") if column.field_key == "updated_at")
    assert updated_column.medium_note


def test_operator_worker_table_contract_pins_shared_field_order_and_labels() -> None:
    expected_fields = ("worker_id", "hostname", "owner_email", "status", "last_heartbeat")

    assert tuple(column.field_key for column in operator_table_columns("workers", "wui")) == expected_fields
    assert tuple(column.field_key for column in operator_table_columns("workers", "tui")) == expected_fields
    assert operator_table_labels("workers", "wui") == ("Worker", "Hostname", "Owner", "Status", "Last heartbeat")
    assert operator_table_labels("workers", "tui") == ("Worker", "Host", "Owner", "Status", "Heartbeat")


def test_operator_review_table_contract_pins_selection_difference() -> None:
    assert tuple(column.field_key for column in operator_table_columns("review_gaps", "wui")) == (
        "task_id",
        "plan_id",
        "reason",
        "stage_id",
        "reviewer",
        "actions",
    )
    assert operator_table_labels("review_gaps", "wui") == (
        "Task",
        "Plan",
        "Reason",
        "Stage",
        "Reviewer",
        "Actions",
    )
    assert tuple(column.field_key for column in operator_table_columns("review_gaps", "tui")) == (
        "selected",
        "task_id",
        "plan_id",
        "reason",
        "stage_id",
        "reviewer",
        "actions",
    )
    assert operator_table_labels("review_gaps", "tui") == (
        "Sel",
        "Task",
        "Plan",
        "Reason",
        "Stage",
        "Reviewer",
        "Actions",
    )
    selected_column = next(
        column for column in operator_table_columns("review_gaps", "tui") if column.field_key == "selected"
    )
    assert selected_column.medium_note


def test_operator_event_table_contract_pins_labels_for_both_media() -> None:
    expected_fields = ("event_id", "task_id", "plan_id", "event_type", "created_at")
    expected_labels = ("Id", "Task", "Plan", "Type", "At")

    assert tuple(column.field_key for column in operator_table_columns("events", "wui")) == expected_fields
    assert tuple(column.field_key for column in operator_table_columns("events", "tui")) == expected_fields
    assert operator_table_labels("events", "wui") == expected_labels
    assert operator_table_labels("events", "tui") == expected_labels


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


def test_human_review_required_event_opens_gap_until_approved() -> None:
    now = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
    task = {
        "id": "T-release",
        "plan_id": "P-release",
        "status": "IN_PROGRESS",
        "priority": "critical",
        "claimed_by": "worker-release",
        "claimed_at": now,
        "updated_at": now,
        "acceptance_criteria": ["release evidence attached"],
        "test_steps": ["pytest -q"],
    }
    required = {
        "id": 1,
        "task_id": "T-release",
        "plan_id": "P-release",
        "event_type": "human_review.required",
        "created_at": now,
        "payload": {
            "task_id": "T-release",
            "plan_id": "P-release",
            "stage_id": "release_review",
            "reason": "stage_human_gate",
        },
        "detail": {
            "task_id": "T-release",
            "plan_id": "P-release",
            "stage_id": "diagnostic_stage",
            "reason": "diagnostic_reason",
        },
    }

    waiting = build_operator_snapshot(tasks=[task], workers=[], events=[required], rendered_at=now)

    assert waiting.review_gaps == (
        ReviewGap(
            task_id="T-release",
            plan_id="P-release",
            reason="awaiting human review",
            stage_id="release_review",
            actionable=True,
        ),
    )

    approved = {
        "id": 2,
        "task_id": "T-release",
        "plan_id": "P-release",
        "event_type": "human_review.approved",
        "created_at": now,
        "detail": {
            "task_id": "T-release",
            "plan_id": "P-release",
            "stage_id": "release_review",
            "reviewer": "lead@example.com",
        },
    }

    closed = build_operator_snapshot(tasks=[task], workers=[], events=[required, approved], rendered_at=now)

    assert closed.review_gaps == ()


def test_human_review_rejected_and_changes_requested_keep_actionable_gaps() -> None:
    now = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
    task = {
        "id": "T-release",
        "plan_id": "P-release",
        "status": "IN_PROGRESS",
        "priority": "critical",
        "claimed_by": "worker-release",
        "claimed_at": now,
        "updated_at": now,
        "acceptance_criteria": ["release evidence attached"],
        "test_steps": ["pytest -q"],
    }
    required = {
        "id": 1,
        "task_id": "T-release",
        "plan_id": "P-release",
        "event_type": "human_review.required",
        "created_at": now,
        "payload": {
            "task_id": "T-release",
            "plan_id": "P-release",
            "stage_id": "release_review",
            "reason": "stage_human_gate",
        },
    }
    rejected = {
        "id": 2,
        "task_id": "T-release",
        "plan_id": "P-release",
        "event_type": "human_review.rejected",
        "created_at": now,
        "payload": {
            "task_id": "T-release",
            "plan_id": "P-release",
            "stage_id": "release_review",
            "reviewer": "lead@example.com",
        },
    }
    changes_requested = {
        "id": 3,
        "task_id": "T-release",
        "plan_id": "P-release",
        "event_type": "human_review.changes_requested",
        "created_at": now,
        "payload": {
            "task_id": "T-release",
            "plan_id": "P-release",
            "stage_id": "release_review",
            "reviewer": "lead@example.com",
        },
    }

    rejected_snapshot = build_operator_snapshot(tasks=[task], workers=[], events=[required, rejected], rendered_at=now)
    changes_snapshot = build_operator_snapshot(
        tasks=[task],
        workers=[],
        events=[required, changes_requested],
        rendered_at=now,
    )

    assert rejected_snapshot.review_gaps == (
        ReviewGap(
            task_id="T-release",
            plan_id="P-release",
            reason="human review rejected",
            stage_id="release_review",
            reviewer="lead@example.com",
            actionable=True,
        ),
    )
    assert changes_snapshot.review_gaps == (
        ReviewGap(
            task_id="T-release",
            plan_id="P-release",
            reason="human review changes requested",
            stage_id="release_review",
            reviewer="lead@example.com",
            actionable=True,
        ),
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
