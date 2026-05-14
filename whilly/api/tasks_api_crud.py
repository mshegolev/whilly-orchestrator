"""FastAPI router for ``PATCH`` / ``DELETE /api/v1/tasks/{task_id}``.

PRD-wui-multi-plan v2 Block 8 — Epic C (task edit + hard delete).

Companion to :mod:`whilly.api.tasks_api` (read side) and
:mod:`whilly.api.plans_api_crud` (plans CRUD). Two endpoints, both
session-only (SC-5.1):

* ``PATCH /api/v1/tasks/{task_id}?plan_id=X`` — partial update.
  Accepts the editable subset of :class:`TaskCreateRequest`
  (``description``, ``priority``, ``key_files``, ``acceptance_criteria``,
  ``test_steps``, ``dependencies``).
* ``DELETE /api/v1/tasks/{task_id}?plan_id=X`` — hard delete with a
  ``task.deleted`` audit event carrying the full pre-deletion row JSON.

Concurrency
-----------
Optimistic via ``If-Match: W/"v<version>"`` (a *weak* ETag carrying the
``tasks.version`` int — the worker's existing optimistic-locking column,
reused as the CRUD ETag source). The shape is intentionally different
from :mod:`whilly.api.plans_api`'s sha256-based strong ETag: ``tasks``
already has a monotonic version column, so we publish it directly
instead of paying a hash on every read.

* Stale ``If-Match`` → 412 Precondition Failed + current ETag in the
  response header (C2).
* Missing ``If-Match`` → 428 Precondition Required.

Worker safety
-------------
PRD C5: while ``claimed_by IS NOT NULL`` *or* status ``IN_PROGRESS`` the
endpoints return **409 Conflict** with ``{"error":"task_claimed",
"detail":"task is currently in worker <id>; release it first",
"worker_id":"..."}``. Note that 409 — not 412 — is the deliberate code
here: 412 is "your view of the version is stale", 409 is "the row is
held by a third party and you must coordinate before editing". The UI
surfaces a Force-release affordance (two-step confirm) for this branch.

Plan archive
------------
If the parent plan has ``archived_at IS NOT NULL`` both endpoints return
**410 Gone** (parity with ``POST /api/v1/tasks`` in
:mod:`whilly.adapters.transport.server`). Hard-delete vs soft-archive is
the PRD change-log decision: tasks have no ARCHIVED status — the audit
trail lives in ``whilly_events.jsonl`` exclusively.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Final

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from whilly.api.auth_routes import (
    DEFAULT_EVENT_LOG_PATH,
    EVENT_LOG_PATH_ENV,
    authenticate_session_request,
)

logger = logging.getLogger(__name__)

#: Allowed keys in the PATCH body. Anything outside this set is silently
#: ignored — the schema accepts a *subset* of TaskCreateRequest and we do
#: not want to leak validation surface for fields the worker controls
#: (``status``, ``claimed_by``, etc.).
_EDITABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "description",
        "priority",
        "key_files",
        "acceptance_criteria",
        "test_steps",
        "dependencies",
    }
)

_VALID_PRIORITIES: Final[frozenset[str]] = frozenset({"critical", "high", "medium", "low"})


def _format_etag(version: int) -> str:
    """Build the weak ETag header value for a given task version.

    Format: ``W/"v<version>"``. We use a *weak* ETag because the body
    we hash is the result of multiple JSONB encodes that may not be
    byte-identical across asyncpg releases — semantic equivalence (same
    version) is what callers care about for round-tripping PATCH/DELETE.
    """
    return f'W/"v{int(version)}"'


def _parse_if_match(header_value: str | None) -> int | None:
    """Extract the integer version from ``If-Match: W/"v17"``.

    Returns ``None`` when the header is missing or malformed (route layer
    decides whether that is 428 or 412). Tolerant of strong-form quotes
    (``"v17"``) — operators copy-pasting from curl sometimes drop the
    ``W/`` prefix, and we should not punish them.
    """
    if not header_value:
        return None
    raw = header_value.strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    raw = raw.strip('"')
    if not raw.startswith("v"):
        return None
    try:
        return int(raw[1:])
    except (TypeError, ValueError):
        return None


def _coerce_string_list(value: Any, *, field: str) -> list[str]:
    """Validate that ``value`` is a list of strings — 400 otherwise."""
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a JSON array of strings",
        )
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field}[*] must be strings",
            )
        out.append(entry)
    return out


def build_tasks_crud_router(pool: asyncpg.Pool, secret: bytes) -> APIRouter:
    """Construct the session-only tasks CRUD router (Epic C).

    Mirrors :func:`whilly.api.plans_api.build_plans_router`'s factory
    shape so :func:`whilly.adapters.transport.server.create_app` can
    register all session-only routers with the same dependency wiring
    (Architect F2 — single key for the whole auth surface).
    """

    router = APIRouter()

    @router.patch("/api/v1/tasks/{task_id}")
    async def patch_task_endpoint(
        request: Request,
        task_id: str,
        plan_id: str = Query(..., min_length=1, max_length=256),
    ) -> Response:
        """Partial-update a task (Epic C1 / C2).

        Editable only when status ∈ {PENDING, DONE, FAILED, SKIPPED}.
        Returns 409 ``task_claimed`` when ``claimed_by IS NOT NULL`` or
        status ``IN_PROGRESS``; 412 when ``If-Match`` is stale.
        """
        principal = await authenticate_session_request(request, pool=pool, secret=secret)

        if_match_raw = request.headers.get("if-match")
        if not if_match_raw:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail='If-Match header required for PATCH /api/v1/tasks/{task_id} (form: W/"v<version>")',
            )
        expected_version = _parse_if_match(if_match_raw)
        if expected_version is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='malformed If-Match header; expected W/"v<int>"',
            )

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid JSON body: {exc}",
            ) from None
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="request body must be a JSON object",
            )

        fields_to_patch: dict[str, Any] = {}
        for key in _EDITABLE_FIELDS:
            if key not in body:
                continue
            raw_val = body[key]
            if key == "description":
                if raw_val is None:
                    fields_to_patch[key] = ""
                elif isinstance(raw_val, str):
                    fields_to_patch[key] = raw_val
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="description must be a string",
                    )
            elif key == "priority":
                if not isinstance(raw_val, str) or raw_val not in _VALID_PRIORITIES:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"priority must be one of {sorted(_VALID_PRIORITIES)}",
                    )
                fields_to_patch[key] = raw_val
            else:
                fields_to_patch[key] = _coerce_string_list(raw_val, field=key)

        from whilly.adapters.db import TaskRepository

        repo = TaskRepository(pool)
        result_status, payload, diff = await repo.patch_task(
            task_id,
            plan_id,
            fields_to_patch=fields_to_patch,
            expected_version=expected_version,
        )

        if result_status == "not_found":
            return JSONResponse(
                {"error": "not_found", "detail": f"task {task_id!r} not found in plan {plan_id!r}"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if result_status == "plan_archived":
            return JSONResponse(
                {"error": "plan_archived", "detail": f"plan {plan_id!r} is archived; tasks cannot be edited"},
                status_code=status.HTTP_410_GONE,
            )
        if result_status in ("claimed", "in_progress"):
            worker_id = payload.get("claimed_by") if payload else None
            return JSONResponse(
                {
                    "error": "task_claimed",
                    "detail": (
                        f"task is currently in worker {worker_id}; release it first"
                        if worker_id
                        else "task is in transient IN_PROGRESS state; wait or release first"
                    ),
                    "worker_id": worker_id,
                },
                status_code=status.HTTP_409_CONFLICT,
                headers={"ETag": _format_etag(payload["version"])} if payload else {},
            )
        if result_status == "version_conflict":
            current_etag = _format_etag(payload["version"]) if payload else 'W/"v0"'
            return JSONResponse(
                {
                    "error": "precondition_failed",
                    "detail": "If-Match header does not match current version",
                    "current_etag": current_etag,
                },
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                headers={"ETag": current_etag},
            )

        assert result_status == "updated", f"unexpected patch_task status: {result_status!r}"
        assert payload is not None

        if diff:
            _append_task_event(
                {
                    "event_type": "task.edited",
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "diff": diff,
                    "new_version": payload["version"],
                    "actor": principal.get("email"),
                }
            )

        return JSONResponse(
            payload,
            headers={"ETag": _format_etag(payload["version"]), "Cache-Control": "no-store"},
        )

    @router.delete("/api/v1/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_task_endpoint(
        request: Request,
        task_id: str,
        plan_id: str = Query(..., min_length=1, max_length=256),
    ) -> Response:
        """Hard-delete a task (Epic C3).

        Writes the full pre-deletion row JSON to ``task.deleted`` so the
        audit trail survives the row's removal (PRD change-log: tasks
        use hard delete + audit, no ARCHIVED status).
        """
        principal = await authenticate_session_request(request, pool=pool, secret=secret)

        if_match_raw = request.headers.get("if-match")
        if not if_match_raw:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail='If-Match header required for DELETE /api/v1/tasks/{task_id} (form: W/"v<version>")',
            )
        expected_version = _parse_if_match(if_match_raw)
        if expected_version is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='malformed If-Match header; expected W/"v<int>"',
            )

        from whilly.adapters.db import TaskRepository

        repo = TaskRepository(pool)
        result_status, payload = await repo.delete_task(
            task_id,
            plan_id,
            expected_version=expected_version,
        )

        if result_status == "not_found":
            return JSONResponse(
                {"error": "not_found", "detail": f"task {task_id!r} not found in plan {plan_id!r}"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if result_status == "plan_archived":
            return JSONResponse(
                {"error": "plan_archived", "detail": f"plan {plan_id!r} is archived; tasks cannot be deleted"},
                status_code=status.HTTP_410_GONE,
            )
        if result_status in ("claimed", "in_progress"):
            worker_id = payload.get("claimed_by") if payload else None
            return JSONResponse(
                {
                    "error": "task_claimed",
                    "detail": (
                        f"task is currently in worker {worker_id}; release it first"
                        if worker_id
                        else "task is in transient IN_PROGRESS state; wait or release first"
                    ),
                    "worker_id": worker_id,
                },
                status_code=status.HTTP_409_CONFLICT,
                headers={"ETag": _format_etag(payload["version"])} if payload else {},
            )
        if result_status == "version_conflict":
            current_etag = _format_etag(payload["version"]) if payload else 'W/"v0"'
            return JSONResponse(
                {
                    "error": "precondition_failed",
                    "detail": "If-Match header does not match current version",
                    "current_etag": current_etag,
                },
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                headers={"ETag": current_etag},
            )

        assert result_status == "deleted", f"unexpected delete_task status: {result_status!r}"
        assert payload is not None
        _append_task_event(
            {
                "event_type": "task.deleted",
                "plan_id": plan_id,
                "task_id": task_id,
                "deleted_row": payload,
                "actor": principal.get("email"),
            }
        )

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


# ─── Event log ───────────────────────────────────────────────────────────────


def _append_task_event(event: dict[str, Any]) -> None:
    """Append a single JSON line to ``whilly_events.jsonl``.

    Mirrors :func:`whilly.api.plans_api._append_plan_event`. Best-effort:
    filesystem errors log a warning but do not raise — operator-facing
    CRUD must not fail because the event log is read-only or full.
    """
    event = dict(event)
    event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    log_path = Path(os.environ.get(EVENT_LOG_PATH_ENV, DEFAULT_EVENT_LOG_PATH))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    except OSError as exc:
        logger.warning("tasks_crud: event-log append failed (%s): %s", log_path, exc)


__all__ = ["build_tasks_crud_router"]
