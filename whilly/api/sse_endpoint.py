"""HTTP endpoint surface for the M3 ``GET /events/stream`` SSE route.

Sits one layer above :mod:`whilly.api.sse` (broker + listener loop) and
wires the ASGI generator that ``EventSourceResponse`` consumes:

* ``Last-Event-ID`` header parse + sanitisation (malformed → start fresh)
* DB replay of any committed events with ``id > last_event_id`` capped
  at :data:`REPLAY_LIMIT` (1000 rows) before handing the subscriber over
  to live broker fan-out
* synthetic ``replay_truncated`` frame when the cap fires
* slow-subscriber close (``_DropSentinel`` → final ``error`` frame +
  generator return) and lifespan-shutdown close (same path, distinct
  reason in the data field)
* per-frame de-duplication: events with ``event_id <= last_replayed_id``
  are silently skipped so the replay→live handover stays gap-free.

The endpoint accepts EITHER a per-worker bearer token OR a
bootstrap-token row OR (one-minor-version legacy fallback) the static
``WHILLY_WORKER_TOKEN`` / ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` env values.
Missing bearer → 401; bearer present but does not match anything → 403
(VAL-M3-SSE-ENDPOINT-002 / -003 split).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any, Final

import asyncpg
from fastapi import HTTPException, Request, status

from whilly.adapters.db import TaskRepository
from whilly.adapters.transport.auth import (
    _BEARER_REALM,
    _extract_bearer,
    hash_bearer_token,
)
from whilly.api.sse import (
    EventNotifyBroker,
    Subscriber,
    _DropSentinel,
)

logger = logging.getLogger(__name__)


REPLAY_LIMIT: Final[int] = 1000

DASHBOARD_DEFAULT_ORIGIN: Final[str] = "*"

_REPLAY_SQL: Final[str] = (
    "SELECT id, event_type, task_id, plan_id, payload FROM events WHERE id > $1 ORDER BY id ASC LIMIT $2"
)


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        value = int(candidate)
    except ValueError:
        logger.warning("invalid Last-Event-ID header, starting fresh: %r", raw)
        return None
    if value < 0:
        logger.warning("invalid Last-Event-ID header, starting fresh: %r", raw)
        return None
    return value


async def _authenticate_stream_request(
    *,
    repo: TaskRepository,
    authorization: str | None,
    legacy_worker_token: str | None,
    legacy_bootstrap_token: str | None,
) -> None:
    """Auth gate for ``GET /events/stream``.

    Returns ``None`` on success. Raises 401 when no bearer was supplied
    (or the header is malformed); raises 403 when a bearer was supplied
    but matches neither a registered worker, an active bootstrap-token
    row, nor a configured legacy fallback.
    """
    token = _extract_bearer(authorization)
    token_hash = hash_bearer_token(token)
    identity_lookup = getattr(repo, "get_worker_identity_by_token_hash", None)
    if identity_lookup is not None:
        identity = await identity_lookup(token_hash)
    else:
        worker_id_only = await repo.get_worker_id_by_token_hash(token_hash)
        identity = (worker_id_only, None) if worker_id_only is not None else None
    if identity is not None:
        return None
    bootstrap_owner: tuple[str, bool] | None = None
    bootstrap_lookup = getattr(repo, "get_bootstrap_token_owner", None)
    if bootstrap_lookup is not None:
        try:
            bootstrap_owner = await bootstrap_lookup(token)
        except Exception:
            logger.exception("sse_endpoint: bootstrap lookup raised")
            bootstrap_owner = None
    if bootstrap_owner is not None:
        return None
    if legacy_worker_token is not None and secrets.compare_digest(token, legacy_worker_token):
        return None
    if legacy_bootstrap_token is not None and secrets.compare_digest(token, legacy_bootstrap_token):
        return None
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="invalid token",
        headers={"WWW-Authenticate": f'Bearer realm="{_BEARER_REALM}"'},
    )


def _frame_from_event_row(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    payload_raw = row["payload"]
    if isinstance(payload_raw, str):
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
    elif isinstance(payload_raw, dict):
        payload = payload_raw
    else:
        payload = {}
    event_id = int(row["id"])
    event_type = row["event_type"]
    data_obj = {
        "event_id": event_id,
        "event_type": event_type,
        "task_id": row["task_id"],
        "plan_id": row["plan_id"],
        "payload": payload,
    }
    return {
        "id": str(event_id),
        "event": event_type,
        "data": json.dumps(data_obj, default=str),
    }


def _frame_from_broker_payload(payload: dict[str, Any]) -> dict[str, Any]:
    event_id = payload.get("event_id")
    event_type = payload.get("event_type") or "message"
    frame: dict[str, Any] = {
        "event": event_type,
        "data": json.dumps(payload, default=str),
    }
    if event_id is not None:
        frame["id"] = str(event_id)
    return frame


def _truncated_frame(*, missed_after_id: int, cap: int = REPLAY_LIMIT) -> dict[str, Any]:
    return {
        "event": "replay_truncated",
        "data": json.dumps(
            {
                "reason": "replay_truncated",
                "missed_after_id": missed_after_id,
                "cap": cap,
            }
        ),
    }


def _drop_frame(*, code: int, reason: str = "slow_subscriber") -> dict[str, Any]:
    return {
        "event": "error",
        "data": json.dumps({"reason": reason, "close_code": code}),
    }


async def stream_event_source(
    *,
    request: Request,
    pool: asyncpg.Pool,
    broker: EventNotifyBroker,
    last_event_id: int | None,
    replay_limit: int = REPLAY_LIMIT,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE frames for one ``GET /events/stream`` consumer.

    Replay (if ``last_event_id`` is set) → live tail. Cleans up the
    broker subscription on exit even if the generator is cancelled.
    """
    sub: Subscriber = broker.subscribe(last_event_id=last_event_id)
    high_water_mark: int | None = last_event_id
    try:
        if last_event_id is not None:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(_REPLAY_SQL, last_event_id, replay_limit + 1)
            except Exception:
                logger.exception("sse_endpoint: replay query failed")
                yield _drop_frame(code=1011, reason="db_error_during_replay")
                return
            truncated = len(rows) > replay_limit
            for row in rows[:replay_limit]:
                frame = _frame_from_event_row(row)
                yield frame
                row_id = int(row["id"])
                if high_water_mark is None or row_id > high_water_mark:
                    high_water_mark = row_id
            if truncated:
                yield _truncated_frame(missed_after_id=last_event_id, cap=replay_limit)

        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(item, _DropSentinel):
                yield _drop_frame(code=item.code, reason="subscriber_dropped")
                return
            if not isinstance(item, dict):
                continue
            event_id = item.get("event_id")
            if isinstance(event_id, int) and high_water_mark is not None and event_id <= high_water_mark:
                continue
            if isinstance(event_id, int):
                if high_water_mark is None or event_id > high_water_mark:
                    high_water_mark = event_id
            yield _frame_from_broker_payload(item)
    finally:
        broker.unsubscribe(sub)


__all__ = [
    "DASHBOARD_DEFAULT_ORIGIN",
    "REPLAY_LIMIT",
    "_authenticate_stream_request",
    "_drop_frame",
    "_frame_from_broker_payload",
    "_frame_from_event_row",
    "_parse_last_event_id",
    "_truncated_frame",
    "stream_event_source",
]
