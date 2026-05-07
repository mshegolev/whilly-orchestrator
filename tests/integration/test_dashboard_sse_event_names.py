"""Integration test pinning the dashboard tasks-table SSE event-name contract.

Background
----------
The HTMX dashboard subscribes the tasks-table to SSE events emitted by
the M3 event-notify broker. The broker forwards Postgres ``NOTIFY
whilly_events`` payloads verbatim, and the SSE endpoint stamps the
``event:`` line on every frame using ``payload["event_type"]`` (see
:func:`whilly.api.sse_endpoint._frame_from_broker_payload`).

The ``events.event_type`` column for task-lifecycle rows is written by
:class:`whilly.adapters.db.repository.TaskRepository` as the **uppercase**
:class:`whilly.core.state_machine.Transition` value
(``CLAIM`` / ``START`` / ``COMPLETE`` / ``FAIL`` / ``RELEASE`` / ``SKIP``)
for the worker-driven transitions, plus dotted audit events such as
``task.created`` and ``task.skipped`` for import/skip paths. Any missing
subscription in the template would silently miss live updates, leaving only
the 5-second polling fallback (round 3 finding #3 / VAL-M3-HTMX-010 / -016 /
-017).

This test drives a real Postgres ``NOTIFY`` (via INSERT-into-events,
which fires the migration-011 trigger) and asserts that the SSE frame
``event:`` line value emitted by :func:`stream_event_source` matches the
exact set of names the ``_tasks_table.html`` template's ``hx-trigger``
attribute subscribes to.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import asyncpg
import pytest
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.sse import EventNotifyBroker, LISTENER_APPLICATION_NAME
from whilly.api.sse_endpoint import stream_event_source

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-dashboard-sse-event-names"

_BROKER_TASK_EVENT_NAMES: tuple[str, ...] = (
    "task.created",
    "CLAIM",
    "START",
    "COMPLETE",
    "FAIL",
    "RELEASE",
    "SKIP",
    "task.skipped",
    "human_review.required",
    "human_review.approved",
    "human_review.rejected",
    "human_review.changes_requested",
)

_TEMPLATES_DIR: Path = Path(__file__).resolve().parents[2] / "whilly" / "api" / "templates"
_TASKS_TEMPLATE: Path = _TEMPLATES_DIR / "_tasks_table.html"
_WORKERS_TEMPLATE: Path = _TEMPLATES_DIR / "_workers_table.html"


def _extract_sse_event_names(template_path: Path) -> set[str]:
    """Return the set of ``sse:<name>`` triggers from a template's ``hx-trigger``."""
    text = template_path.read_text(encoding="utf-8")
    match = re.search(r'hx-trigger="([^"]*)"', text)
    assert match is not None, f"hx-trigger attribute missing in {template_path}"
    triggers = match.group(1)
    return {raw.strip().removeprefix("sse:") for raw in triggers.split(",") if raw.strip().startswith("sse:")}


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


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"plan {plan_id}",
        )
    return plan_id


async def _insert_task(pool: asyncpg.Pool, *, task_id: str, plan_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, plan_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            task_id,
            plan_id,
        )


async def _insert_event(
    pool: asyncpg.Pool,
    *,
    event_type: str,
    task_id: str,
    plan_id: str,
    payload: dict[str, Any] | None = None,
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events (task_id, plan_id, event_type, payload, detail) "
            "VALUES ($1, $2, $3, $4::jsonb, NULL) RETURNING id",
            task_id,
            plan_id,
            event_type,
            json.dumps(payload or {}),
        )
    assert row is not None
    return int(row["id"])


def _request_stub_with_disconnect_after(n_calls: int) -> Any:
    """Return a Request stub that reports disconnected after ``n_calls`` checks."""
    counter = {"n": 0}

    async def _is_disconnected() -> bool:
        counter["n"] += 1
        return counter["n"] > n_calls

    stub = MagicMock()
    stub.is_disconnected = _is_disconnected
    return stub


def test_template_subscribes_to_uppercase_broker_event_names() -> None:
    """``_tasks_table.html`` hx-trigger names match what mutates task rows.

    Round 3 finding #3: the prior ``sse:task.claim, …`` set missed the
    broker's UPPERCASE transition values, while later import/skip flows need
    the dotted audit-event names. Pin the full corrected set here so any
    future template edit silently dropping a task update fails the suite.
    """
    subscribed = _extract_sse_event_names(_TASKS_TEMPLATE)
    assert subscribed == set(_BROKER_TASK_EVENT_NAMES), (
        f"_tasks_table.html hx-trigger subscriptions {subscribed!r} must equal broker event names "
        f"{set(_BROKER_TASK_EVENT_NAMES)!r}"
    )


def test_workers_template_subscription_unchanged() -> None:
    """Regression guard: workers-table keeps its dotted-lowercase event names.

    The broker emits ``worker.registered`` / ``worker.heartbeat`` /
    ``worker.revoked`` / ``worker.offline`` verbatim from the audit
    layer (``WORKER_REGISTERED_EVENT_TYPE = "worker.registered"`` and the
    other constants in ``whilly/adapters/db/repository.py``). The
    tasks-table fix MUST NOT touch the workers-table subscription.
    """
    subscribed = _extract_sse_event_names(_WORKERS_TEMPLATE)
    expected = {"worker.registered", "worker.heartbeat", "worker.revoked", "worker.offline"}
    assert subscribed == expected, (
        f"_workers_table.html hx-trigger {subscribed!r} must remain {expected!r} (workers fix-out-of-scope)"
    )


@pytest.mark.parametrize("event_type", _BROKER_TASK_EVENT_NAMES)
async def test_pg_notify_emits_sse_frame_event_line_matching_template(
    event_type: str,
    sse_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """End-to-end: INSERT events row → trigger NOTIFY → SSE ``event:`` line.

    For each task update event the broker actually emits, we drive the real
    Postgres NOTIFY trigger and confirm the SSE frame yielded by
    :func:`stream_event_source` carries an ``event`` field equal to the name
    that ``_tasks_table.html`` ``hx-trigger`` subscribes to.
    """
    plan_id = await _seed_plan(db_pool, f"plan-sse-name-{event_type.lower()}")
    task_id = f"task-sse-name-{event_type.lower()}"
    await _insert_task(db_pool, task_id=task_id, plan_id=plan_id)

    broker: EventNotifyBroker = sse_app.state.event_notify_broker

    request = _request_stub_with_disconnect_after(n_calls=2)
    gen = stream_event_source(
        request=request,
        pool=db_pool,
        broker=broker,
        last_event_id=None,
    )

    async def _drive() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for frame in gen:
            out.append(frame)
            if frame.get("event") == event_type:
                break
        return out

    drive_task = asyncio.create_task(_drive())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while broker.subscriber_count < 1:
            if loop.time() >= deadline:
                drive_task.cancel()
                pytest.fail("subscriber never registered with broker")
            await asyncio.sleep(0.02)

        await _insert_event(
            db_pool,
            event_type=event_type,
            task_id=task_id,
            plan_id=plan_id,
            payload={"transition": event_type},
        )

        frames = await asyncio.wait_for(drive_task, timeout=5.0)
    finally:
        if not drive_task.done():
            drive_task.cancel()

    matching = [f for f in frames if f.get("event") == event_type]
    assert matching, f"expected at least one SSE frame with event={event_type!r}; got {frames!r}"
    frame = matching[0]
    assert frame["event"] == event_type
    data = json.loads(frame["data"])
    assert data["event_type"] == event_type
    assert data["task_id"] == task_id
    assert data["plan_id"] == plan_id


async def test_pg_notify_uppercase_event_lands_within_2s_budget(
    sse_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """VAL-M3-HTMX-016: tasks-table receives a row update within 2 s of NOTIFY.

    Models the htmx-ext-sse trigger latency by measuring the wall-clock
    delta between the INSERT (which fires the trigger) and the broker
    delivering the matching SSE frame. The contract bound is 2 s; the
    in-process NOTIFY round-trip is typically well under 200 ms.
    """
    plan_id = await _seed_plan(db_pool, "plan-sse-budget")
    task_id = "task-sse-budget"
    await _insert_task(db_pool, task_id=task_id, plan_id=plan_id)

    broker: EventNotifyBroker = sse_app.state.event_notify_broker
    sub = broker.subscribe()
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await _insert_event(
            db_pool,
            event_type="CLAIM",
            task_id=task_id,
            plan_id=plan_id,
            payload={"worker_id": "w-budget"},
        )
        item = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
        elapsed = loop.time() - t0
    finally:
        broker.unsubscribe(sub)
    assert isinstance(item, dict)
    assert item.get("event_type") == "CLAIM"
    assert elapsed < 2.0, f"NOTIFY → broker delivery exceeded 2 s budget ({elapsed:.3f}s)"
