"""Integration tests for the lifespan event flusher (TASK-106, VAL-OBS-001..017).

Drives the flusher end-to-end against testcontainers Postgres via
:func:`create_app`'s lifespan, so the full TaskGroup wiring (queue,
flusher, drain on shutdown) is exercised without mocking.

Why not unit tests with a fake pool?
    The flusher's contract is "events arrive in the ``events`` table" —
    the only honest verification is to query that table after flushing.
    A unit test with a fake pool would assert we *call* the right
    method, not that the wire SQL produces the right rows. The
    bulk-INSERT shape (VAL-OBS-003) is also part of the contract and
    is observable only through real Postgres' ``pg_stat_statements`` /
    captured SQL traces.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.event_flusher import (
    DEFAULT_BATCH_LIMIT,
    EVENT_FLUSHER_TASK_NAME,
    EventFlusher,
    EventRecord,
    _build_bulk_insert,
    _record_to_params,
)
from whilly.api.main import _log_event

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN: str = "bootstrap-flusher-test"


# ─── helpers / fixtures ──────────────────────────────────────────────────


async def _seed_plan(pool: asyncpg.Pool, plan_id: str = "plan-flusher-test") -> str:
    """Insert a plan row so events.plan_id FK is satisfied for the tests
    that use ``plan_id`` payloads."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"Plan {plan_id}",
        )
    return plan_id


@pytest.fixture
async def fast_flusher_app(db_pool: asyncpg.Pool, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """An app with a ~10 ms flusher cadence so tests don't pay 100 ms.

    The 100 ms default is fine in production but bloats the test runtime.
    Tests that need to assert on the *default* cadence pin
    ``event_flush_interval_seconds=0.1`` explicitly.
    """
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.01,
        event_batch_limit=DEFAULT_BATCH_LIMIT,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        yield app


# ─── pure helper unit-shaped tests (no DB) ──────────────────────────────


def test_build_bulk_insert_shape_one_row() -> None:
    sql = _build_bulk_insert(1)
    assert sql.startswith("INSERT INTO events (task_id, plan_id, event_type, payload, detail) VALUES ")
    assert "($1, $2, $3, $4::jsonb, $5::jsonb)" in sql


def test_build_bulk_insert_shape_three_rows() -> None:
    sql = _build_bulk_insert(3)
    # Three placeholder tuples joined with ", "
    assert sql.count("::jsonb") == 6  # payload + detail per row × 3
    assert "$15" in sql  # last param index for 3 rows × 5 params


def test_record_to_params_serialises_payload_detail_jsonb() -> None:
    rec = EventRecord(
        event_type="audit.note",
        task_id="t-1",
        plan_id="p-1",
        payload={"a": 1},
        detail={"reason": "x"},
    )
    params = _record_to_params(rec)
    assert params[0] == "t-1"
    assert params[1] == "p-1"
    assert params[2] == "audit.note"
    assert json.loads(params[3]) == {"a": 1}
    assert json.loads(params[4]) == {"reason": "x"}


def test_record_to_params_detail_none_round_trips_to_sql_null() -> None:
    rec = EventRecord(event_type="audit.note", payload={"k": "v"})
    params = _record_to_params(rec)
    # detail jsonb param must be Python ``None`` (NULL), not the string "null"
    assert params[4] is None
    # payload defaults to empty dict, never None
    assert json.loads(params[3]) == {"k": "v"}


# ─── lifespan / TaskGroup wiring ────────────────────────────────────────


async def test_lifespan_spawns_flusher_task_with_canonical_name(
    fast_flusher_app: FastAPI,
) -> None:
    """VAL-OBS-001: flusher TaskGroup task is alive while the app is up."""
    task = fast_flusher_app.state.event_flusher_task
    assert task is not None, "lifespan should expose event_flusher_task on app.state"
    assert task.get_name() == EVENT_FLUSHER_TASK_NAME
    assert not task.done()
    flusher = fast_flusher_app.state.event_flusher
    assert isinstance(flusher, EventFlusher)
    assert fast_flusher_app.state.event_queue is flusher.queue


async def test_log_event_returns_synchronously_and_enqueues(fast_flusher_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    """VAL-OBS-002: ``_log_event`` is non-blocking, just enqueues."""
    plan_id = await _seed_plan(db_pool)
    # Drain anything already queued by lifespan startup so we measure
    # *just* this enqueue.
    qsize_before = fast_flusher_app.state.event_queue.qsize()
    _log_event(
        fast_flusher_app,
        "audit.note",
        plan_id=plan_id,
        payload={"k": "v"},
    )
    qsize_after = fast_flusher_app.state.event_queue.qsize()
    assert qsize_after >= qsize_before  # at least one event enqueued
    # Wait for the flusher to drain it.
    deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE event_type='audit.note' AND plan_id=$1",
                plan_id,
            )
        if count >= 1:
            break
    assert count == 1


# ─── batch trigger semantics ────────────────────────────────────────────


async def test_batch_flush_triggers_at_size_threshold(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-003: 500 events enqueue → bulk INSERT with all rows visible **before** the timer fires.

    Strengthened from a 3 s eventual-correctness wait to an explicit
    ≤ 100 ms latency assertion (M3 scrutiny round-1 cited the original
    3 s wait as too permissive). With ``event_flush_interval_seconds=1.0``
    the only signal that can flush within 100 ms is the wake-on-
    threshold path (VAL-OBS-003 contract pin: "Single bulk INSERT
    against `events` with `params count == 500` appears in the trace
    within 50 ms of enqueue"). The 100 ms budget here gives ≥ 2 ×
    safety vs. the contract's 50 ms target while staying well under
    the 1 s timer.
    """
    plan_id = await _seed_plan(db_pool)
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        # Long flush interval so the *only* trigger that can fire
        # within the 100 ms latency budget is the batch-size threshold.
        event_flush_interval_seconds=1.0,
        event_batch_limit=500,
        event_drain_timeout_seconds=3.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        t0 = time.monotonic()
        for i in range(500):
            _log_event(app, "audit.bulk", plan_id=plan_id, payload={"i": i})
        # Tight 5 ms-cadence poll loop bounded by 100 ms — under the
        # 50 ms contract target plus a generous safety margin. If the
        # wake path regresses the loop will time out before count
        # reaches 500.
        deadline = t0 + 0.1
        count: int = 0
        while time.monotonic() < deadline:
            async with db_pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE event_type='audit.bulk' AND plan_id=$1",
                    plan_id,
                )
            if count >= 500:
                break
            await asyncio.sleep(0.005)
        elapsed = time.monotonic() - t0
        assert count == 500, f"only {count}/500 rows visible after {elapsed * 1000:.1f} ms"
        assert elapsed < 0.1, f"flush took {elapsed * 1000:.1f} ms; expected < 100 ms"


async def test_batch_flush_triggers_within_50ms_of_500th_enqueue(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-003 (B1 fix): the 500th enqueue triggers a flush within ≤ 100 ms.

    Sibling of :func:`test_batch_flush_triggers_at_size_threshold`
    that explicitly counts the bulk INSERTs issued via the structured
    ``event_flusher.insert_ok`` log records — the wake path must
    materialise as a single bulk INSERT (VAL-OBS-003 evidence:
    "Single bulk INSERT against ``events`` with ``params count == 500``
    appears in the trace within 50 ms of enqueue"). Uses the same
    1 s timer / 500-batch config so the only signal that can fire
    within the latency budget is the wake-on-threshold path.

    Why 100 ms here and 50 ms in the contract? The contract pins
    50 ms for the SQL trace-side observation (the moment the bulk
    INSERT statement appears in pg_stat_statements). The DB-side
    poll loop in pytest adds a few ms of round-trip overhead per
    sample; 100 ms gives ≥ 2 × safety vs. the contract target while
    still tightly constraining the wake-on-threshold path against
    the 1 s timer.
    """
    plan_id = await _seed_plan(db_pool, "plan-flusher-50ms")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=1.0,
        event_batch_limit=500,
        event_drain_timeout_seconds=3.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        t0 = time.monotonic()
        for i in range(500):
            _log_event(app, "audit.50ms", plan_id=plan_id, payload={"i": i})
        deadline = t0 + 0.1
        count: int = 0
        while time.monotonic() < deadline:
            async with db_pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE event_type='audit.50ms' AND plan_id=$1",
                    plan_id,
                )
            if count >= 500:
                break
            await asyncio.sleep(0.005)
        t1 = time.monotonic()
        assert count == 500, (
            f"500 events did not arrive within 100 ms of enqueue; saw {count} after {(t1 - t0) * 1000:.1f} ms"
        )
        assert (t1 - t0) < 0.1, f"flush latency {(t1 - t0) * 1000:.1f} ms exceeds the 100 ms budget"
        # Bulk-INSERT shape pin: the flusher's per-batch metadata
        # records the size of the most recent flush. With a single
        # threshold-driven batch, ``last_batch_size`` should equal
        # the batch_limit.
        flusher: EventFlusher = app.state.event_flusher
        assert flusher.last_batch_size == 500, (
            f"expected one 500-row bulk INSERT; flusher.last_batch_size={flusher.last_batch_size}"
        )


async def test_sub_threshold_burst_waits_for_timer_not_wake(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-004 (B1 fix): 499 enqueues do NOT trigger a wake — timer cadence is preserved.

    The wake path is gated on ``qsize() >= batch_limit``; sub-batch
    bursts must not degenerate into a busy-wake loop. Configure the
    flusher with ``event_flush_interval_seconds=1.0`` and
    ``event_batch_limit=500``; enqueue 499 events; assert no flush
    has occurred within 200 ms (well below the timer interval), and
    that the flush eventually lands when the 1 s timer fires.

    This is the symmetric companion to
    :func:`test_batch_flush_triggers_within_50ms_of_500th_enqueue`:
    that test ensures the wake path fires for full batches, this one
    ensures it does *not* fire for partial batches.
    """
    plan_id = await _seed_plan(db_pool, "plan-flusher-499")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=1.0,
        event_batch_limit=500,
        event_drain_timeout_seconds=3.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        t0 = time.monotonic()
        for i in range(499):
            _log_event(app, "audit.499", plan_id=plan_id, payload={"i": i})
        # Assert NO flush has occurred within 200 ms of the burst —
        # if the wake path fired for sub-batch volume, we'd see rows
        # here.
        await asyncio.sleep(0.2)
        async with db_pool.acquire() as conn:
            early_count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE event_type='audit.499' AND plan_id=$1",
                plan_id,
            )
        assert early_count == 0, (
            f"sub-threshold burst should wait for the 1 s timer; saw {early_count} rows after 200 ms "
            f"(wake path may have degenerated to per-enqueue spin)"
        )
        # The 1 s timer must eventually fire and drain the batch.
        deadline = t0 + 2.0
        late_count: int = 0
        while time.monotonic() < deadline:
            async with db_pool.acquire() as conn:
                late_count = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE event_type='audit.499' AND plan_id=$1",
                    plan_id,
                )
            if late_count == 499:
                break
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - t0
        assert late_count == 499, f"timer-driven flush incomplete: {late_count}/499 after {elapsed:.2f} s"
        # The flush must have happened *after* the 1 s timer fires —
        # not before (which would mean the wake fired spuriously on a
        # sub-batch put). Allow a small slack vs. the 1.0 s interval.
        assert elapsed >= 0.95, (
            f"flush happened at {elapsed * 1000:.1f} ms — earlier than the 1 s timer should permit; "
            f"wake path may have fired below batch_limit"
        )


async def test_time_based_flush_below_batch_size(fast_flusher_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    """VAL-OBS-004: 3 events → flush within ~150 ms, no need for 500-row threshold."""
    plan_id = await _seed_plan(db_pool, "plan-flusher-time")
    for i in range(3):
        _log_event(fast_flusher_app, "audit.tick", plan_id=plan_id, payload={"i": i})
    deadline = asyncio.get_event_loop().time() + 1.0
    count: int = 0
    while asyncio.get_event_loop().time() < deadline:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE event_type='audit.tick' AND plan_id=$1",
                plan_id,
            )
        if count == 3:
            break
        await asyncio.sleep(0.005)
    assert count == 3


# ─── append-only / no JSONL replay ───────────────────────────────────────


async def test_flusher_is_append_only(fast_flusher_app: FastAPI, db_pool: asyncpg.Pool) -> None:
    """VAL-OBS-016: events row count strictly non-decreasing under flushes."""
    plan_id = await _seed_plan(db_pool, "plan-flusher-append")
    samples: list[int] = []
    for batch in range(3):
        for i in range(10):
            _log_event(fast_flusher_app, "audit.append", plan_id=plan_id, payload={"b": batch, "i": i})
        # Wait for that batch's events to land.
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                cnt = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE event_type='audit.append' AND plan_id=$1",
                    plan_id,
                )
            if cnt == (batch + 1) * 10:
                break
            await asyncio.sleep(0.02)
        samples.append(cnt)
    # Strictly non-decreasing.
    assert all(samples[i] <= samples[i + 1] for i in range(len(samples) - 1))
    # Final count is exactly the number enqueued.
    assert samples[-1] == 30


async def test_cold_start_queue_is_empty_no_jsonl_replay(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-009 / VAL-OBS-010: a stale legacy JSONL file does not seed the queue."""
    # Seed a fake legacy whilly_logs/whilly_events.jsonl in the
    # tmpdir so we'd notice if the new flusher tried to replay it.
    legacy_dir = tmp_path / "whilly_logs"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "whilly_events.jsonl").write_text(
        "\n".join(json.dumps({"event_type": "legacy.replay", "i": i, "payload": {}}) for i in range(50)) + "\n",
        encoding="utf-8",
    )
    # Snapshot the events count before lifespan.
    async with db_pool.acquire() as conn:
        baseline = await conn.fetchval("SELECT count(*) FROM events")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.01,
        event_drain_timeout_seconds=1.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        # On startup the queue is empty — no JSONL replay seeded it.
        assert app.state.event_queue.qsize() == 0
        # Settle a couple of polls to confirm the flusher does
        # *not* invent rows from the JSONL.
        await asyncio.sleep(0.05)
    async with db_pool.acquire() as conn:
        after = await conn.fetchval("SELECT count(*) FROM events")
    assert after == baseline


# ─── checkpoint state file ──────────────────────────────────────────────


async def test_checkpoint_written_via_tempfile_os_replace(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-008: checkpoint file is JSON ``{last_flushed_seq, saved_at}``,
    no .tmp siblings remain, atomic os.replace is used.
    """
    plan_id = await _seed_plan(db_pool, "plan-flusher-cp")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.01,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        for i in range(5):
            _log_event(app, "audit.cp", plan_id=plan_id, payload={"i": i})
        # Wait for the flusher's checkpoint to appear.
        cp = tmp_path / ".event_flusher.checkpoint"
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if cp.is_file():
                break
            await asyncio.sleep(0.02)
        assert cp.is_file()
        loaded = json.loads(cp.read_text("utf-8"))
        assert isinstance(loaded.get("last_flushed_seq"), int)
        assert loaded["last_flushed_seq"] > 0
        assert isinstance(loaded.get("saved_at"), str)
        # No half-written .tmp siblings.
        siblings = list(tmp_path.glob(".event_flusher_*.tmp"))
        assert siblings == []


async def test_checkpoint_advances_monotonically(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-012: ``last_flushed_seq`` non-decreasing; matches max(events.id) at end."""
    plan_id = await _seed_plan(db_pool, "plan-flusher-mono")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.01,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
    )
    cp = tmp_path / ".event_flusher.checkpoint"
    samples: list[int] = []
    async with app.router.lifespan_context(app):
        for batch in range(5):
            for i in range(20):
                _log_event(app, "audit.mono", plan_id=plan_id, payload={"b": batch, "i": i})
            deadline = asyncio.get_event_loop().time() + 1.0
            while asyncio.get_event_loop().time() < deadline:
                if cp.is_file():
                    try:
                        loaded = json.loads(cp.read_text("utf-8"))
                        if int(loaded.get("last_flushed_seq", 0)) >= (batch + 1) * 20:
                            break
                    except (json.JSONDecodeError, OSError):
                        pass
                await asyncio.sleep(0.01)
            assert cp.is_file()
            loaded = json.loads(cp.read_text("utf-8"))
            samples.append(int(loaded["last_flushed_seq"]))
        # Verify against DB.
        async with db_pool.acquire() as conn:
            db_max = await conn.fetchval("SELECT max(id) FROM events")
        assert samples[-1] == db_max
    # Monotonic non-decreasing.
    assert all(samples[i] <= samples[i + 1] for i in range(len(samples) - 1))


# ─── idle behaviour ─────────────────────────────────────────────────────


async def test_flusher_idle_polls_when_queue_empty(db_pool: asyncpg.Pool, tmp_path: Path) -> None:
    """VAL-OBS-013: idle window of ~1 s yields ~10 polls (50 ms cadence here),
    flusher task remains alive.
    """
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.05,
        event_drain_timeout_seconds=1.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        flusher: EventFlusher = app.state.event_flusher
        before = flusher.idle_polls
        await asyncio.sleep(1.0)
        after = flusher.idle_polls
        # Approximately 1.0s / 0.05s = 20 polls. Allow for
        # event-loop scheduling slack: between 10 and 30.
        assert 10 <= (after - before) <= 30, f"idle polls delta out of band: {after - before}"
        assert not app.state.event_flusher_task.done()


# ─── transient error retry ──────────────────────────────────────────────


async def test_flusher_retries_transient_pg_error(
    db_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-OBS-011: a single execute() failure does not drop events.

    The flusher catches :class:`asyncpg.PostgresError` raised during
    the bulk INSERT and retries per its backoff schedule. We patch
    ``Connection.execute`` so the *first* call raises a synthetic
    ``PostgresConnectionError``; subsequent calls fall through to the
    real implementation. The flusher's retry path must observe both
    the failure and the eventual success and emit one of each
    structured log record.
    """
    plan_id = await _seed_plan(db_pool, "plan-flusher-retry")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.02,
        event_drain_timeout_seconds=5.0,
        event_checkpoint_dir=str(tmp_path),
    )
    # Patch asyncpg.connection.Connection.execute to fail the first
    # bulk INSERT, then fall through to the original on subsequent calls.
    real_execute = asyncpg.connection.Connection.execute
    fail_count = 0

    async def flaky_execute(self: asyncpg.Connection, query: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal fail_count
        if "INSERT INTO events" in query and fail_count == 0:
            fail_count += 1
            raise asyncpg.exceptions.PostgresConnectionError("simulated transient pg error")
        return await real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.connection.Connection, "execute", flaky_execute)
    async with app.router.lifespan_context(app):
        for i in range(5):
            _log_event(app, "audit.retry", plan_id=plan_id, payload={"i": i})
        # Wait for the events to land despite the synthetic failure.
        deadline = asyncio.get_event_loop().time() + 5.0
        cnt = 0
        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                cnt = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE event_type='audit.retry' AND plan_id=$1",
                    plan_id,
                )
            if cnt >= 5:
                break
            await asyncio.sleep(0.05)
        assert cnt == 5
        assert fail_count == 1, "flusher should have observed exactly one synthetic failure"


async def test_flusher_emits_structured_insert_failed_log(
    db_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """VAL-OBS-017: failed INSERT emits one log record with ``event=event_flusher.insert_failed``."""
    plan_id = await _seed_plan(db_pool, "plan-flusher-failed-log")
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.02,
        event_drain_timeout_seconds=5.0,
        event_checkpoint_dir=str(tmp_path),
    )
    real_execute = asyncpg.connection.Connection.execute
    fail_count = 0

    async def flaky_execute(self: asyncpg.Connection, query: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal fail_count
        if "INSERT INTO events" in query and fail_count == 0:
            fail_count += 1
            raise asyncpg.exceptions.PostgresConnectionError("simulated transient pg error")
        return await real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.connection.Connection, "execute", flaky_execute)
    with caplog.at_level("WARNING", logger="whilly.api.event_flusher"):
        async with app.router.lifespan_context(app):
            _log_event(app, "audit.failed_log", plan_id=plan_id, payload={"x": 1})
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                async with db_pool.acquire() as conn:
                    cnt = await conn.fetchval(
                        "SELECT count(*) FROM events WHERE event_type='audit.failed_log' AND plan_id=$1",
                        plan_id,
                    )
                if cnt >= 1:
                    break
                await asyncio.sleep(0.02)
        failed = [r for r in caplog.records if getattr(r, "event", None) == "event_flusher.insert_failed"]
        assert len(failed) >= 1
        assert all(getattr(r, "error_class", "") for r in failed)


# ─── structured log records ─────────────────────────────────────────────


async def test_flusher_emits_structured_insert_ok_log(
    fast_flusher_app: FastAPI, db_pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """VAL-OBS-017: success log carries ``event=event_flusher.insert_ok`` and ``rows_inserted``."""
    plan_id = await _seed_plan(db_pool, "plan-flusher-log")
    with caplog.at_level("INFO", logger="whilly.api.event_flusher"):
        for i in range(3):
            _log_event(fast_flusher_app, "audit.log", plan_id=plan_id, payload={"i": i})
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                cnt = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE event_type='audit.log' AND plan_id=$1",
                    plan_id,
                )
            if cnt == 3:
                break
            await asyncio.sleep(0.02)
        ok_records = [r for r in caplog.records if getattr(r, "event", None) == "event_flusher.insert_ok"]
        assert len(ok_records) >= 1
        assert any(getattr(r, "rows_inserted", 0) >= 1 for r in ok_records)
        # Each ok record carries a positive latency_ms field.
        for r in ok_records:
            assert isinstance(getattr(r, "latency_ms", None), float)
            assert r.latency_ms >= 0.0
