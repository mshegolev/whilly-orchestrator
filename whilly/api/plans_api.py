"""FastAPI router for ``GET /api/v1/plans`` (PRD-wui-multi-plan v2, Block 4).

Epic B1 (plan list) + B2 (status counts) + B7 (archive filter, list side
only). The CRUD verbs (POST / PATCH archive / unarchive) land in Block 7
via :mod:`whilly.api.plans_api_crud` — this module is read-only.

Auth: session-only (SC-5.1). The dispatcher route in Block 5 passes the
authenticated email into the index template; this router enforces the
same gate at the JSON layer via
:func:`whilly.api.auth_routes.authenticate_session_request`. Worker
bearer / dashboard JWT tokens are **not** accepted on
``/api/v1/plans`` — those auth modes never reach a human session and the
new CRUD surface is humans-only by design.

Response shape (kept narrow on purpose so the HTMX-hydrated table in
``index.html.j2`` can render without a second round-trip):

.. code-block:: json

   {
     "plans": [
       {
         "id": "demo",
         "name": "Demo plan",
         "prd_file": "PRD-demo.md",
         "budget_usd": "25.00",
         "archived_at": null,
         "last_event_at": "2026-05-15T08:42:11.123456+00:00",
         "task_counts": {
           "pending": 3, "claimed": 0, "in_progress": 1,
           "done": 7, "failed": 0, "skipped": 1
         }
       }
     ],
     "next_cursor": null
   }

Sort order: ``last_event_at DESC NULLS LAST, id ASC``. Mirrors the
partial index added in migration ``019a_plans_archived_at``.

Cursor format: base64url(``{last_event_at_iso}|{id}``) — the migration
PRD §6.3 commits to a forward-only opaque cursor; we intentionally do
not promise stability across server upgrades, only across a single
client's pagination walk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
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

#: Hard ceiling on ``?limit=`` so a hostile or buggy client cannot ask
#: for ten thousand plans in a single round-trip. PRD §6.3 names 500 as
#: the upper bound; we expose 1..500 via the FastAPI ``Query`` validator.
_MAX_LIMIT: Final[int] = 500
_DEFAULT_LIMIT: Final[int] = 100

#: PRD-wui-multi-plan v2 Epic B3 — plan_id validation. Lowercase ASCII
#: letters, digits, dash and underscore; must start with alnum; ≤ 256
#: chars. Matches the slug shape worker tokens and event_log filters
#: already assume across the codebase.
_PLAN_ID_REGEX: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,254}$")
_PLAN_ID_MAX_LEN: Final[int] = 256
_PLAN_NAME_MAX_LEN: Final[int] = 1024


def build_plans_router(*, pool: asyncpg.Pool, secret: bytes) -> APIRouter:
    """Construct the read-only plans router bound to a pool + session secret.

    Factory wiring mirrors :func:`whilly.api.auth_routes.build_auth_router`
    so unit tests can inject a testcontainer pool and a per-test HMAC
    secret without touching module-level state.

    Parameters
    ----------
    pool:
        Open :class:`asyncpg.Pool`. The router never owns the lifecycle;
        :func:`whilly.adapters.transport.server.create_app` (or a test
        fixture) closes the pool on shutdown.
    secret:
        HMAC secret bytes used to verify the session cookie. **Must** be
        the same bytes passed to :func:`build_auth_router` — the
        dispatcher in Block 5 enforces this at app composition time
        (Architect F2 — single key for the whole auth surface).
    """

    router = APIRouter()

    @router.get("/api/v1/plans")
    async def list_plans(
        request: Request,
        include_archived: bool = Query(
            False,
            description="When true, include plans where archived_at IS NOT NULL.",
        ),
        limit: int = Query(
            _DEFAULT_LIMIT,
            ge=1,
            le=_MAX_LIMIT,
            description=f"Page size (1..{_MAX_LIMIT}); default {_DEFAULT_LIMIT}.",
        ),
        cursor: str | None = Query(
            None,
            description="Opaque base64url cursor from a previous response's next_cursor.",
        ),
    ) -> dict[str, object]:
        # SC-5.1: session-only auth on every CRUD endpoint. Worker bearer
        # tokens and dashboard JWTs are deliberately NOT accepted here —
        # the human-facing CRUD surface is gated to authenticated
        # operators only (PRD §5.1).
        await authenticate_session_request(request, pool=pool, secret=secret)

        cursor_last_event_at, cursor_id = _decode_cursor(cursor)

        rows = await _fetch_plan_page(
            pool,
            include_archived=include_archived,
            limit=limit,
            cursor_last_event_at=cursor_last_event_at,
            cursor_id=cursor_id,
        )

        plans = [_row_to_plan_json(row) for row in rows]

        # Cursor is opaque on the wire. Encode the *last* row's sort key
        # tuple; the next page query reads "strictly after" that tuple
        # per the lexicographic ordering on (last_event_at DESC, id ASC).
        next_cursor: str | None = None
        if len(plans) == limit and plans:
            tail = plans[-1]
            next_cursor = _encode_cursor(tail["last_event_at"], str(tail["id"]))

        return {"plans": plans, "next_cursor": next_cursor}

    @router.post("/api/v1/plans", status_code=status.HTTP_201_CREATED)
    async def create_plan(request: Request) -> Response:
        """Create an empty plan (Epic B3).

        Validates plan_id shape, INSERTs, on unique-violation returns
        409 with ``{"error":"plan_exists",...}``. Successful path emits
        ``plan.created`` to ``whilly_events.jsonl`` and returns the
        canonical PlanPayload + ETag header so the client can flip
        straight into a PATCH round-trip without an extra GET.
        """
        principal = await authenticate_session_request(request, pool=pool, secret=secret)

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

        plan_id = body.get("plan_id")
        name = body.get("name")
        prd_file = body.get("prd_file")
        budget_usd_raw = body.get("budget_usd")

        _validate_plan_id(plan_id)
        _validate_plan_name(name)
        if prd_file is not None and not isinstance(prd_file, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="prd_file must be a string or null",
            )
        budget = _coerce_budget(budget_usd_raw)

        try:
            payload_dict = await _insert_plan(
                pool,
                plan_id=plan_id,  # type: ignore[arg-type]
                name=name,  # type: ignore[arg-type]
                prd_file=prd_file,
                budget_usd=budget,
            )
        except asyncpg.UniqueViolationError:
            return JSONResponse(
                {
                    "error": "plan_exists",
                    "detail": f"plan_id {plan_id!r} already exists",
                },
                status_code=status.HTTP_409_CONFLICT,
            )

        _append_plan_event(
            {
                "event_type": "plan.created",
                "plan_id": plan_id,
                "name": name,
                "prd_file": prd_file,
                "budget_usd": str(budget) if budget is not None else None,
                "actor": principal.get("email"),
            }
        )

        wire_payload = _payload_dict_to_json(payload_dict)
        etag = _compute_plan_etag(payload_dict)
        return JSONResponse(
            wire_payload,
            status_code=status.HTTP_201_CREATED,
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )

    @router.patch("/api/v1/plans/{plan_id}")
    async def patch_plan_endpoint(request: Request, plan_id: str) -> Response:
        """Partial-update a plan (Epic B4 / B5).

        Requires ``If-Match`` header (428 if missing, 412 if stale).
        ``archived: true`` archives, ``archived: false`` restores —
        this PATCH is the only path that replaces v1's ``/restore``
        endpoint (PRD change-log line 17).
        """
        principal = await authenticate_session_request(request, pool=pool, secret=secret)

        if_match = request.headers.get("if-match")
        if not if_match:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="If-Match header required for PATCH /api/v1/plans/{plan_id}",
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

        # Parse + validate fields. Use sentinels so we can distinguish
        # "key absent" from "key present, value null".
        _UNSET = object()
        new_name: Any = _UNSET
        new_budget: Any = _UNSET
        new_archived: Any = _UNSET
        if "name" in body:
            _validate_plan_name(body["name"])
            new_name = body["name"]
        if "budget_usd" in body:
            new_budget = _coerce_budget(body["budget_usd"])
        if "archived" in body:
            arc = body["archived"]
            if not isinstance(arc, bool):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="archived must be a boolean",
                )
            new_archived = arc

        # Concurrency check: fetch current row, compare ETag.
        current = await _fetch_one_plan(pool, plan_id=plan_id, include_archived=True)
        if current is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"plan {plan_id!r} not found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        current_etag = _compute_plan_etag(current)
        if if_match.strip() != current_etag:
            return JSONResponse(
                {
                    "error": "precondition_failed",
                    "detail": "If-Match header does not match current ETag",
                    "current_etag": current_etag,
                },
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                headers={"ETag": current_etag},
            )

        updated = await _update_plan(
            pool,
            plan_id=plan_id,
            name=new_name,
            budget_usd=new_budget,
            archived=new_archived,
            sentinel=_UNSET,
        )
        if updated is None:
            # Row vanished between fetch + update (extremely rare —
            # plan.deleted is not yet a supported operation in v2 so
            # this only fires under a parallel migration / manual SQL).
            return JSONResponse(
                {"error": "not_found", "detail": f"plan {plan_id!r} not found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Build diff for the audit event — only fields that actually
        # changed (so an idempotent PATCH does not spam events).
        diff: dict[str, Any] = {}
        if new_name is not _UNSET and current["name"] != new_name:
            diff["name"] = {"from": current["name"], "to": new_name}
        if new_budget is not _UNSET:
            old_budget = current["budget_usd"]
            if (str(old_budget) if old_budget is not None else None) != (
                str(new_budget) if new_budget is not None else None
            ):
                diff["budget_usd"] = {
                    "from": str(old_budget) if old_budget is not None else None,
                    "to": str(new_budget) if new_budget is not None else None,
                }
        if new_archived is not _UNSET:
            old_archived = current["archived_at"] is not None
            if old_archived != bool(new_archived):
                diff["archived"] = {"from": old_archived, "to": bool(new_archived)}

        if diff:
            _append_plan_event(
                {
                    "event_type": "plan.edited",
                    "plan_id": plan_id,
                    "diff": diff,
                    "actor": principal.get("email"),
                }
            )

        new_etag = _compute_plan_etag(updated)
        wire_payload = _payload_dict_to_json(updated)
        return JSONResponse(
            wire_payload,
            headers={"ETag": new_etag, "Cache-Control": "no-store"},
        )

    return router


# ─── SQL ─────────────────────────────────────────────────────────────────────


# We compute ``last_event_at`` via a correlated subquery against
# ``events`` (PRD §6.3 reserves the column on ``plans`` but the
# migration explicitly does NOT populate it in v2 — recovery scripts and
# back-fills land in v3). The subquery is cheap because ``events`` has
# an ``(plan_id, emitted_at DESC)`` index from migration 005. We coerce
# NULL to ``-infinity`` for the ORDER BY so NULLS LAST works under
# DESC without a NULLS LAST clause — keeping the SQL portable.
#
# Task counts are pulled in a single LATERAL join so a plan with zero
# tasks still appears in the result set (LEFT JOIN of aggregate ↔ plan
# would coalesce to NULL, which we'd then have to map to 0 in Python;
# the LATERAL form gives us 0s in SQL).
#: Single-plan projection sharing the same column list as
#: :data:`_SELECT_PLANS_SQL`. Used by the POST/PATCH/GET-by-id paths so
#: every plan response shape is identical (Block 7 — Architect F2).
_SELECT_ONE_PLAN_SQL: str = """
SELECT
    p.id                   AS id,
    p.name                 AS name,
    p.prd_file             AS prd_file,
    p.budget_usd           AS budget_usd,
    p.archived_at          AS archived_at,
    (
        SELECT MAX(e.created_at)
        FROM events e
        WHERE e.plan_id = p.id
    )                      AS last_event_at,
    COALESCE(tc.pending,     0) AS pending_count,
    COALESCE(tc.claimed,     0) AS claimed_count,
    COALESCE(tc.in_progress, 0) AS in_progress_count,
    COALESCE(tc.done,        0) AS done_count,
    COALESCE(tc.failed,      0) AS failed_count,
    COALESCE(tc.skipped,     0) AS skipped_count
FROM plans p
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE status = 'PENDING')      AS pending,
        COUNT(*) FILTER (WHERE status = 'CLAIMED')      AS claimed,
        COUNT(*) FILTER (WHERE status = 'IN_PROGRESS')  AS in_progress,
        COUNT(*) FILTER (WHERE status = 'DONE')         AS done,
        COUNT(*) FILTER (WHERE status = 'FAILED')       AS failed,
        COUNT(*) FILTER (WHERE status = 'SKIPPED')      AS skipped
    FROM tasks
    WHERE plan_id = p.id
) tc ON TRUE
WHERE p.id = $1
  AND ($2::bool OR p.archived_at IS NULL)
LIMIT 1
"""


_SELECT_PLANS_SQL: str = """
SELECT
    p.id                   AS id,
    p.name                 AS name,
    p.prd_file             AS prd_file,
    p.budget_usd           AS budget_usd,
    p.archived_at          AS archived_at,
    (
        SELECT MAX(e.created_at)
        FROM events e
        WHERE e.plan_id = p.id
    )                      AS last_event_at,
    COALESCE(tc.pending,     0) AS pending_count,
    COALESCE(tc.claimed,     0) AS claimed_count,
    COALESCE(tc.in_progress, 0) AS in_progress_count,
    COALESCE(tc.done,        0) AS done_count,
    COALESCE(tc.failed,      0) AS failed_count,
    COALESCE(tc.skipped,     0) AS skipped_count
FROM plans p
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE status = 'PENDING')      AS pending,
        COUNT(*) FILTER (WHERE status = 'CLAIMED')      AS claimed,
        COUNT(*) FILTER (WHERE status = 'IN_PROGRESS')  AS in_progress,
        COUNT(*) FILTER (WHERE status = 'DONE')         AS done,
        COUNT(*) FILTER (WHERE status = 'FAILED')       AS failed,
        COUNT(*) FILTER (WHERE status = 'SKIPPED')      AS skipped
    FROM tasks
    WHERE plan_id = p.id
) tc ON TRUE
WHERE ($1::bool OR p.archived_at IS NULL)
  AND (
        $2::timestamptz IS NULL
     OR (
            (
                SELECT MAX(e.created_at) FROM events e WHERE e.plan_id = p.id
            ) IS NULL AND $2::timestamptz = '-infinity'::timestamptz AND p.id > $3
         OR (
                SELECT MAX(e.created_at) FROM events e WHERE e.plan_id = p.id
            ) < $2::timestamptz
         OR (
                SELECT MAX(e.created_at) FROM events e WHERE e.plan_id = p.id
            ) = $2::timestamptz AND p.id > $3
      )
  )
ORDER BY
    (SELECT MAX(e.created_at) FROM events e WHERE e.plan_id = p.id) DESC NULLS LAST,
    p.id ASC
LIMIT $4
"""


async def _fetch_plan_page(
    pool: asyncpg.Pool,
    *,
    include_archived: bool,
    limit: int,
    cursor_last_event_at: object | None,
    cursor_id: str | None,
) -> list[asyncpg.Record]:
    """Run the paginated plans query and return raw asyncpg records.

    Split out so unit tests can monkeypatch the row source without
    standing up a Postgres instance (Architect F9 — IO at the edge).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SELECT_PLANS_SQL,
            include_archived,
            cursor_last_event_at,
            cursor_id or "",
            limit,
        )
    return list(rows)


# ─── Serialisation ───────────────────────────────────────────────────────────


def _row_to_plan_json(row: asyncpg.Record) -> dict[str, object]:
    """Convert one ``plans`` row into the JSON-serialisable response shape.

    asyncpg returns :class:`decimal.Decimal` for ``numeric`` columns and
    :class:`datetime.datetime` for ``timestamptz``; both serialise
    cleanly through FastAPI's default ``jsonable_encoder``, but we
    canonicalise to ISO-8601 strings here so the wire format is stable
    regardless of which JSON encoder FastAPI chooses (the encoder swap
    in Block 7 must not change the shape callers see).
    """
    archived_at = row["archived_at"]
    last_event_at = row["last_event_at"]
    budget_usd = row["budget_usd"]
    return {
        "id": row["id"],
        "name": row["name"],
        "prd_file": row["prd_file"],
        "budget_usd": str(budget_usd) if budget_usd is not None else None,
        "archived_at": archived_at.isoformat() if archived_at is not None else None,
        "last_event_at": last_event_at.isoformat() if last_event_at is not None else None,
        "task_counts": {
            "pending": int(row["pending_count"]),
            "claimed": int(row["claimed_count"]),
            "in_progress": int(row["in_progress_count"]),
            "done": int(row["done_count"]),
            "failed": int(row["failed_count"]),
            "skipped": int(row["skipped_count"]),
        },
    }


def _payload_dict_to_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Serialise a repository-shaped PlanPayload dict to JSON-safe types.

    The repository returns raw ``datetime`` and ``Decimal`` values
    (Architect F9 — IO at the edge). The router is the place that
    canonicalises them to ISO-8601 / string-Decimal so the wire format
    matches :func:`_row_to_plan_json` exactly.
    """
    archived_at = payload.get("archived_at")
    last_event_at = payload.get("last_event_at")
    budget_usd = payload.get("budget_usd")
    return {
        "id": payload["id"],
        "name": payload["name"],
        "prd_file": payload.get("prd_file"),
        "budget_usd": str(budget_usd) if budget_usd is not None else None,
        "archived_at": archived_at.isoformat() if hasattr(archived_at, "isoformat") else archived_at,
        "last_event_at": last_event_at.isoformat() if hasattr(last_event_at, "isoformat") else last_event_at,
        "task_counts": dict(payload.get("task_counts") or {}),
    }


# ─── ETag + repository wrappers (Block 7) ────────────────────────────────────


def _compute_plan_etag(row: asyncpg.Record | dict[str, Any]) -> str:
    """Strong ETag from ``(id, name, budget_usd, archived_at, last_event_at)``.

    PRD §6.5: ``'"' + sha256(repr(tuple)).hexdigest()[:16] + '"'``. We
    accept both an asyncpg Record (route GET reads directly from the
    pool) and a plain dict (repository methods return dicts) so the
    POST/PATCH paths can share a single ETag computation. ``str(Decimal)``
    canonicalises the budget so two equal budgets that differ in driver
    precision still hash the same.
    """
    raw = (
        row["id"],
        row["name"],
        str(row["budget_usd"]) if row["budget_usd"] is not None else None,
        row["archived_at"].isoformat() if row["archived_at"] is not None else None,
        row["last_event_at"].isoformat() if row["last_event_at"] is not None else None,
    )
    digest = hashlib.sha256(repr(raw).encode("utf-8")).hexdigest()[:16]
    return f'"{digest}"'


async def _fetch_one_plan(
    pool: asyncpg.Pool,
    *,
    plan_id: str,
    include_archived: bool,
) -> asyncpg.Record | None:
    """Return one ``plans`` row using the canonical projection.

    Used by GET-by-id + the PATCH pre-flight ETag fetch. Mirrors
    :data:`_SELECT_ONE_PLAN_SQL` arity.
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(_SELECT_ONE_PLAN_SQL, plan_id, bool(include_archived))


async def _insert_plan(
    pool: asyncpg.Pool,
    *,
    plan_id: str,
    name: str,
    prd_file: str | None,
    budget_usd: Decimal | None,
) -> dict[str, Any]:
    """Thin wrapper around :meth:`TaskRepository.create_plan`.

    Importing the repository at module top would create a runtime
    import cycle (the repository imports ``_SELECT_PLANS_SQL`` from
    here). Local import per call site keeps the cycle broken.
    """
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(pool)
    return await repo.create_plan(
        plan_id=plan_id,
        name=name,
        prd_file=prd_file,
        budget_usd=budget_usd,
    )


async def _update_plan(
    pool: asyncpg.Pool,
    *,
    plan_id: str,
    name: Any,
    budget_usd: Any,
    archived: Any,
    sentinel: object,
) -> dict[str, Any] | None:
    """Thin wrapper around :meth:`TaskRepository.patch_plan` translating
    sentinel-typed args to the repository's own ``_UNSET`` API."""
    from whilly.adapters.db import TaskRepository
    from whilly.adapters.db.repository import _UNSET as REPO_UNSET

    repo = TaskRepository(pool)
    kwargs: dict[str, Any] = {}
    if name is not sentinel:
        kwargs["name"] = name
    if budget_usd is not sentinel:
        kwargs["budget_usd"] = budget_usd
    if archived is not sentinel:
        kwargs["archived"] = archived
    # Fill in REPO_UNSET for omitted keys so the repository method
    # receives its own sentinel and only updates the present columns.
    for key in ("name", "budget_usd", "archived"):
        kwargs.setdefault(key, REPO_UNSET)
    return await repo.patch_plan(plan_id, **kwargs)


# ─── Validation helpers ──────────────────────────────────────────────────────


def _validate_plan_id(value: Any) -> None:
    """Reject malformed plan_id with a 400 + actionable detail."""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="plan_id is required and must be a string",
        )
    if len(value) == 0 or len(value) > _PLAN_ID_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan_id length must be 1..{_PLAN_ID_MAX_LEN}",
        )
    if not _PLAN_ID_REGEX.match(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "plan_id must match ^[a-z0-9][a-z0-9-_]{0,254}$ "
                "(lowercase letters / digits / dash / underscore; start with alnum)"
            ),
        )


def _validate_plan_name(value: Any) -> None:
    """Reject malformed name with a 400 + actionable detail."""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="name is required and must be a string",
        )
    if len(value) == 0 or len(value) > _PLAN_NAME_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"name length must be 1..{_PLAN_NAME_MAX_LEN}",
        )


def _coerce_budget(value: Any) -> Decimal | None:
    """Coerce budget_usd input into Decimal or None.

    Accepts int / float / str / None. Returns ``None`` for null /
    missing. Raises 400 for non-numeric strings or negative values
    (a budget cap of -1 USD makes no sense; the worker would never
    claim again).
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="budget_usd must be a number, not a boolean",
        )
    if isinstance(value, (int, float, str)):
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"budget_usd is not a valid decimal: {exc}",
            ) from None
        if dec < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="budget_usd must be >= 0",
            )
        return dec
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="budget_usd must be a number, string, or null",
    )


# ─── Event log ───────────────────────────────────────────────────────────────


def _append_plan_event(event: dict[str, Any]) -> None:
    """Append a single JSON line to whilly_events.jsonl.

    Mirrors :func:`whilly.api.auth_routes._append_event` so the audit
    surface stays consistent. Best-effort: filesystem errors log a
    warning but do not raise — operator-facing CRUD must not fail
    because the event log is read-only or full.
    """
    event = dict(event)
    event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    log_path = Path(os.environ.get(EVENT_LOG_PATH_ENV, DEFAULT_EVENT_LOG_PATH))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    except OSError as exc:
        logger.warning("plans: event-log append failed (%s): %s", log_path, exc)


# ─── Cursor codec ────────────────────────────────────────────────────────────


def _encode_cursor(last_event_at_iso: object, plan_id: str) -> str:
    """Encode (last_event_at, id) as a base64url string with no padding.

    ``last_event_at_iso`` is either an ISO-8601 string (from the JSON
    response we just built) or ``None``; in the latter case we emit a
    sentinel ``-`` so the decoder can round-trip a NULL.
    """
    head = last_event_at_iso if isinstance(last_event_at_iso, str) and last_event_at_iso else "-"
    raw = f"{head}|{plan_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[object | None, str | None]:
    """Decode a cursor into ``(last_event_at, id)``.

    Raises ``HTTPException(400)`` on malformed input — operators get a
    clean error rather than a Postgres parse failure deep in the query.
    Returns ``(None, None)`` when no cursor was supplied.
    """
    if cursor is None or cursor == "":
        return (None, None)
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"malformed cursor: {exc}") from None
    if "|" not in raw:
        raise HTTPException(status_code=400, detail="malformed cursor: missing separator")
    head, _, tail = raw.partition("|")
    if not tail:
        raise HTTPException(status_code=400, detail="malformed cursor: empty plan id")
    if head == "-":
        # NULL last_event_at — Postgres compares NULLS LAST, so the
        # cursor row is in the "no events yet" tail of the result. We
        # pass an ``-infinity`` sentinel and the WHERE clause treats it
        # as "rows whose last_event_at is also NULL and id > $cursor_id".
        from datetime import datetime, timezone

        # asyncpg accepts datetime.min as -infinity equivalent in TZ-aware form.
        return (datetime.min.replace(tzinfo=timezone.utc), tail)
    # Validate ISO-8601 format so a malformed cursor produces a clean
    # 400 rather than a Postgres ``invalid input syntax for type
    # timestamp with time zone`` 500 deep in the query plan.
    from datetime import datetime

    try:
        # ``fromisoformat`` accepts both ``...+00:00`` and ``...Z`` since 3.11.
        parsed = datetime.fromisoformat(head.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"malformed cursor timestamp: {exc}") from None
    return (parsed, tail)


__all__ = ["build_plans_router"]
