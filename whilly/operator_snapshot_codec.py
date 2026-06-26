"""JSON (de)serialization for OperatorSnapshot — the single wire schema
shared by the HTTP operator-snapshot endpoint and the TUI HTTP backend."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _human_review_to_dict(h: HumanReviewState) -> dict[str, Any]:
    return {
        "required": h.required,
        "decision": h.decision,
        "stage_id": h.stage_id,
        "reason": h.reason,
        "reviewer": h.reviewer,
        "approval_channel": h.approval_channel,
    }


def _human_review_from_dict(d: dict[str, Any]) -> HumanReviewState:
    return HumanReviewState(
        required=d.get("required", False),
        decision=d.get("decision"),
        stage_id=d.get("stage_id", ""),
        reason=d.get("reason", ""),
        reviewer=d.get("reviewer"),
        approval_channel=d.get("approval_channel", ""),
    )


def _task_to_dict(t: OperatorTaskRow) -> dict[str, Any]:
    return {
        "task_id": t.task_id,
        "plan_id": t.plan_id,
        "status": t.status,
        "priority": t.priority,
        "claimed_by": t.claimed_by,
        "started_at": _dt(t.started_at),
        "updated_at": _dt(t.updated_at),
        "acceptance_criteria": list(t.acceptance_criteria),
        "test_steps": list(t.test_steps),
        "human_review": _human_review_to_dict(t.human_review),
        "version": t.version,
        "description": t.description,
        "key_files": list(t.key_files),
        "dependencies": list(t.dependencies),
    }


def _task_from_dict(d: dict[str, Any]) -> OperatorTaskRow:
    return OperatorTaskRow(
        task_id=d["task_id"],
        plan_id=d["plan_id"],
        status=d["status"],
        priority=d["priority"],
        claimed_by=d.get("claimed_by"),
        started_at=_opt_dt(d.get("started_at")),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        acceptance_criteria=tuple(d.get("acceptance_criteria", ())),
        test_steps=tuple(d.get("test_steps", ())),
        human_review=_human_review_from_dict(d.get("human_review", {})),
        version=d.get("version", 0),
        description=d.get("description", ""),
        key_files=tuple(d.get("key_files", ())),
        dependencies=tuple(d.get("dependencies", ())),
    )


def _worker_to_dict(w: WorkerRow) -> dict[str, Any]:
    return {
        "worker_id": w.worker_id,
        "hostname": w.hostname,
        "owner_email": w.owner_email,
        "status": w.status,
        "last_heartbeat": _dt(w.last_heartbeat),
    }


def _worker_from_dict(d: dict[str, Any]) -> WorkerRow:
    return WorkerRow(
        worker_id=d["worker_id"],
        hostname=d["hostname"],
        owner_email=d.get("owner_email"),
        status=d["status"],
        last_heartbeat=datetime.fromisoformat(d["last_heartbeat"]),
    )


def _event_to_dict(e: EventRow) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "task_id": e.task_id,
        "plan_id": e.plan_id,
        "event_type": e.event_type,
        "created_at": _dt(e.created_at),
        "detail": dict(e.detail),
    }


def _event_from_dict(d: dict[str, Any]) -> EventRow:
    return EventRow(
        event_id=d["event_id"],
        task_id=d.get("task_id"),
        plan_id=d.get("plan_id"),
        event_type=d["event_type"],
        created_at=datetime.fromisoformat(d["created_at"]),
        detail=dict(d.get("detail", {})),
    )


def _gap_to_dict(g: ReviewGap) -> dict[str, Any]:
    return {
        "task_id": g.task_id,
        "plan_id": g.plan_id,
        "reason": g.reason,
        "stage_id": g.stage_id,
        "reviewer": g.reviewer,
        "approval_channel": g.approval_channel,
        "actionable": g.actionable,
    }


def _gap_from_dict(d: dict[str, Any]) -> ReviewGap:
    return ReviewGap(
        task_id=d["task_id"],
        plan_id=d["plan_id"],
        reason=d["reason"],
        stage_id=d.get("stage_id", ""),
        reviewer=d.get("reviewer"),
        approval_channel=d.get("approval_channel", ""),
        actionable=d.get("actionable", False),
    )


def _summary_to_dict(s: ComplianceSummary) -> dict[str, Any]:
    return {
        "total_tasks": s.total_tasks,
        "tasks_by_status": dict(s.tasks_by_status),
        "workers_online": s.workers_online,
        "workers_total": s.workers_total,
        "failed_tasks": s.failed_tasks,
        "open_review_gaps": s.open_review_gaps,
    }


def _summary_from_dict(d: dict[str, Any]) -> ComplianceSummary:
    return ComplianceSummary(
        total_tasks=d["total_tasks"],
        tasks_by_status=dict(d["tasks_by_status"]),
        workers_online=d["workers_online"],
        workers_total=d["workers_total"],
        failed_tasks=d["failed_tasks"],
        open_review_gaps=d["open_review_gaps"],
    )


def _control_to_dict(c: OperatorControlState) -> dict[str, Any]:
    return {
        "paused": c.paused,
        "pause_reason": c.pause_reason,
        "paused_by": c.paused_by,
        "paused_at": _dt(c.paused_at),
        "updated_at": _dt(c.updated_at),
    }


def _control_from_dict(d: dict[str, Any]) -> OperatorControlState:
    return OperatorControlState(
        paused=d.get("paused", False),
        pause_reason=d.get("pause_reason"),
        paused_by=d.get("paused_by"),
        paused_at=_opt_dt(d.get("paused_at")),
        updated_at=_opt_dt(d.get("updated_at")),
    )


def snapshot_to_dict(snap: OperatorSnapshot) -> dict[str, Any]:
    return {
        "rendered_at": _dt(snap.rendered_at),
        "summary": _summary_to_dict(snap.summary),
        "tasks": [_task_to_dict(t) for t in snap.tasks],
        "workers": [_worker_to_dict(w) for w in snap.workers],
        "events": [_event_to_dict(e) for e in snap.events],
        "review_gaps": [_gap_to_dict(g) for g in snap.review_gaps],
        "control_state": _control_to_dict(snap.control_state),
    }


def snapshot_from_dict(payload: dict[str, Any]) -> OperatorSnapshot:
    return OperatorSnapshot(
        rendered_at=datetime.fromisoformat(payload["rendered_at"]),
        summary=_summary_from_dict(payload["summary"]),
        tasks=tuple(_task_from_dict(t) for t in payload.get("tasks", ())),
        workers=tuple(_worker_from_dict(w) for w in payload.get("workers", ())),
        events=tuple(_event_from_dict(e) for e in payload.get("events", ())),
        review_gaps=tuple(_gap_from_dict(g) for g in payload.get("review_gaps", ())),
        control_state=_control_from_dict(payload.get("control_state", {})),
    )
