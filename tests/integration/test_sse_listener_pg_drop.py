"""Integration tests for the M3 SSE event-notify listener (m3-sse-listener).

End-to-end coverage of the dedicated asyncpg LISTEN connection running
inside :func:`whilly.adapters.transport.server.create_app`'s lifespan
TaskGroup. Drives the real Postgres NOTIFY trigger (migration 011) so
the listener observes a row hit ``events`` → trigger fires ``pg_notify``
→ broker fans out the payload onto each subscriber's queue.

Verifies the contract assertions VAL-M3-SSE-LISTENER-001 / -002 / -003 /
-004 / -005 / -007 / -008 / -011 fulfilled by the m3-sse-listener
feature.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.sse import (
    EVENT_NOTIFY_LISTENER_TASK_NAME,
    LISTENER_APPLICATION_NAME,
    NOTIFY_CHANNEL,
    EventNotifyBroker,
    _DropSentinel,
)

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN: str = "bootstrap-sse-listener-test"


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"Plan {plan_id}",
        )
    return plan_id


async def _insert_event(
    pool: asyncpg.Pool,
    *,
    event_type: str,
    plan_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events (task_id, plan_id, event_type, payload, detail) "
            "VALUES (NULL, $1, $2, $3::jsonb, NULL) RETURNING id",
            plan_id,
            event_type,
            json.dumps(payload or {}),
        )
    assert row is not None
    return int(row["id"])


@pytest.fixture
async def sse_app(db_pool: asyncpg.Pool, postgres_dsn: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=10.0,
        event_batch_limit=10_000,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
        dsn=postgres_dsn,
    )
    async with app.router.lifespan_context(app):
        # Wait for the listener to attach LISTEN before yielding so
        # tests that immediately INSERT events don't race against the
        # connect handshake.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM pg_stat_activity WHERE application_name = $1",
                    LISTENER_APPLICATION_NAME,
                )
            if row is not None:
                break
            await asyncio.sleep(0.05)
        yield app


async def _wait_for(condition, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


async def test_listener_task_registered_in_taskgroup(sse_app: FastAPI) -> None:
    tasks = sse_app.state.background_tasks
    assert tasks is not None
    names = [t.get_name() for t in tasks]
    assert EVENT_NOTIFY_LISTENER_TASK_NAME in names
    assert "whilly-visibility-sweep" in names
    assert "whilly-offline-worker-sweep" in names
    assert "whilly-event-flusher" in names


async def test_listener_owns_dedicated_connection_outside_pool(sse_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    state = sse_app.state.event_notify_listener_task
    assert state is not None

    async def _present() -> bool:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT application_name FROM pg_stat_activity WHERE application_name = $1",
                LISTENER_APPLICATION_NAME,
            )
        return any(r["application_name"] == LISTENER_APPLICATION_NAME for r in rows)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        if await _present():
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"listener connection not visible in pg_stat_activity within budget; task={state!r}")


async def test_listener_uses_listen_channel(sse_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    async def _listening_present() -> bool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pg_stat_activity sa, "
                "LATERAL pg_listening_channels() ch "
                "WHERE sa.application_name = $1 AND ch = $2 "
                "AND sa.pid = pg_backend_pid()",
                LISTENER_APPLICATION_NAME,
                NOTIFY_CHANNEL,
            )
            return row is not None

    plan_id = await _seed_plan(db_pool, "plan-sse-listening-channel")
    broker: EventNotifyBroker = sse_app.state.event_notify_broker
    sub = broker.subscribe()
    try:
        await _insert_event(
            db_pool,
            event_type="audit.note",
            plan_id=plan_id,
            payload={"k": "listening-channel"},
        )
        item = await asyncio.wait_for(sub.queue.get(), timeout=5.0)
    finally:
        broker.unsubscribe(sub)
    assert isinstance(item, dict)
    assert item.get("event_type") == "audit.note"
    assert item.get("plan_id") == plan_id


async def test_event_notify_queue_attribute_present(sse_app: FastAPI) -> None:
    assert isinstance(sse_app.state.event_notify_queue, asyncio.Queue)


async def test_fan_out_delivers_to_two_simultaneous_subscribers(sse_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    plan_id = await _seed_plan(db_pool, "plan-sse-fanout-2")
    broker: EventNotifyBroker = sse_app.state.event_notify_broker
    sub_a = broker.subscribe()
    sub_b = broker.subscribe()
    try:
        await _insert_event(
            db_pool,
            event_type="audit.fanout",
            plan_id=plan_id,
            payload={"k": "fan-out-1"},
        )
        await _insert_event(
            db_pool,
            event_type="audit.fanout",
            plan_id=plan_id,
            payload={"k": "fan-out-2"},
        )
        a1 = await asyncio.wait_for(sub_a.queue.get(), timeout=5.0)
        a2 = await asyncio.wait_for(sub_a.queue.get(), timeout=5.0)
        b1 = await asyncio.wait_for(sub_b.queue.get(), timeout=5.0)
        b2 = await asyncio.wait_for(sub_b.queue.get(), timeout=5.0)
    finally:
        broker.unsubscribe(sub_a)
        broker.unsubscribe(sub_b)

    seen_a = {a1["payload"]["k"], a2["payload"]["k"]}
    seen_b = {b1["payload"]["k"], b2["payload"]["k"]}
    assert seen_a == {"fan-out-1", "fan-out-2"}
    assert seen_b == {"fan-out-1", "fan-out-2"}


async def test_listener_reconnects_after_pg_terminate_backend(sse_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    plan_id = await _seed_plan(db_pool, "plan-sse-reconnect")
    broker: EventNotifyBroker = sse_app.state.event_notify_broker

    sub = broker.subscribe()
    try:
        # Sanity: deliver one event before termination.
        await _insert_event(
            db_pool,
            event_type="audit.before-drop",
            plan_id=plan_id,
            payload={"phase": "before"},
        )
        first = await asyncio.wait_for(sub.queue.get(), timeout=5.0)
        assert first["payload"]["phase"] == "before"

        # Terminate the listener's backend to force a reconnect.
        async with db_pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = $1",
                LISTENER_APPLICATION_NAME,
            )

        # Wait for the reconnect — the listener row reappears in
        # pg_stat_activity after backoff + new connect.
        async def _back_online() -> bool:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM pg_stat_activity WHERE application_name = $1",
                    LISTENER_APPLICATION_NAME,
                )
                return row is not None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0
        while loop.time() < deadline:
            if await _back_online():
                break
            await asyncio.sleep(0.1)
        assert await _back_online(), "listener did not reconnect within budget"

        # Drain the queue of any straggler events from the pre-drop
        # connection; then deliver a fresh event under the new
        # connection.
        while not sub.queue.empty():
            sub.queue.get_nowait()
        await _insert_event(
            db_pool,
            event_type="audit.after-reconnect",
            plan_id=plan_id,
            payload={"phase": "after"},
        )
        item = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
        assert isinstance(item, dict)
        assert item["payload"]["phase"] == "after"
    finally:
        broker.unsubscribe(sub)


async def test_listener_clean_teardown_when_sweep_stop_set(
    db_pool: asyncpg.Pool, postgres_dsn: str, tmp_path: Path
) -> None:
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=10.0,
        event_batch_limit=10_000,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
        dsn=postgres_dsn,
    )
    listener_task = None
    async with app.router.lifespan_context(app):
        listener_task = app.state.event_notify_listener_task
        # Subscribe so we can verify the drop sentinel arrives.
        broker: EventNotifyBroker = app.state.event_notify_broker
        sub = broker.subscribe()
        await asyncio.sleep(0.1)
    # Lifespan exited — listener task must be done within ~1s of stop.
    assert listener_task is not None
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline and not listener_task.done():
        await asyncio.sleep(0.02)
    assert listener_task.done(), "listener task should be done after lifespan exit"

    # Listener row should be gone from pg_stat_activity.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_stat_activity WHERE application_name = $1",
            LISTENER_APPLICATION_NAME,
        )
    assert row is None, "listener PG connection still present after teardown"

    # The subscriber should have received a drop sentinel.
    assert sub.dropped is True
    item = sub.queue.get_nowait()
    assert isinstance(item, _DropSentinel)


async def test_listener_survives_malformed_pg_notify_payload(sse_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    plan_id = await _seed_plan(db_pool, "plan-sse-malformed")
    broker: EventNotifyBroker = sse_app.state.event_notify_broker
    sub = broker.subscribe()
    try:
        # Fire a malformed payload directly via NOTIFY (bypasses the
        # trigger so the bytes really are non-JSON).
        async with db_pool.acquire() as conn:
            await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}, 'NOT-JSON-{{'")
        # The listener should drop the malformed payload and keep
        # delivering subsequent valid events.
        await _insert_event(
            db_pool,
            event_type="audit.after-malformed",
            plan_id=plan_id,
            payload={"k": "still-alive"},
        )
        item = await asyncio.wait_for(sub.queue.get(), timeout=5.0)
        assert isinstance(item, dict)
        assert item["event_type"] == "audit.after-malformed"
    finally:
        broker.unsubscribe(sub)
