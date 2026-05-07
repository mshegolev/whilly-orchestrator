"""Shared operator read models for browser and terminal dashboards."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final


class OperatorSurface(str, Enum):
    """Stable information architecture shared by TUI and web renderers."""

    OVERVIEW = "overview"
    COMPLIANCE = "compliance"
    PLANS_TASKS = "plans_tasks"
    WORKERS = "workers"
    EVENTS = "events"


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


@dataclass(frozen=True)
class ComplianceSummary:
    total_tasks: int
    tasks_by_status: Mapping[str, int]
    workers_online: int
    workers_total: int
    failed_tasks: int
    open_review_gaps: int


@dataclass(frozen=True)
class OperatorSnapshot:
    rendered_at: datetime
    summary: ComplianceSummary
    tasks: tuple[OperatorTaskRow, ...]
    workers: tuple[WorkerRow, ...]
    events: tuple[EventRow, ...]
    review_gaps: tuple[ReviewGap, ...]


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
SELECT id, task_id, plan_id, event_type, created_at, COALESCE(detail, payload, '{}'::jsonb) AS detail
FROM events
WHERE ($1::text IS NULL OR plan_id = $1)
ORDER BY created_at DESC, id DESC
LIMIT $2
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
    return build_operator_snapshot(
        tasks=task_rows,
        workers=worker_rows,
        events=event_rows,
        rendered_at=rendered_at,
    )


def build_operator_snapshot(
    *,
    tasks: Sequence[Mapping[str, Any]],
    workers: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    rendered_at: datetime | None = None,
) -> OperatorSnapshot:
    """Build a pure value snapshot from database-like mappings."""

    task_rows = tuple(_task_row(row) for row in tasks)
    worker_rows = tuple(_worker_row(row) for row in workers)
    event_rows = tuple(_event_row(row) for row in events)
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
        detail=dict(row.get("detail") or {}),
    )


def _review_gaps(tasks: Sequence[OperatorTaskRow]) -> tuple[ReviewGap, ...]:
    gaps: list[ReviewGap] = []
    for task in tasks:
        if not task.acceptance_criteria:
            gaps.append(ReviewGap(task_id=task.task_id, plan_id=task.plan_id, reason="missing acceptance criteria"))
            continue
        if task.status in {"PENDING", "DONE"}:
            gaps.append(ReviewGap(task_id=task.task_id, plan_id=task.plan_id, reason="pending verification evidence"))
        if _mentions_human_review(task) and task.status != "DONE":
            gaps.append(ReviewGap(task_id=task.task_id, plan_id=task.plan_id, reason="awaiting human review"))
    rank = {
        "missing acceptance criteria": 0,
        "awaiting human review": 1,
        "pending verification evidence": 2,
    }
    return tuple(sorted(gaps, key=lambda gap: (rank.get(gap.reason, 99), gap.plan_id, gap.task_id)))


def _mentions_human_review(task: OperatorTaskRow) -> bool:
    text = " ".join((*task.acceptance_criteria, *task.test_steps)).lower()
    return "human" in text or "approval" in text or "review" in text


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
    fields = (row.task_id, row.plan_id, row.status, row.priority, row.claimed_by or "")
    return any(needle in field.lower() for field in fields)


def _matches_worker(row: WorkerRow, needle: str) -> bool:
    fields = (row.worker_id, row.hostname, row.owner_email or "", row.status)
    return any(needle in field.lower() for field in fields)


def _matches_event(row: EventRow, needle: str) -> bool:
    fields = (str(row.event_id), row.task_id or "", row.plan_id or "", row.event_type, str(row.detail))
    return any(needle in field.lower() for field in fields)


def _matches_gap(row: ReviewGap, needle: str) -> bool:
    fields = (row.task_id, row.plan_id, row.reason)
    return any(needle in field.lower() for field in fields)


__all__ = [
    "EVENTS_LIMIT",
    "TASKS_LIMIT",
    "WORKERS_LIMIT",
    "ComplianceSummary",
    "EventRow",
    "OperatorSnapshot",
    "OperatorSurface",
    "OperatorTaskRow",
    "ReviewGap",
    "WorkerRow",
    "build_operator_snapshot",
    "fetch_operator_snapshot",
    "filter_snapshot",
]
