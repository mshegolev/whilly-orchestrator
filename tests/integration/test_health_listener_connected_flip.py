"""Integration test for the M3 /health.listener_connected true→false→true flip.

Validates VAL-M3-HEALTH-902: the ``listener_connected`` flag in the
``GET /health`` body must reflect the actual asyncpg LISTEN session
state — not just whether the supervisor task is alive. Because
:func:`whilly.api.sse.event_notify_listener_loop` auto-reconnects
forever, the supervisor task is *never* ``done`` during a
``pg_terminate_backend``-induced reconnect. The flag must therefore be
state-coupled to :class:`whilly.api.sse._ListenerState.connected`,
which toggles ``True`` after :meth:`asyncpg.Connection.add_listener`
succeeds and ``False`` in the loop's finally block.

The test boots ``create_app`` against a real testcontainer Postgres,
waits for the listener to settle (``listener_connected==true``), kills
the listener's backend with ``pg_terminate_backend(...)``, then polls
``/health`` every 50 ms for up to 3 s and asserts at least one
``false`` reading is observed inside the reconnect window before the
flag flips back to ``true``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.sse import LISTENER_APPLICATION_NAME

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-health-flip-test"


@pytest.fixture
async def app(db_pool: asyncpg.Pool, postgres_dsn: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.1,
        claim_poll_interval=0.05,
        event_flush_interval_seconds=10.0,
        event_batch_limit=10_000,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
        dsn=postgres_dsn,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _wait_for_listener_connected(client: AsyncClient, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        response = await client.get("/health")
        if response.json().get("listener_connected") is True:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"listener_connected did not become true within {timeout}s")


async def _wait_for_listener_row(db_pool: asyncpg.Pool, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pg_stat_activity WHERE application_name = $1",
                LISTENER_APPLICATION_NAME,
            )
        if row is not None:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"listener pg_stat_activity row did not appear within {timeout}s")


async def test_app_state_exposes_event_notify_listener_state(app: FastAPI) -> None:
    """Lifespan must stash a ``_ListenerState`` instance for handlers to read."""
    from whilly.api.sse import _ListenerState

    state = getattr(app.state, "event_notify_listener_state", None)
    assert isinstance(state, _ListenerState)


async def test_health_listener_connected_true_under_healthy_db(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    await _wait_for_listener_row(db_pool)
    await _wait_for_listener_connected(client)
    body = (await client.get("/health")).json()
    assert body["listener_connected"] is True
    assert body["status"] == "ok"


async def test_health_listener_connected_flips_true_false_true_on_pg_terminate(
    client: AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _wait_for_listener_row(db_pool)
    await _wait_for_listener_connected(client)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = $1",
            LISTENER_APPLICATION_NAME,
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    saw_false = False
    saw_true_after_false = False
    readings: list[bool] = []
    while loop.time() < deadline:
        body = (await client.get("/health")).json()
        flag = bool(body["listener_connected"])
        readings.append(flag)
        if not flag:
            saw_false = True
        elif saw_false:
            saw_true_after_false = True
            break
        await asyncio.sleep(0.05)

    assert saw_false, (
        f"expected at least one listener_connected==false reading during reconnect window; readings={readings!r}"
    )
    if not saw_true_after_false:
        await _wait_for_listener_connected(client, timeout=8.0)
        saw_true_after_false = True
    assert saw_true_after_false, "expected listener_connected to flip back to true after reconnect"
