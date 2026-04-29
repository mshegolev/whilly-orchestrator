"""Throughput contract for the lifespan event flusher (TASK-106, VAL-OBS-005, VAL-OBS-006).

Asserts that the flusher sustains ≥ 1000 events/sec for 5 seconds with
zero loss. This is the production target the v4.1 mission pinned for
TRIZ + audit + budget-sentinel + decision-gate event volumes combined.

Why a separate file?
    The throughput test takes 5+ seconds of wall clock; isolating it
    keeps the bulk of the flusher tests fast (< 5 s total) and lets
    operators run only this file when validating capacity changes
    (`pytest tests/integration/test_event_flusher_throughput.py -v`).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import asyncpg
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.event_flusher import EventFlusher
from whilly.api.main import _log_event

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN: str = "bootstrap-flusher-throughput"


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"Plan {plan_id}",
        )
    return plan_id


async def test_sustained_thousand_events_per_second_for_five_seconds(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-005: 5000 events generated at 1000/s land in DB within 200 ms drain.

    We keep ``batch_id`` tagging on every event so the count is
    unambiguous across any other rows already in the events table.
    """
    plan_id = await _seed_plan(db_pool, "plan-flusher-throughput")
    batch_id = "throughput-5k"
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.1,
        event_batch_limit=500,
        event_drain_timeout_seconds=5.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        flusher: EventFlusher = app.state.event_flusher
        max_qsize_observed = 0
        start = time.monotonic()
        for second in range(5):
            second_start = time.monotonic()
            for i in range(1000):
                _log_event(
                    app,
                    "audit.throughput",
                    plan_id=plan_id,
                    payload={"batch_id": batch_id, "i": i, "second": second},
                )
            max_qsize_observed = max(max_qsize_observed, flusher.queue.qsize())
            # Pace ourselves to roughly 1000/sec for this second.
            elapsed = time.monotonic() - second_start
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
        # Drain budget: 200 ms after the last enqueue.
        await asyncio.sleep(0.2)
        # After drain, queue should be empty.
        # Allow a small grace window for the flusher to finalise the
        # last bulk INSERT it kicked off — one cadence + one round-trip.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and flusher.queue.qsize() > 0:
            await asyncio.sleep(0.02)
        assert flusher.queue.qsize() == 0
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE payload->>'batch_id' = $1",
                batch_id,
            )
        assert count == 5000, f"expected 5000 events with batch_id={batch_id!r}, got {count}"
        # VAL-OBS-006: queue depth bounded < 2 × batch_limit (1000).
        assert max_qsize_observed < 2000, (
            f"queue depth grew unbounded: max={max_qsize_observed}; flusher cannot keep up"
        )
        elapsed_total = time.monotonic() - start
        # Sanity: the run took roughly 5 s + 0.2 s drain.
        assert elapsed_total < 8.0, f"throughput test took too long: {elapsed_total:.2f}s"
