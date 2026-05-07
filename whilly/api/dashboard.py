"""HTMX dashboard surface for the M3 ``GET /`` endpoint.

Renders ``whilly/api/templates/index.html.j2`` (full page) and the two
partials (``_workers_table.html`` / ``_tasks_table.html``) used by the
``?fragment=workers|tasks`` polling fallback. Jinja2 autoescape stays
on (the default for ``.html`` files in starlette's
:class:`Jinja2Templates`) so any user-supplied string in the row
projections (``hostname``, ``owner_email``, ``claimed_by``, ``id``)
cannot break out of HTML.

Live updates flow over the existing ``GET /events/stream`` SSE channel
(htmx-ext-sse@2.2.4): the body element carries ``hx-ext="sse"`` plus
a short-lived dashboard token on ``sse-connect``. The two tables fire
``hx-get`` against ``/?fragment=...`` on the relevant SSE event names.
When the EventSource is unavailable (proxy strips, browser blocks),
``hx-trigger="every 5s"`` keeps the tables fresh.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import asyncpg
from fastapi import Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from whilly import __version__ as WHILLY_VERSION
from whilly.operator_views import (
    ComplianceSummary,
    OperatorSnapshot,
    OperatorSurface,
    fetch_operator_snapshot,
)

logger = logging.getLogger(__name__)


TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

DASHBOARD_TEMPLATE: Final[str] = "index.html.j2"
WORKERS_FRAGMENT_TEMPLATE: Final[str] = "_workers_table.html"
TASKS_FRAGMENT_TEMPLATE: Final[str] = "_tasks_table.html"

TASKS_LIMIT: Final[int] = 200
WORKERS_LIMIT: Final[int] = 200

_SURFACE_LABELS: Final[dict[OperatorSurface, str]] = {
    OperatorSurface.OVERVIEW: "Overview",
    OperatorSurface.COMPLIANCE: "Compliance",
    OperatorSurface.PLANS_TASKS: "Plans/Tasks",
    OperatorSurface.WORKERS: "Workers",
    OperatorSurface.EVENTS: "Events",
}


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


def _decode_jsonb_value(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class _SnapshotConnection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(query, *args)
        decoded: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("acceptance_criteria", "test_steps", "detail"):
                if key in item:
                    item[key] = _decode_jsonb_value(item[key])
            decoded.append(item)
        return decoded


class _SnapshotAcquire:
    def __init__(self, acquire_context: Any) -> None:
        self._acquire_context = acquire_context

    async def __aenter__(self) -> _SnapshotConnection:
        conn = await self._acquire_context.__aenter__()
        return _SnapshotConnection(conn)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self._acquire_context.__aexit__(exc_type, exc, tb)


class _SnapshotPool:
    """Pool adapter that decodes JSONB fields before building operator rows."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    def acquire(self) -> _SnapshotAcquire:
        return _SnapshotAcquire(self._pool.acquire())


def _empty_snapshot(rendered_at: datetime) -> OperatorSnapshot:
    return OperatorSnapshot(
        rendered_at=rendered_at,
        summary=ComplianceSummary(
            total_tasks=0,
            tasks_by_status={},
            workers_online=0,
            workers_total=0,
            failed_tasks=0,
            open_review_gaps=0,
        ),
        tasks=(),
        workers=(),
        events=(),
        review_gaps=(),
    )


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
    events_token: str | None = None,
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
    rendered_at = datetime.now(tz=UTC)
    snapshot = _empty_snapshot(rendered_at)
    try:
        snapshot = await fetch_operator_snapshot(_SnapshotPool(pool), rendered_at=rendered_at)
    except Exception as exc:
        logger.warning("dashboard fetch failed: %s", exc)
        error = f"{type(exc).__name__}: {exc}"

    context: dict[str, Any] = {
        "request": request,
        "snapshot": snapshot,
        "workers": snapshot.workers,
        "tasks": snapshot.tasks,
        "events": snapshot.events,
        "review_gaps": snapshot.review_gaps,
        "summary": snapshot.summary,
        "surfaces": [(surface.value, _SURFACE_LABELS[surface]) for surface in OperatorSurface],
        "error": error,
        "version": WHILLY_VERSION,
        "rendered_at_iso": _format_iso(snapshot.rendered_at),
        "rendered_at_human": _format_human(snapshot.rendered_at),
        "events_token": events_token,
        "format_iso": _format_iso,
        "format_human": _format_human,
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
