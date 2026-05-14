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
import logging
from typing import Final

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request

from whilly.api.auth_routes import authenticate_session_request

logger = logging.getLogger(__name__)

#: Hard ceiling on ``?limit=`` so a hostile or buggy client cannot ask
#: for ten thousand plans in a single round-trip. PRD §6.3 names 500 as
#: the upper bound; we expose 1..500 via the FastAPI ``Query`` validator.
_MAX_LIMIT: Final[int] = 500
_DEFAULT_LIMIT: Final[int] = 100


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
_SELECT_PLANS_SQL: str = """
SELECT
    p.id                   AS id,
    p.name                 AS name,
    p.prd_file             AS prd_file,
    p.budget_usd           AS budget_usd,
    p.archived_at          AS archived_at,
    (
        SELECT MAX(e.emitted_at)
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
                SELECT MAX(e.emitted_at) FROM events e WHERE e.plan_id = p.id
            ) IS NULL AND $2::timestamptz = '-infinity'::timestamptz AND p.id > $3
         OR (
                SELECT MAX(e.emitted_at) FROM events e WHERE e.plan_id = p.id
            ) < $2::timestamptz
         OR (
                SELECT MAX(e.emitted_at) FROM events e WHERE e.plan_id = p.id
            ) = $2::timestamptz AND p.id > $3
      )
  )
ORDER BY
    (SELECT MAX(e.emitted_at) FROM events e WHERE e.plan_id = p.id) DESC NULLS LAST,
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
