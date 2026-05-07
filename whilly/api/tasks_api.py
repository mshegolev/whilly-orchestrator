"""JSON listing endpoint helpers for ``GET /api/v1/tasks`` (m3-tasks-api).

Designed for use by the M3 dashboard's initial render and external
integrations. The route itself is registered inside
:func:`whilly.adapters.transport.server.create_app`; this module owns
the SQL projection, cursor encoding, and response shape so the route
handler stays a thin call-and-serialise wrapper.

Cursor model
------------
Cursors are opaque to clients: an URL-safe base64 of a JSON tuple
``[priority_rank, task_id]`` carrying the row's sort key. The next
page resumes strictly after that pair under the deterministic order
``(priority_rank ASC, id ASC)`` (PRIORITY_ORDER → critical=0, high=1,
medium=2, low=3 — equivalent to "priority DESC" in the validation
contract). This shape keeps pagination stable across mid-flight
inserts: a row inserted with a higher rank than the cursor (e.g. a
new ``critical`` while the cursor is mid-``medium``) is simply not
included on subsequent pages — the row's own first-page lookup
returns it on a fresh request.

Why opaque rather than ``cursor=<task_id>``?
    Decoding ``(priority_rank, task_id)`` server-side lets the SQL
    use a strict-tuple comparison (``(rank, id) > ($cursor_rank,
    $cursor_id)``) which Postgres can satisfy with the existing
    primary key index. Splitting the cursor across two query
    parameters would couple every client to the server's sort key.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any, Final

import asyncpg

from whilly.core.models import TaskStatus
from whilly.operator_views import EventRow, HumanReviewState, human_review_states_from_events

PRIORITY_ORDER_SQL: Final[str] = (
    "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
)

DEFAULT_LIMIT: Final[int] = 50
MAX_LIMIT: Final[int] = 500

_TASK_COLUMNS: Final[str] = (
    "id, plan_id, status, priority, claimed_by, claimed_at, version, "
    "key_files, description, acceptance_criteria, test_steps"
)

_LIST_TASKS_FIRST_PAGE_SQL: Final[str] = f"""
SELECT {_TASK_COLUMNS},
       {PRIORITY_ORDER_SQL} AS priority_rank
FROM tasks
WHERE plan_id = $1
  AND ($2::text IS NULL OR status = $2)
ORDER BY priority_rank ASC, id ASC
LIMIT $3
"""

_LIST_TASKS_AFTER_CURSOR_SQL: Final[str] = f"""
SELECT {_TASK_COLUMNS},
       {PRIORITY_ORDER_SQL} AS priority_rank
FROM tasks
WHERE plan_id = $1
  AND ($2::text IS NULL OR status = $2)
  AND ({PRIORITY_ORDER_SQL}, id) > ($3::int, $4::text)
ORDER BY priority_rank ASC, id ASC
LIMIT $5
"""

_LIST_HUMAN_REVIEW_EVENTS_SQL: Final[str] = """
SELECT id, task_id, plan_id, event_type, created_at, payload, detail
FROM events
WHERE task_id = ANY($1::text[])
  AND event_type LIKE 'human_review.%'
ORDER BY created_at ASC, id ASC
"""


class CursorDecodeError(ValueError):
    """Raised when a client supplies a malformed ``cursor`` query param."""


def encode_cursor(priority_rank: int, task_id: str) -> str:
    payload = json.dumps([priority_rank, task_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[int, str]:
    if not value:
        raise CursorDecodeError("cursor is empty")
    padding = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError) as exc:
        raise CursorDecodeError(f"cursor is not valid base64url: {exc}") from None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CursorDecodeError(f"cursor payload is not valid JSON: {exc}") from None
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not isinstance(decoded[0], int)
        or not isinstance(decoded[1], str)
    ):
        raise CursorDecodeError("cursor payload must be [priority_rank: int, id: str]")
    return decoded[0], decoded[1]


def _row_to_payload(row: asyncpg.Record, human_review: HumanReviewState | None = None) -> dict[str, Any]:
    claimed_at = row["claimed_at"]
    return {
        "id": row["id"],
        "plan_id": row["plan_id"],
        "status": row["status"],
        "priority": row["priority"],
        "claimed_by": row["claimed_by"],
        "claimed_at": claimed_at.isoformat() if claimed_at is not None else None,
        "version": int(row["version"]),
        "key_files": _decode_json_list(row["key_files"]),
        "description": row["description"] or "",
        "acceptance_criteria": _decode_json_list(row["acceptance_criteria"]),
        "test_steps": _decode_json_list(row["test_steps"]),
        "human_review": _human_review_payload(human_review),
    }


def _decode_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return decoded
        return []
    return list(value)


async def list_tasks(
    pool: asyncpg.Pool,
    *,
    plan_id: str,
    status_filter: TaskStatus | None,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    status_value: str | None = status_filter.value if status_filter is not None else None
    fetch_limit = limit + 1

    if cursor is None:
        sql = _LIST_TASKS_FIRST_PAGE_SQL
        args: tuple[Any, ...] = (plan_id, status_value, fetch_limit)
    else:
        cursor_rank, cursor_id = decode_cursor(cursor)
        sql = _LIST_TASKS_AFTER_CURSOR_SQL
        args = (plan_id, status_value, cursor_rank, cursor_id, fetch_limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        task_ids = [str(row["id"]) for row in page_rows]
        event_rows = await conn.fetch(_LIST_HUMAN_REVIEW_EVENTS_SQL, task_ids) if task_ids else []

    human_review_by_task = human_review_states_from_events(tuple(_event_row(row) for row in event_rows))
    tasks = [_row_to_payload(r, human_review_by_task.get(str(r["id"]))) for r in page_rows]
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(int(last["priority_rank"]), last["id"])

    return {"tasks": tasks, "next_cursor": next_cursor}


def _event_row(row: asyncpg.Record) -> EventRow:
    return EventRow(
        event_id=int(row["id"]),
        task_id=str(row["task_id"]) if row["task_id"] is not None else None,
        plan_id=str(row["plan_id"]) if row["plan_id"] is not None else None,
        event_type=str(row["event_type"]),
        created_at=row["created_at"],
        detail=_merged_event_detail(row),
    )


def _merged_event_detail(row: asyncpg.Record) -> dict[str, Any]:
    merged = _decode_json_mapping(row["detail"])
    merged.update(_decode_json_mapping(row["payload"]))
    return merged


def _decode_json_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return dict(value)


def _human_review_payload(state: HumanReviewState | None) -> dict[str, Any]:
    state = state or HumanReviewState()
    return {
        "required": state.required,
        "decision": state.decision,
        "stage_id": state.stage_id or None,
        "reason": state.reason or None,
        "reviewer": state.reviewer,
    }


__all__ = [
    "CursorDecodeError",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "decode_cursor",
    "encode_cursor",
    "list_tasks",
]
