"""HTMX dashboard surface for the M3 ``GET /`` endpoint.

Renders ``whilly/api/templates/dashboard.html`` (full page) and the two
partials (``_workers_table.html`` / ``_tasks_table.html``) used by the
``?fragment=workers|tasks`` polling fallback. Jinja2 autoescape stays
on (the default for ``.html`` files in starlette's
:class:`Jinja2Templates`) so any user-supplied string in the row
projections (``hostname``, ``owner_email``, ``claimed_by``, ``id``)
cannot break out of HTML.

Live updates flow over the existing ``GET /events/stream`` SSE channel
(htmx-ext-sse@2.2.4): the body element carries
``hx-ext="sse" sse-connect="/events/stream"`` and the two tables fire
``hx-get`` against ``/?fragment=...`` on the relevant SSE event names
(``task.claim``, ``task.complete``, ``worker.heartbeat`` etc.). When
the EventSource is unavailable (proxy strips, browser blocks),
``hx-trigger="every 5s"`` keeps the tables fresh.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import asyncpg
from fastapi import Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from whilly import __version__ as WHILLY_VERSION

logger = logging.getLogger(__name__)


TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

DASHBOARD_TEMPLATE: Final[str] = "dashboard.html"
WORKERS_FRAGMENT_TEMPLATE: Final[str] = "_workers_table.html"
TASKS_FRAGMENT_TEMPLATE: Final[str] = "_tasks_table.html"

TASKS_LIMIT: Final[int] = 200
WORKERS_LIMIT: Final[int] = 200

_TASKS_SQL: Final[str] = """
SELECT id, plan_id, status, priority, claimed_by, claimed_at, updated_at
FROM tasks
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
LIMIT $1
"""

_WORKERS_SQL: Final[str] = """
SELECT worker_id, hostname, owner_email, status, last_heartbeat, registered_at
FROM workers
ORDER BY status ASC, last_heartbeat DESC
LIMIT $1
"""

_SUMMARY_SQL: Final[str] = """
SELECT
    (SELECT COUNT(*) FROM workers WHERE status = 'online') AS workers_online,
    (SELECT COUNT(*) FROM tasks WHERE status = 'PENDING') AS tasks_pending,
    (SELECT COUNT(*) FROM tasks WHERE status IN ('CLAIMED', 'IN_PROGRESS')) AS tasks_in_progress,
    (SELECT COUNT(*) FROM tasks WHERE status = 'DONE') AS tasks_done,
    (SELECT COUNT(*) FROM tasks WHERE status = 'FAILED') AS tasks_failed
"""


_templates: Jinja2Templates | None = None


def get_templates() -> Jinja2Templates:
    """Return the module-level :class:`Jinja2Templates` (autoescape on).

    Lazy-init lets module import succeed in environments where the
    optional ``jinja2`` dep is not installed (the worker import path,
    enforced by ``.importlinter``); the dashboard endpoint is only
    reachable from the control-plane app, which always pulls
    ``[server]`` extras.
    """
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    return _templates


def _format_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _format_human(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _row_to_worker(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    last_heartbeat = row["last_heartbeat"]
    return {
        "worker_id": row["worker_id"],
        "hostname": row["hostname"],
        "owner_email": row["owner_email"],
        "status": row["status"],
        "last_heartbeat_iso": _format_iso(last_heartbeat),
        "last_heartbeat_human": _format_human(last_heartbeat),
    }


def _row_to_task(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    updated_at = row["updated_at"]
    return {
        "id": row["id"],
        "plan_id": row["plan_id"],
        "status": row["status"],
        "priority": row["priority"],
        "claimed_by": row["claimed_by"],
        "updated_at_iso": _format_iso(updated_at),
        "updated_at_human": _format_human(updated_at),
    }


_EMPTY_SUMMARY: Final[dict[str, int]] = {
    "workers_online": 0,
    "tasks_pending": 0,
    "tasks_in_progress": 0,
    "tasks_done": 0,
    "tasks_failed": 0,
}


async def _fetch_dashboard_data(
    pool: asyncpg.Pool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    async with pool.acquire() as conn:
        worker_rows = await conn.fetch(_WORKERS_SQL, WORKERS_LIMIT)
        task_rows = await conn.fetch(_TASKS_SQL, TASKS_LIMIT)
        summary_row = await conn.fetchrow(_SUMMARY_SQL)
    workers = [_row_to_worker(r) for r in worker_rows]
    tasks = [_row_to_task(r) for r in task_rows]
    if summary_row is None:
        summary = dict(_EMPTY_SUMMARY)
    else:
        summary = {
            "workers_online": int(summary_row["workers_online"] or 0),
            "tasks_pending": int(summary_row["tasks_pending"] or 0),
            "tasks_in_progress": int(summary_row["tasks_in_progress"] or 0),
            "tasks_done": int(summary_row["tasks_done"] or 0),
            "tasks_failed": int(summary_row["tasks_failed"] or 0),
        }
    return workers, tasks, summary


def _normalise_fragment(raw: str | None) -> str | None:
    if raw is None:
        return None
    candidate = raw.strip().lower()
    if candidate in ("workers", "tasks"):
        return candidate
    return None


async def render_dashboard(
    *,
    request: Request,
    pool: asyncpg.Pool,
    fragment: str | None = None,
) -> HTMLResponse:
    """Render the dashboard (full page or one of its two partials).

    Returns 200 with HTML on success and on DB failure (a friendly
    error banner replaces the live tables); never raises 500. The
    fragment partials surface the same banner when DB is down so the
    polling fallback shows the issue without flashing the page empty.
    """
    fragment_name = _normalise_fragment(fragment)
    templates = get_templates()
    error: str | None = None
    workers: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    summary: dict[str, int] = dict(_EMPTY_SUMMARY)
    try:
        workers, tasks, summary = await _fetch_dashboard_data(pool)
    except Exception as exc:
        logger.warning("dashboard fetch failed: %s", exc)
        error = f"{type(exc).__name__}: {exc}"

    rendered_at = datetime.now(tz=UTC)
    context: dict[str, Any] = {
        "request": request,
        "workers": workers,
        "tasks": tasks,
        "summary": summary,
        "error": error,
        "version": WHILLY_VERSION,
        "rendered_at_iso": rendered_at.isoformat(),
        "rendered_at_human": rendered_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    if fragment_name == "workers":
        template_name = WORKERS_FRAGMENT_TEMPLATE
    elif fragment_name == "tasks":
        template_name = TASKS_FRAGMENT_TEMPLATE
    else:
        template_name = DASHBOARD_TEMPLATE

    response = templates.TemplateResponse(
        request,
        template_name,
        context,
        status_code=status.HTTP_200_OK,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = [
    "DASHBOARD_TEMPLATE",
    "TASKS_FRAGMENT_TEMPLATE",
    "TASKS_LIMIT",
    "TEMPLATES_DIR",
    "WORKERS_FRAGMENT_TEMPLATE",
    "WORKERS_LIMIT",
    "get_templates",
    "render_dashboard",
]
