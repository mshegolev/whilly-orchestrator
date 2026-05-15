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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
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
    OperatorTable,
    fetch_operator_snapshot,
    operator_surface_hotkey_help,
    operator_surface_items,
    operator_table_columns,
    operator_wui_route_prefixes,
)

UTC = timezone.utc

logger = logging.getLogger(__name__)


TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

DASHBOARD_TEMPLATE: Final[str] = "index.html.j2"
WORKERS_FRAGMENT_TEMPLATE: Final[str] = "_workers_table.html"
TASKS_FRAGMENT_TEMPLATE: Final[str] = "_tasks_table.html"
LOGS_FRAGMENT_TEMPLATE: Final[str] = "_logs.html"

TASKS_LIMIT: Final[int] = 200
WORKERS_LIMIT: Final[int] = 200
LOG_TAIL_DEFAULT: Final[int] = 200
LOG_TOKENS_BUDGET_DEFAULT: Final[int] = 40_000


@dataclass
class LogLine:
    ts: str
    level: str
    msg: str


class FileLogStore:
    """Minimal tail-N reader over ``whilly_logs/{task_id}.jsonl``.

    Each line is expected to be a JSON object with at least ``msg``;
    ``ts`` and ``level`` are looked up but tolerated when missing so
    legacy log files still render. ``usage()`` is a stub — replace with
    a real cost-tracking lookup when the orchestrator persists it.
    """

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def tail(self, task_id: str, n: int = LOG_TAIL_DEFAULT) -> list[LogLine]:
        path = self.log_dir / f"{task_id}.jsonl"
        if not path.exists():
            return []
        with path.open() as handle:
            recent = deque(handle, maxlen=n)
        lines: list[LogLine] = []
        for raw in recent:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                lines.append(LogLine(ts="--:--:--", level="INFO", msg=raw.rstrip()))
                continue
            lines.append(
                LogLine(
                    ts=str(obj.get("ts", "--:--:--")),
                    level=str(obj.get("level", "INFO")),
                    msg=str(obj.get("msg", "")),
                )
            )
        return lines

    def usage(self, task_id: str) -> tuple[int, int, float]:
        # TODO: read from real cost-tracking store once available.
        return (0, LOG_TOKENS_BUDGET_DEFAULT, 0.0)


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
            for key in (
                "acceptance_criteria",
                "test_steps",
                "key_files",
                "dependencies",
                "payload",
                "detail",
            ):
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
    if candidate in ("workers", "tasks", "logs"):
        return candidate
    return None


def dashboard_logs(
    request: Request,
    task_id: str | None,
    tasks: tuple[Any, ...],
) -> HTMLResponse:
    """Render the Logs surface fragment.

    Reads :class:`FileLogStore` (or any compatible object) from
    ``request.app.state.log_store``. ``tasks`` is the already-fetched
    snapshot slice used to populate the dropdown so the caller can
    share its single round-trip across surfaces.
    """
    templates = get_templates()
    log_store: FileLogStore | None = getattr(request.app.state, "log_store", None)
    if task_id is None and tasks:
        task_id = getattr(tasks[0], "task_id", None)

    log_lines: list[LogLine] = []
    tokens_used = 0
    tokens_budget = LOG_TOKENS_BUDGET_DEFAULT
    cost_usd = 0.0
    selected_status: str | None = None
    if task_id and log_store is not None:
        log_lines = list(log_store.tail(task_id, n=LOG_TAIL_DEFAULT))
        tokens_used, tokens_budget, cost_usd = log_store.usage(task_id)
        for candidate in tasks:
            if getattr(candidate, "task_id", None) == task_id:
                selected_status = getattr(candidate, "status", None)
                break

    context: dict[str, Any] = {
        "request": request,
        "task_id": task_id,
        "tasks": tasks,
        "log_lines": log_lines,
        "tokens_used": tokens_used,
        "tokens_budget": tokens_budget,
        "cost_usd": cost_usd,
        "status": selected_status,
    }
    response = templates.TemplateResponse(
        request,
        LOGS_FRAGMENT_TEMPLATE,
        context,
        status_code=status.HTTP_200_OK,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def render_dashboard(
    *,
    request: Request,
    pool: asyncpg.Pool,
    fragment: str | None = None,
    events_token: str | None = None,
    task_id: str | None = None,
    auth_email: str | None = None,
    plan_id_for_share_link: str | None = None,
) -> HTMLResponse:
    """Render the dashboard (full page or one of its two partials).

    Returns 200 with HTML on success and on DB failure (a friendly
    error banner replaces the live tables); never raises 500. The
    fragment partials surface the same banner when DB is down so the
    polling fallback shows the issue without flashing the page empty.

    ``auth_email`` and ``plan_id_for_share_link`` are PRD-wui-multi-plan
    v2 Block 5 (Epic D2) plumbing. When ``auth_email`` is truthy the
    template renders the header nav + plans-table block. When
    ``plan_id_for_share_link`` is truthy and ``auth_email`` is falsy the
    template surfaces the "shared plan" banner offering a sign-in
    affordance — see ``index.html.j2``.
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

    surface_items = operator_surface_items()
    surface_order = [surface.value for surface, _label in surface_items]
    context: dict[str, Any] = {
        "request": request,
        "snapshot": snapshot,
        "workers": snapshot.workers,
        "tasks": snapshot.tasks,
        "events": snapshot.events,
        "review_gaps": snapshot.review_gaps,
        "control_state": snapshot.control_state,
        "summary": snapshot.summary,
        "surfaces": [(surface.value, label) for surface, label in surface_items],
        "surface_order": surface_order,
        "surface_order_json": json.dumps(surface_order),
        "surface_switch_hotkey_label": operator_surface_hotkey_help(),
        "wui_route_prefixes": operator_wui_route_prefixes(),
        "table_columns": {
            "tasks": operator_table_columns(OperatorTable.TASKS, "wui"),
            "workers": operator_table_columns(OperatorTable.WORKERS, "wui"),
            "review_gaps": operator_table_columns(OperatorTable.REVIEW_GAPS, "wui"),
            "events": operator_table_columns(OperatorTable.EVENTS, "wui"),
        },
        "error": error,
        "version": WHILLY_VERSION,
        "rendered_at_iso": _format_iso(snapshot.rendered_at),
        "rendered_at_human": _format_human(snapshot.rendered_at),
        "events_token": events_token,
        "format_iso": _format_iso,
        "format_human": _format_human,
        "auth_email": auth_email,
        "plan_id_for_share_link": plan_id_for_share_link,
    }

    if fragment_name == "logs":
        return dashboard_logs(request, task_id, snapshot.tasks)
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
    "FileLogStore",
    "LOGS_FRAGMENT_TEMPLATE",
    "LOG_TAIL_DEFAULT",
    "LOG_TOKENS_BUDGET_DEFAULT",
    "LogLine",
    "TASKS_FRAGMENT_TEMPLATE",
    "TASKS_LIMIT",
    "TEMPLATES_DIR",
    "WORKERS_FRAGMENT_TEMPLATE",
    "WORKERS_LIMIT",
    "dashboard_logs",
    "get_templates",
    "render_dashboard",
]
