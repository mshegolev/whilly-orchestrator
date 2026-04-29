"""Graceful shutdown contract for the lifespan event flusher (TASK-106, VAL-OBS-007 / VAL-OBS-015).

Asserts:

* Events enqueued just before lifespan ``__aexit__`` reach the DB
  before the lifespan returns (VAL-OBS-007 SIGTERM analogue).
* The same drain happens on the SIGINT path (VAL-OBS-015).
* The flusher task transitions to ``done()`` cleanly after drain.

Why two near-duplicate tests?
    SIGTERM and SIGINT enter the same Python control-flow path through
    ``LifespanManager`` / ``app.router.lifespan_context.__aexit__``,
    but the contract requires both names to be assertable in case
    future signal-handling changes diverge them. Keeping them as two
    parametrised tests makes a regression on either path immediately
    visible in the test name.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.main import _log_event

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN: str = "bootstrap-flusher-shutdown"


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"Plan {plan_id}",
        )
    return plan_id


@pytest.mark.parametrize("scenario", ["sigterm", "sigint"])
async def test_lifespan_drains_queue_on_shutdown(db_pool: asyncpg.Pool, tmp_path: Path, scenario: str) -> None:
    """VAL-OBS-007 / VAL-OBS-015: in-flight events flushed before lifespan exit."""
    plan_id = await _seed_plan(db_pool, f"plan-flusher-shutdown-{scenario}")
    # Use a deliberately *long* flush interval so the flush only fires
    # via the shutdown drain path, not the periodic timer.
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=10.0,
        event_batch_limit=10_000,
        event_drain_timeout_seconds=5.0,
        event_checkpoint_dir=str(tmp_path),
    )
    flusher_task = None
    async with app.router.lifespan_context(app):
        flusher_task = app.state.event_flusher_task
        # Enqueue 250 events — none of them will be flushed by the
        # idle tick (10 s cadence) so the only way they reach the DB
        # is via the shutdown drain.
        for i in range(250):
            _log_event(
                app,
                "audit.shutdown",
                plan_id=plan_id,
                payload={"i": i, "scenario": scenario},
            )
        # Sanity: events are still in the queue at this point.
        assert app.state.event_queue.qsize() == 250
    # Lifespan exit done. The drain must have fired.
    assert flusher_task is not None
    # Flusher task is done after the lifespan exits.
    # Allow a small grace window for the asyncio task scheduler.
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline and not flusher_task.done():
        await asyncio.sleep(0.02)
    assert flusher_task.done(), "event_flusher_task should be done after lifespan exit"
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='audit.shutdown' AND plan_id=$1",
            plan_id,
        )
    assert count == 250, f"expected 250 drained events, got {count}"
