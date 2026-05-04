"""Integration tests for the M3 ``GET /events/stream`` SSE endpoint
(m3-sse-endpoint).

Three test surfaces:

* ``stream_event_source`` generator unit tests — exercise the
  replay → live-tail handover, ``Last-Event-ID`` parsing, slow-
  subscriber drop frame, and broker-subscriber lifecycle without
  spinning up an HTTP server.
* In-process ASGITransport for non-streaming behaviour (route
  registration, 401 / 403 auth split, parser surface) — fast, no
  real socket needed.
* Real uvicorn server on a free port for one end-to-end smoke
  (Content-Type, header set, basic stream open) — proves the
  ASGI wiring lands a real ``text/event-stream`` response on the
  wire.

Covers VAL-M3-SSE-ENDPOINT-001..014/016/017/901/902/903/904 fulfilled
by the m3-sse-endpoint feature.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any
from unittest.mock import MagicMock

import asyncpg
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.transport.server import REGISTER_PATH, create_app
from whilly.api.sse import EventNotifyBroker, _DropSentinel
from whilly.api.sse_endpoint import (
    REPLAY_LIMIT,
    _parse_last_event_id,
    stream_event_source,
)

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-sse-endpoint-test"
_LONG_POLL_TIMEOUT = 0.3
_POLL_INTERVAL = 0.05


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_started(server: uvicorn.Server, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not server.started:
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"uvicorn did not signal started within {timeout}s")
        await asyncio.sleep(0.05)


def _make_request_stub(*, disconnected: bool = False) -> Any:
    """Build a minimal ``Request``-shaped stub for the generator.

    The generator only touches :meth:`Request.is_disconnected`, which
    is ``async`` — anything that returns a coroutine yielding the
    desired bool is enough.
    """
    stub = MagicMock()

    async def _is_disconnected() -> bool:
        return disconnected

    stub.is_disconnected = _is_disconnected
    return stub


@pytest.fixture
async def app_no_dsn(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=1,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app_no_dsn: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_no_dsn)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _mint_bootstrap(repo: TaskRepository, plaintext: str, owner: str, *, is_admin: bool = False) -> None:
    await repo.mint_bootstrap_token(plaintext, owner_email=owner, is_admin=is_admin)


async def _register_worker(client: AsyncClient, *, plaintext_bootstrap: str, hostname: str = "h") -> tuple[str, str]:
    resp = await client.post(
        REGISTER_PATH,
        json={"hostname": hostname},
        headers={"Authorization": f"Bearer {plaintext_bootstrap}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["worker_id"], body["token"]


async def _seed_event_row(
    pool: asyncpg.Pool,
    *,
    event_type: str = "audit.note",
    plan_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    async with pool.acquire() as conn:
        if plan_id is not None:
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                plan_id,
                f"plan {plan_id}",
            )
        row = await conn.fetchrow(
            "INSERT INTO events (task_id, plan_id, event_type, payload, detail) "
            "VALUES (NULL, $1, $2, $3::jsonb, NULL) RETURNING id",
            plan_id,
            event_type,
            json.dumps(payload or {}),
        )
        assert row is not None
        return int(row["id"])


# ─── Generator unit tests against an in-memory broker ───────────────────


async def test_generator_replays_events_after_last_event_id(
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-gen-replay"
    id_a = await _seed_event_row(db_pool, event_type="audit.alpha", plan_id=plan_id)
    id_b = await _seed_event_row(db_pool, event_type="audit.beta", plan_id=plan_id)
    id_c = await _seed_event_row(db_pool, event_type="audit.gamma", plan_id=plan_id)

    broker = EventNotifyBroker()
    request = _make_request_stub(disconnected=True)
    gen = stream_event_source(
        request=request,
        pool=db_pool,
        broker=broker,
        last_event_id=id_a,
    )
    frames: list[dict[str, Any]] = []
    async for frame in gen:
        frames.append(frame)

    ids_emitted = [f.get("id") for f in frames if "id" in f]
    assert str(id_b) in ids_emitted
    assert str(id_c) in ids_emitted
    assert str(id_a) not in ids_emitted


async def test_generator_skips_replay_when_last_event_id_none(
    db_pool: asyncpg.Pool,
) -> None:
    await _seed_event_row(db_pool, event_type="audit.x", plan_id="plan-gen-no-replay")
    broker = EventNotifyBroker()
    request = _make_request_stub(disconnected=True)
    gen = stream_event_source(
        request=request,
        pool=db_pool,
        broker=broker,
        last_event_id=None,
    )
    frames = [frame async for frame in gen]
    assert frames == []


async def test_generator_emits_replay_truncated_when_capped(
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-gen-trunc"
    for i in range(6):
        await _seed_event_row(
            db_pool,
            event_type=f"audit.t{i}",
            plan_id=plan_id,
            payload={"i": i},
        )
    broker = EventNotifyBroker()
    request = _make_request_stub(disconnected=True)
    gen = stream_event_source(
        request=request,
        pool=db_pool,
        broker=broker,
        last_event_id=0,
        replay_limit=3,
    )
    frames = [frame async for frame in gen]
    truncated = [f for f in frames if f.get("event") == "replay_truncated"]
    assert truncated, f"expected replay_truncated frame; got {frames!r}"
    payload = json.loads(truncated[0]["data"])
    assert payload["reason"] == "replay_truncated"
    assert payload["cap"] == 3


async def test_generator_emits_drop_frame_on_slow_subscriber_sentinel(
    db_pool: asyncpg.Pool,
) -> None:
    broker = EventNotifyBroker()
    request = _make_request_stub(disconnected=False)
    gen = stream_event_source(
        request=request,
        pool=db_pool,
        broker=broker,
        last_event_id=None,
    )

    async def _drive() -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        async for frame in gen:
            frames.append(frame)
        return frames

    drive_task = asyncio.create_task(_drive())
    deadline = asyncio.get_event_loop().time() + 5.0
    while broker.subscriber_count < 1:
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError("subscriber never registered with broker")
        await asyncio.sleep(0.02)
    sub = next(iter(broker._subscribers))
    sub.queue.put_nowait(_DropSentinel(code=1015))

    frames = await asyncio.wait_for(drive_task, timeout=3.0)
    assert any(f.get("event") == "error" for f in frames)
    error_frame = next(f for f in frames if f.get("event") == "error")
    payload = json.loads(error_frame["data"])
    assert payload["close_code"] == 1015


async def test_generator_unsubscribes_on_completion(
    db_pool: asyncpg.Pool,
) -> None:
    broker = EventNotifyBroker()
    request = _make_request_stub(disconnected=True)
    gen = stream_event_source(
        request=request,
        pool=db_pool,
        broker=broker,
        last_event_id=None,
    )
    before = broker.subscriber_count
    async for _ in gen:  # noqa: F841
        pass
    assert broker.subscriber_count == before


async def test_generator_dedupes_high_water_mark(
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-gen-dedupe"
    id_a = await _seed_event_row(db_pool, event_type="audit.a", plan_id=plan_id)
    id_b = await _seed_event_row(db_pool, event_type="audit.b", plan_id=plan_id)

    broker = EventNotifyBroker()
    is_disconnected_calls = {"n": 0}

    async def _is_disconnected() -> bool:
        is_disconnected_calls["n"] += 1
        return is_disconnected_calls["n"] > 2

    stub = MagicMock()
    stub.is_disconnected = _is_disconnected
    gen = stream_event_source(
        request=stub,
        pool=db_pool,
        broker=broker,
        last_event_id=id_a,
    )

    async def _drive() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for frame in gen:
            out.append(frame)
        return out

    drive_task = asyncio.create_task(_drive())
    deadline = asyncio.get_event_loop().time() + 5.0
    while broker.subscriber_count < 1:
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError("subscriber never registered")
        await asyncio.sleep(0.02)
    broker.fan_out(
        {
            "event_id": id_b,
            "event_type": "audit.b-late",
            "payload": {"dup": True},
        }
    )
    broker.fan_out(
        {
            "event_id": id_b + 1,
            "event_type": "audit.fresh",
            "payload": {"fresh": True},
        }
    )

    frames = await asyncio.wait_for(drive_task, timeout=3.0)
    fresh = [f for f in frames if f.get("event") == "audit.fresh"]
    duplicate = [f for f in frames if f.get("event") == "audit.b-late"]
    assert fresh, f"expected audit.fresh frame in {frames!r}"
    assert not duplicate, f"high-water-mark should suppress audit.b-late: {frames!r}"


# ─── ASGITransport route + auth tests (no streaming required) ───────────


def test_endpoint_registered_on_app(app_no_dsn: FastAPI) -> None:
    paths = {r.path for r in app_no_dsn.routes if hasattr(r, "path")}
    assert "/events/stream" in paths


async def test_missing_bearer_returns_401(client: AsyncClient) -> None:
    response = await client.get("/events/stream")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")


async def test_bad_bearer_returns_403(client: AsyncClient) -> None:
    response = await client.get(
        "/events/stream",
        headers={"Authorization": "Bearer this-is-not-a-real-token"},
    )
    assert response.status_code == 403


def test_parse_last_event_id_accepts_int() -> None:
    assert _parse_last_event_id("42") == 42


def test_parse_last_event_id_rejects_negative() -> None:
    assert _parse_last_event_id("-1") is None


def test_parse_last_event_id_rejects_non_numeric() -> None:
    assert _parse_last_event_id("foo") is None


def test_parse_last_event_id_treats_blank_as_none() -> None:
    assert _parse_last_event_id("   ") is None
    assert _parse_last_event_id(None) is None


def test_replay_limit_default_is_1000() -> None:
    assert REPLAY_LIMIT == 1000


# ─── End-to-end uvicorn smoke (single test) ─────────────────────────────


async def test_real_http_stream_returns_event_stream_with_cors(
    db_pool: asyncpg.Pool,
    postgres_dsn: str,
) -> None:
    repo = TaskRepository(db_pool)
    plaintext_bs = "e2e-live-bs"
    await _mint_bootstrap(repo, plaintext_bs, "e2e@example.com")

    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=1,
        dsn=postgres_dsn,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="sse-uvicorn-e2e")
    try:
        await _wait_until_started(server)
        broker: EventNotifyBroker = app.state.event_notify_broker

        async def _push() -> None:
            deadline = asyncio.get_event_loop().time() + 5.0
            while broker.subscriber_count < 1:
                if asyncio.get_event_loop().time() >= deadline:
                    return
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            broker.fan_out(
                {
                    "event_id": 99999,
                    "event_type": "audit.live-e2e",
                    "task_id": None,
                    "plan_id": None,
                    "payload": {"phase": "live-tail"},
                }
            )

        push_task = asyncio.create_task(_push())
        try:
            collected = bytearray()
            origin = "http://dashboard.local"
            async with httpx.AsyncClient(timeout=10.0) as ac:
                async with ac.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/events/stream",
                    headers={
                        "Authorization": f"Bearer {plaintext_bs}",
                        "Origin": origin,
                    },
                ) as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers.get("content-type", "")
                    assert resp.headers.get("cache-control", "").lower().startswith("no-cache")
                    assert resp.headers.get("x-accel-buffering") == "no"
                    assert resp.headers.get("access-control-allow-origin") == origin
                    deadline = asyncio.get_event_loop().time() + 6.0
                    try:
                        async for chunk in resp.aiter_raw():
                            collected.extend(chunk)
                            if b"audit.live-e2e" in collected and b"event: ping" in collected:
                                break
                            if asyncio.get_event_loop().time() >= deadline:
                                break
                    except (httpx.RemoteProtocolError, httpx.ReadError):
                        pass
            text = bytes(collected).decode("utf-8", errors="ignore")
            assert "audit.live-e2e" in text, f"missing live frame in: {text!r}"
            assert "event: ping" in text, f"missing heartbeat in: {text!r}"
        finally:
            push_task.cancel()
            with suppress(asyncio.CancelledError):
                await push_task
    finally:
        server.should_exit = True
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)
        if not server_task.done():
            server_task.cancel()
            with suppress(asyncio.CancelledError, BaseException):
                await server_task


async def test_pyproject_lists_sse_starlette_in_server_extras() -> None:
    import tomllib
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    server_extras = pyproject["project"]["optional-dependencies"]["server"]
    assert any(dep.startswith("sse-starlette") for dep in server_extras), (
        f"sse-starlette missing from [project.optional-dependencies].server: {server_extras!r}"
    )
