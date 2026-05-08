"""Shared operator read models for browser and terminal dashboards."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final, Literal

from whilly.pipeline.human_review import (
    HUMAN_REVIEW_APPROVED,
    HUMAN_REVIEW_CHANGES_REQUESTED,
    HUMAN_REVIEW_REJECTED,
    HUMAN_REVIEW_REQUIRED,
)


class OperatorSurface(str, Enum):
    """Stable information architecture shared by TUI and web renderers."""

    OVERVIEW = "overview"
    COMPLIANCE = "compliance"
    PLANS_TASKS = "plans_tasks"
    WORKERS = "workers"
    EVENTS = "events"


class OperatorTable(str, Enum):
    """Stable operator table identifiers shared by TUI and web renderers."""

    TASKS = "tasks"
    WORKERS = "workers"
    REVIEW_GAPS = "review_gaps"
    EVENTS = "events"


OperatorMedium = Literal["tui", "wui"]


@dataclass(frozen=True)
class OperatorTableColumn:
    field_key: str
    canonical_label: str
    tui_label: str | None = None
    wui_label: str | None = None
    show_tui: bool = True
    show_wui: bool = True
    medium_note: str = ""

    def label_for(self, medium: OperatorMedium) -> str:
        if medium == "tui":
            return self.tui_label or self.canonical_label
        if medium == "wui":
            return self.wui_label or self.canonical_label
        raise ValueError(f"unsupported operator medium: {medium}")


OPERATOR_SURFACE_LABELS: Final[Mapping[OperatorSurface, str]] = {
    OperatorSurface.OVERVIEW: "Overview",
    OperatorSurface.COMPLIANCE: "Compliance",
    OperatorSurface.PLANS_TASKS: "Plans/Tasks",
    OperatorSurface.WORKERS: "Workers",
    OperatorSurface.EVENTS: "Events",
}


OPERATOR_TABLE_COLUMNS: Final[Mapping[OperatorTable, tuple[OperatorTableColumn, ...]]] = {
    OperatorTable.TASKS: (
        OperatorTableColumn("task_id", "Task"),
        OperatorTableColumn("plan_id", "Plan"),
        OperatorTableColumn("status", "Status"),
        OperatorTableColumn("priority", "Priority"),
        OperatorTableColumn("claimed_by", "Worker"),
        OperatorTableColumn("human_review", "Review"),
        OperatorTableColumn(
            "updated_at",
            "Updated",
            show_tui=False,
            show_wui=True,
            medium_note="WUI shows update time; TUI omits it for width.",
        ),
    ),
    OperatorTable.WORKERS: (
        OperatorTableColumn("worker_id", "Worker"),
        OperatorTableColumn(
            "hostname",
            "Hostname",
            tui_label="Host",
            medium_note="TUI uses a compact label only.",
        ),
        OperatorTableColumn("owner_email", "Owner"),
        OperatorTableColumn("status", "Status"),
        OperatorTableColumn(
            "last_heartbeat",
            "Last heartbeat",
            tui_label="Heartbeat",
            medium_note="TUI uses a compact label only.",
        ),
    ),
    OperatorTable.REVIEW_GAPS: (
        OperatorTableColumn(
            "selected",
            "Sel",
            show_tui=True,
            show_wui=False,
            medium_note="WUI uses selected row outline and aria-selected.",
        ),
        OperatorTableColumn("task_id", "Task"),
        OperatorTableColumn("plan_id", "Plan"),
        OperatorTableColumn("reason", "Reason"),
        OperatorTableColumn("stage_id", "Stage"),
        OperatorTableColumn("reviewer", "Reviewer"),
        OperatorTableColumn("actions", "Actions"),
    ),
    OperatorTable.EVENTS: (
        OperatorTableColumn("event_id", "Id"),
        OperatorTableColumn("task_id", "Task"),
        OperatorTableColumn("plan_id", "Plan"),
        OperatorTableColumn("event_type", "Type"),
        OperatorTableColumn("created_at", "At"),
    ),
}


def operator_surface_items() -> tuple[tuple[OperatorSurface, str], ...]:
    """Return operator surfaces and labels in shared display order."""

    return tuple((surface, OPERATOR_SURFACE_LABELS[surface]) for surface in OperatorSurface)


def operator_table_columns(table: OperatorTable | str, medium: OperatorMedium) -> tuple[OperatorTableColumn, ...]:
    """Return visible column metadata for an operator table and medium."""

    operator_table = table if isinstance(table, OperatorTable) else OperatorTable(table)
    if medium not in {"tui", "wui"}:
        raise ValueError(f"unsupported operator medium: {medium}")
    return tuple(
        column
        for column in OPERATOR_TABLE_COLUMNS[operator_table]
        if (column.show_tui if medium == "tui" else column.show_wui)
    )


def operator_table_labels(table: OperatorTable | str, medium: OperatorMedium) -> tuple[str, ...]:
    """Return visible column labels for an operator table and medium."""

    return tuple(column.label_for(medium) for column in operator_table_columns(table, medium))


@dataclass(frozen=True)
class HumanReviewState:
    required: bool = False
    decision: str | None = None
    stage_id: str = ""
    reason: str = ""
    reviewer: str | None = None
    approval_channel: str = ""


@dataclass(frozen=True)
class OperatorTaskRow:
    task_id: str
    plan_id: str
    status: str
    priority: str
    claimed_by: str | None
    started_at: datetime | None
    updated_at: datetime
    acceptance_criteria: tuple[str, ...] = ()
    test_steps: tuple[str, ...] = ()
    human_review: HumanReviewState = field(default_factory=HumanReviewState)


@dataclass(frozen=True)
class WorkerRow:
    worker_id: str
    hostname: str
    owner_email: str | None
    status: str
    last_heartbeat: datetime


@dataclass(frozen=True)
class EventRow:
    event_id: int
    task_id: str | None
    plan_id: str | None
    event_type: str
    created_at: datetime
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class ReviewGap:
    task_id: str
    plan_id: str
    reason: str
    stage_id: str = ""
    reviewer: str | None = None
    approval_channel: str = ""
    actionable: bool = False


@dataclass(frozen=True)
class ComplianceSummary:
    total_tasks: int
    tasks_by_status: Mapping[str, int]
    workers_online: int
    workers_total: int
    failed_tasks: int
    open_review_gaps: int


@dataclass(frozen=True)
class OperatorControlState:
    paused: bool = False
    pause_reason: str | None = None
    paused_by: str | None = None
    paused_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class OperatorSnapshot:
    rendered_at: datetime
    summary: ComplianceSummary
    tasks: tuple[OperatorTaskRow, ...]
    workers: tuple[WorkerRow, ...]
    events: tuple[EventRow, ...]
    review_gaps: tuple[ReviewGap, ...]
    control_state: OperatorControlState = field(default_factory=OperatorControlState)


TASKS_LIMIT: Final[int] = 200
WORKERS_LIMIT: Final[int] = 200
EVENTS_LIMIT: Final[int] = 200

_TASKS_SQL: Final[str] = """
SELECT id, plan_id, status, priority, claimed_by, claimed_at, updated_at, acceptance_criteria, test_steps
FROM tasks
WHERE ($1::text IS NULL OR plan_id = $1)
ORDER BY
    CASE status
        WHEN 'IN_PROGRESS' THEN 0
        WHEN 'CLAIMED' THEN 1
        WHEN 'PENDING' THEN 2
        WHEN 'FAILED' THEN 3
        WHEN 'DONE' THEN 4
        WHEN 'SKIPPED' THEN 5
        ELSE 6
    END,
    updated_at DESC
LIMIT $2
"""

_WORKERS_SQL: Final[str] = """
SELECT worker_id, hostname, owner_email, status, last_heartbeat
FROM workers
ORDER BY status ASC, last_heartbeat DESC
LIMIT $1
"""

_EVENTS_SQL: Final[str] = """
SELECT id, task_id, plan_id, event_type, created_at, COALESCE(payload, '{}'::jsonb) AS payload, detail
FROM events
WHERE ($1::text IS NULL OR plan_id = $1)
ORDER BY created_at DESC, id DESC
LIMIT $2
"""

_HUMAN_REVIEW_EVENTS_SQL: Final[str] = """
SELECT id, task_id, plan_id, event_type, created_at, COALESCE(payload, '{}'::jsonb) AS payload, detail
FROM events
WHERE task_id = ANY($1::text[])
  AND event_type LIKE 'human_review.%'
ORDER BY created_at ASC, id ASC
"""

_CONTROL_STATE_SQL: Final[str] = """
SELECT paused, pause_reason, paused_by, paused_at, updated_at
FROM control_state
WHERE id = 'global'
LIMIT 1
"""


async def fetch_operator_snapshot(
    pool: Any,
    *,
    plan_id: str | None = None,
    rendered_at: datetime | None = None,
    tasks_limit: int = TASKS_LIMIT,
    workers_limit: int = WORKERS_LIMIT,
    events_limit: int = EVENTS_LIMIT,
) -> OperatorSnapshot:
    """Fetch the shared operator snapshot from Postgres."""

    async with pool.acquire() as conn:
        task_rows = await conn.fetch(_TASKS_SQL, plan_id, tasks_limit)
        worker_rows = await conn.fetch(_WORKERS_SQL, workers_limit)
        event_rows = await conn.fetch(_EVENTS_SQL, plan_id, events_limit)
        control_state_rows = await conn.fetch(_CONTROL_STATE_SQL)
        task_ids = [str(row["id"]) for row in task_rows]
        human_review_rows = await conn.fetch(_HUMAN_REVIEW_EVENTS_SQL, task_ids) if task_ids else []
    return build_operator_snapshot(
        tasks=task_rows,
        workers=worker_rows,
        events=event_rows,
        human_review_events=human_review_rows,
        control_state=control_state_rows[0] if control_state_rows else None,
        rendered_at=rendered_at,
    )


def build_operator_snapshot(
    *,
    tasks: Sequence[Mapping[str, Any]],
    workers: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    human_review_events: Sequence[Mapping[str, Any]] | None = None,
    control_state: Mapping[str, Any] | None = None,
    rendered_at: datetime | None = None,
) -> OperatorSnapshot:
    """Build a pure value snapshot from database-like mappings."""

    task_rows = tuple(_task_row(row) for row in tasks)
    worker_rows = tuple(_worker_row(row) for row in workers)
    event_rows = tuple(_event_row(row) for row in events)
    human_review_event_rows = tuple(_event_row(row) for row in (human_review_events or ()))
    human_review_by_task = human_review_states_from_events((*event_rows, *human_review_event_rows))
    task_rows = tuple(
        replace(task, human_review=human_review_by_task.get(task.task_id, task.human_review)) for task in task_rows
    )
    review_gaps = _review_gaps(task_rows)
    by_status: dict[str, int] = {}
    for task in task_rows:
        by_status[task.status] = by_status.get(task.status, 0) + 1
    summary = ComplianceSummary(
        total_tasks=len(task_rows),
        tasks_by_status=by_status,
        workers_online=sum(1 for worker in worker_rows if worker.status.lower() == "online"),
        workers_total=len(worker_rows),
        failed_tasks=by_status.get("FAILED", 0),
        open_review_gaps=len(review_gaps),
    )
    return OperatorSnapshot(
        rendered_at=rendered_at or datetime.now(tz=UTC),
        summary=summary,
        tasks=task_rows,
        workers=worker_rows,
        events=event_rows,
        review_gaps=review_gaps,
        control_state=_control_state(control_state),
    )


def filter_snapshot(snapshot: OperatorSnapshot, query: str) -> OperatorSnapshot:
    """Return a snapshot whose row collections match ``query``."""

    needle = query.strip().lower()
    if not needle:
        return snapshot
    return OperatorSnapshot(
        rendered_at=snapshot.rendered_at,
        summary=snapshot.summary,
        tasks=tuple(row for row in snapshot.tasks if _matches_task(row, needle)),
        workers=tuple(row for row in snapshot.workers if _matches_worker(row, needle)),
        events=tuple(row for row in snapshot.events if _matches_event(row, needle)),
        review_gaps=tuple(row for row in snapshot.review_gaps if _matches_gap(row, needle)),
        control_state=snapshot.control_state,
    )


def _control_state(row: Mapping[str, Any] | None) -> OperatorControlState:
    if row is None:
        return OperatorControlState()
    return OperatorControlState(
        paused=bool(row.get("paused")),
        pause_reason=_optional_str(row.get("pause_reason")),
        paused_by=_optional_str(row.get("paused_by")),
        paused_at=row.get("paused_at"),
        updated_at=row.get("updated_at"),
    )


def _task_row(row: Mapping[str, Any]) -> OperatorTaskRow:
    return OperatorTaskRow(
        task_id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        status=str(row["status"]),
        priority=str(row["priority"]),
        claimed_by=_optional_str(row.get("claimed_by")),
        started_at=row.get("claimed_at"),
        updated_at=row["updated_at"],
        acceptance_criteria=_string_tuple(row.get("acceptance_criteria")),
        test_steps=_string_tuple(row.get("test_steps")),
    )


def _worker_row(row: Mapping[str, Any]) -> WorkerRow:
    return WorkerRow(
        worker_id=str(row["worker_id"]),
        hostname=str(row["hostname"]),
        owner_email=_optional_str(row.get("owner_email")),
        status=str(row["status"]),
        last_heartbeat=row["last_heartbeat"],
    )


def _event_row(row: Mapping[str, Any]) -> EventRow:
    return EventRow(
        event_id=int(row["id"]),
        task_id=_optional_str(row.get("task_id")),
        plan_id=_optional_str(row.get("plan_id")),
        event_type=str(row["event_type"]),
        created_at=row["created_at"],
        detail=_merged_event_detail(row),
    )


def human_review_states_from_events(events: Sequence[EventRow]) -> Mapping[str, HumanReviewState]:
    """Derive the latest human-review checkpoint state per task from audit events."""

    checkpoints: dict[tuple[str, str], HumanReviewState] = {}
    ordered = sorted(events, key=lambda event: (event.created_at, event.event_id))
    for event in ordered:
        if not event.event_type.startswith("human_review."):
            continue
        task_id = event.task_id or _string_value(event.detail.get("task_id"))
        if not task_id:
            continue
        stage_id = _string_value(event.detail.get("stage_id"))
        key = (task_id, stage_id)
        previous = checkpoints.get(key, HumanReviewState(stage_id=stage_id))
        if event.event_type == HUMAN_REVIEW_REQUIRED:
            checkpoints[key] = HumanReviewState(
                required=True,
                decision=None,
                stage_id=stage_id,
                reason=_string_value(event.detail.get("reason")),
                reviewer=None,
                approval_channel=_string_value(event.detail.get("approval_channel")),
            )
            continue

        decision = _human_review_decision(event.event_type)
        if decision is None:
            continue
        checkpoints[key] = HumanReviewState(
            required=True,
            decision=decision,
            stage_id=stage_id or previous.stage_id,
            reason=previous.reason or _string_value(event.detail.get("reason")),
            reviewer=_optional_str(event.detail.get("reviewer")),
            approval_channel=previous.approval_channel or _string_value(event.detail.get("approval_channel")),
        )

    by_task: dict[str, HumanReviewState] = {}
    for (task_id, _), state in checkpoints.items():
        current = by_task.get(task_id)
        if current is None or _human_review_state_rank(state) < _human_review_state_rank(current):
            by_task[task_id] = state
    return by_task


def _review_gaps(tasks: Sequence[OperatorTaskRow]) -> tuple[ReviewGap, ...]:
    gaps: list[ReviewGap] = []
    for task in tasks:
        if not task.acceptance_criteria:
            gaps.append(ReviewGap(task_id=task.task_id, plan_id=task.plan_id, reason="missing acceptance criteria"))
            continue
        if task.status in {"PENDING", "DONE"}:
            gaps.append(ReviewGap(task_id=task.task_id, plan_id=task.plan_id, reason="pending verification evidence"))
        if task.human_review.required and task.human_review.decision != "approved":
            gaps.append(
                ReviewGap(
                    task_id=task.task_id,
                    plan_id=task.plan_id,
                    reason=_human_review_gap_reason(task.human_review),
                    stage_id=task.human_review.stage_id,
                    reviewer=task.human_review.reviewer,
                    approval_channel=task.human_review.approval_channel,
                    actionable=True,
                )
            )
        elif _mentions_human_review(task) and task.status != "DONE":
            gaps.append(ReviewGap(task_id=task.task_id, plan_id=task.plan_id, reason="awaiting human review"))
    rank = {
        "missing acceptance criteria": 0,
        "awaiting human review": 1,
        "human review changes requested": 2,
        "human review rejected": 3,
        "pending verification evidence": 4,
    }
    return tuple(sorted(gaps, key=lambda gap: (rank.get(gap.reason, 99), gap.plan_id, gap.task_id)))


def _merged_event_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    payload = row.get("payload")
    detail = row.get("detail")
    merged.update(_json_mapping(detail))
    merged.update(_json_mapping(payload))
    return merged


def _json_mapping(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return dict(raw)


def _human_review_decision(event_type: str) -> str | None:
    if event_type == HUMAN_REVIEW_APPROVED:
        return "approved"
    if event_type == HUMAN_REVIEW_REJECTED:
        return "rejected"
    if event_type == HUMAN_REVIEW_CHANGES_REQUESTED:
        return "changes_requested"
    return None


def _human_review_state_rank(state: HumanReviewState) -> int:
    if state.required and state.decision != "approved":
        return 0
    if state.required:
        return 1
    return 2


def _human_review_gap_reason(state: HumanReviewState) -> str:
    if state.decision == "changes_requested":
        return "human review changes requested"
    if state.decision == "rejected":
        return "human review rejected"
    return "awaiting human review"


def _mentions_human_review(task: OperatorTaskRow) -> bool:
    text = " ".join((*task.acceptance_criteria, *task.test_steps)).lower()
    return "human" in text or "approval" in text or "review" in text


def _string_value(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw)


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw)
    return (str(raw),)


def _optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    return str(raw)


def _matches_task(row: OperatorTaskRow, needle: str) -> bool:
    fields = (
        row.task_id,
        row.plan_id,
        row.status,
        row.priority,
        row.claimed_by or "",
        row.human_review.stage_id,
        row.human_review.decision or "",
        row.human_review.reason,
        row.human_review.reviewer or "",
    )
    return any(needle in field.lower() for field in fields)


def _matches_worker(row: WorkerRow, needle: str) -> bool:
    fields = (row.worker_id, row.hostname, row.owner_email or "", row.status)
    return any(needle in field.lower() for field in fields)


def _matches_event(row: EventRow, needle: str) -> bool:
    fields = (str(row.event_id), row.task_id or "", row.plan_id or "", row.event_type, str(row.detail))
    return any(needle in field.lower() for field in fields)


def _matches_gap(row: ReviewGap, needle: str) -> bool:
    fields = (row.task_id, row.plan_id, row.reason, row.stage_id, row.reviewer or "", row.approval_channel)
    return any(needle in field.lower() for field in fields)


__all__ = [
    "EVENTS_LIMIT",
    "OPERATOR_SURFACE_LABELS",
    "OPERATOR_TABLE_COLUMNS",
    "TASKS_LIMIT",
    "WORKERS_LIMIT",
    "ComplianceSummary",
    "EventRow",
    "HumanReviewState",
    "OperatorMedium",
    "OperatorSnapshot",
    "OperatorSurface",
    "OperatorTable",
    "OperatorTableColumn",
    "OperatorTaskRow",
    "ReviewGap",
    "WorkerRow",
    "build_operator_snapshot",
    "fetch_operator_snapshot",
    "filter_snapshot",
    "human_review_states_from_events",
    "operator_surface_items",
    "operator_table_columns",
    "operator_table_labels",
]
