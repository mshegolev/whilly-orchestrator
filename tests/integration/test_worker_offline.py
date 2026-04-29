"""Integration tests for the offline-worker sweep (TASK-025b, PRD FR-1.4 / NFR-1 / SC-2).

This module exercises the second coroutine the FastAPI lifespan supervises
under :class:`asyncio.TaskGroup`: the periodic sweep that flips workers
whose ``last_heartbeat`` predates a configurable threshold to ``offline``
and releases all their CLAIMED / IN_PROGRESS work back to ``PENDING``.

What the offline-worker sweep adds on top of the visibility-timeout sweep
-------------------------------------------------------------------------
The visibility-timeout sweep (TASK-025a) reclaims tasks once their
``claimed_at`` ages past ~15 minutes. That window is a deliberate safety
margin — long enough that a slow agent on a healthy worker doesn't lose
its claim under event-loop pressure. But it's a *coarse* signal: a hard-
killed worker pins its claim for the full 15 minutes before the sweep
notices, which is unacceptable for the SC-2 "kill -9 a worker, peer
takes over within seconds" target.

The offline-worker sweep is the primary fault-tolerance signal: a worker
that stops heartbeating for ~2 minutes is presumed dead, flipped to
``offline``, and its in-flight work is released *with the same atomic
audit-event guarantee* as the visibility-timeout sweep — but 7.5x
faster. The visibility timeout remains the slower fallback for cases
where a heartbeat is alive but a task is somehow stuck.

What's covered
--------------
* A worker with a stale ``last_heartbeat`` (older than the threshold) is
  flipped to ``status = 'offline'`` by the next sweep tick. All its
  CLAIMED / IN_PROGRESS tasks come back to ``PENDING``, and a
  ``RELEASE`` event lands per task with
  ``payload['reason'] == 'worker_offline'`` and the post-update version.
* A fresh-heartbeat worker is *not* touched. The sweep filters on the
  staleness threshold, so a healthy heartbeat (any age younger than the
  cutoff) keeps the worker online and its tasks claimed.
* A worker already flipped to ``offline`` does not generate a *second*
  batch of RELEASE events. Re-running the sweep against an already-
  offline worker is a no-op — important because otherwise a long-dead
  worker would burn through ``events`` rows on every tick.
* A heartbeat from a previously-offline worker resurrects it: the
  ``status`` returns to ``'online'``, so the sweep stops releasing
  tasks claimed by that worker after recovery. Pins the
  cluster-restart contract (a worker process re-using the same
  ``worker_id`` after a brief outage rejoins cleanly).
* ``create_app`` rejects a non-positive
  ``offline_worker_sweep_interval_seconds`` and a negative
  ``heartbeat_timeout_seconds`` at construction time.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import (
    HEARTBEAT_TIMEOUT_DEFAULT_SECONDS,
    OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS,
    create_app,
)

pytestmark = DOCKER_REQUIRED

_BOOTSTRAP_TOKEN = "bootstrap-tok-off"
_WORKER_TOKEN = "worker-tok-off"

# Aggressive timing knobs — the offline-worker sweep is the *fast* sweep
# (PRD reference 30s cadence / 2 min threshold), but the test suite needs
# both sub-second. ``_SWEEP_INTERVAL`` × 5 ticks fits inside
# ``_WAIT_FOR_SWEEP`` so a flaky scheduler that overshoots a single tick
# still surfaces the release in time.
_SWEEP_INTERVAL = 0.1
_HEARTBEAT_TIMEOUT = 1
_WAIT_FOR_SWEEP = 0.5
# A long visibility timeout disables the *other* sweep for our tests:
# we want to assert that it is the offline-worker sweep (not the
# visibility-timeout sweep) that releases the tasks. 600s is well past
# any realistic test runtime.
_DISABLED_VISIBILITY_TIMEOUT = 600


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> None:
    """Insert a plans row idempotently (same idiom as test_visibility_timeout)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            plan_id,
            f"plan-{plan_id}",
        )


async def _seed_worker(
    pool: asyncpg.Pool,
    worker_id: str,
    *,
    heartbeat_age_seconds: float,
    status: str = "online",
) -> None:
    """Insert a workers row with ``last_heartbeat = NOW() - heartbeat_age_seconds``.

    Aging the heartbeat through the same Postgres clock the sweep reads
    from means a clock-skew between the test driver and the database
    cannot mask a real bug. ``status`` is configurable so we can set up
    the "already offline" idempotency test directly.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, token_hash, last_heartbeat, status)
            VALUES ($1, $2, $3, NOW() - make_interval(secs => $4::float8), $5)
            ON CONFLICT (worker_id) DO NOTHING
            """,
            worker_id,
            f"host-{worker_id}",
            f"hash-{worker_id}",
            heartbeat_age_seconds,
            status,
        )


async def _seed_claimed_task(
    pool: asyncpg.Pool,
    *,
    plan_id: str,
    task_id: str,
    worker_id: str,
) -> None:
    """Insert a CLAIMED task owned by ``worker_id`` with a *fresh* claim.

    The ``claimed_at = NOW()`` keeps the visibility-timeout sweep from
    accidentally releasing the row even if its threshold is somehow
    breached during the test — the offline-worker sweep should be the
    sole writer.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (id, plan_id, status, priority, claimed_by, claimed_at)
            VALUES ($1, $2, 'CLAIMED', 'medium', $3, NOW())
            """,
            task_id,
            plan_id,
            worker_id,
        )


@pytest.fixture
async def lifespan_app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """Build a FastAPI app with aggressive offline-sweep timings and run its lifespan.

    Visibility timeout is parked at 600s so the offline-worker sweep is
    unambiguously the writer that flips any task back to ``PENDING``
    during the test. Tokens are passed explicitly to avoid env races.
    """
    app: FastAPI = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        sweep_interval_seconds=_SWEEP_INTERVAL,
        visibility_timeout_seconds=_DISABLED_VISIBILITY_TIMEOUT,
        offline_worker_sweep_interval_seconds=_SWEEP_INTERVAL,
        heartbeat_timeout_seconds=_HEARTBEAT_TIMEOUT,
    )
    async with app.router.lifespan_context(app):
        yield app


# ---------------------------------------------------------------------------
# Happy path — stale heartbeat → worker offline + tasks released.
# ---------------------------------------------------------------------------


async def test_sweep_flips_offline_and_releases_tasks(
    lifespan_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """A worker with a stale heartbeat is flipped offline; its tasks come back to PENDING.

    Pins the full FR-1.4 / NFR-1 / SC-2 contract:

    * ``workers.status`` flips ``online`` → ``offline``;
    * ``tasks.status`` flips ``CLAIMED`` → ``PENDING``;
    * ``tasks.claimed_by`` / ``claimed_at`` are cleared;
    * ``tasks.version`` advances by 1;
    * a single ``RELEASE`` event lands per task with the post-update
      version, ``reason='worker_offline'``, and the ``worker_id`` of
      the offline worker.

    All assertions hit the same mid-loop database state — the lifespan
    fixture keeps the sweep actively ticking, so a regression that
    flipped status without writing the event row (or vice versa) would
    surface here, not in a separate phase.
    """
    plan_id = "PLAN-OFF-STALE"
    task_id = "T-off-stale"
    worker_id = "w-off-stale"
    await _seed_plan(db_pool, plan_id)
    await _seed_worker(db_pool, worker_id, heartbeat_age_seconds=5.0)
    await _seed_claimed_task(db_pool, plan_id=plan_id, task_id=task_id, worker_id=worker_id)

    await asyncio.sleep(_WAIT_FOR_SWEEP)

    async with db_pool.acquire() as conn:
        worker_status = await conn.fetchval(
            "SELECT status FROM workers WHERE worker_id = $1",
            worker_id,
        )
        task_row = await conn.fetchrow(
            "SELECT status, claimed_by, claimed_at, version FROM tasks WHERE id = $1",
            task_id,
        )
        event_rows = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id ASC",
            task_id,
        )

    assert worker_status == "offline", f"expected worker offline, got {worker_status!r}"
    assert task_row is not None, f"task {task_id} disappeared"
    assert task_row["status"] == "PENDING", f"expected PENDING, got {task_row['status']}"
    assert task_row["claimed_by"] is None
    assert task_row["claimed_at"] is None
    assert task_row["version"] == 1

    release_events = [row for row in event_rows if row["event_type"] == "RELEASE"]
    assert len(release_events) == 1, f"expected 1 RELEASE event, got {len(release_events)}: {event_rows}"
    payload = json.loads(release_events[0]["payload"])
    assert payload["reason"] == "worker_offline"
    assert payload["version"] == 1
    assert payload["worker_id"] == worker_id


# ---------------------------------------------------------------------------
# Negative path — a fresh heartbeat is NOT touched.
# ---------------------------------------------------------------------------


async def test_sweep_skips_fresh_heartbeat(
    lifespan_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """A worker heartbeating within the threshold rides through every sweep untouched.

    ``heartbeat_age_seconds=0`` means the seeded worker's last_heartbeat
    is "right now" — far younger than the 1s threshold even after the
    test waits ``_WAIT_FOR_SWEEP``. The worker should stay online, the
    task should stay CLAIMED, and no RELEASE events should land.

    Pinned because the sweep is the only mechanism that mutates worker
    rows asynchronously to the heartbeat path — a regression that
    accidentally released *all* online workers (or filtered on the
    wrong side of the inequality) would surface as a status flip we
    then assert against.
    """
    plan_id = "PLAN-OFF-FRESH"
    task_id = "T-off-fresh"
    worker_id = "w-off-fresh"
    await _seed_plan(db_pool, plan_id)
    await _seed_worker(db_pool, worker_id, heartbeat_age_seconds=0.0)
    await _seed_claimed_task(db_pool, plan_id=plan_id, task_id=task_id, worker_id=worker_id)

    await asyncio.sleep(_WAIT_FOR_SWEEP)

    async with db_pool.acquire() as conn:
        worker_status = await conn.fetchval(
            "SELECT status FROM workers WHERE worker_id = $1",
            worker_id,
        )
        task_row = await conn.fetchrow(
            "SELECT status, claimed_by FROM tasks WHERE id = $1",
            task_id,
        )
        release_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'RELEASE'",
            task_id,
        )

    assert worker_status == "online", f"fresh worker was unexpectedly flipped offline: {worker_status!r}"
    assert task_row is not None
    assert task_row["status"] == "CLAIMED"
    assert task_row["claimed_by"] == worker_id
    assert release_count == 0


# ---------------------------------------------------------------------------
# Idempotency — already-offline worker is not re-released.
# ---------------------------------------------------------------------------


async def test_sweep_is_idempotent_on_offline_workers(
    lifespan_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """Re-running the sweep against an already-offline worker writes no new events.

    Important because every healthy cluster will eventually accumulate
    long-dead workers (decommissioned hosts, replaced nodes). If each
    sweep tick re-released their (now-PENDING-or-reclaimed) tasks, the
    ``events`` table would balloon by O(num_dead_workers × ticks) for
    no audit value. We seed a worker as ``offline`` directly and assert
    that no RELEASE events land for any of its (already-released) tasks
    over the lifespan window.

    The seeded task is PENDING from the start (mimicking the post-
    release state) so a regression that re-claimed and re-released it
    would still surface — there'd be a CLAIM event followed by a
    RELEASE that we'd catch in the assertion.
    """
    plan_id = "PLAN-OFF-IDEM"
    task_id = "T-off-idem"
    worker_id = "w-off-idem"
    await _seed_plan(db_pool, plan_id)
    await _seed_worker(
        db_pool,
        worker_id,
        heartbeat_age_seconds=10.0,
        status="offline",
    )
    # Task is already PENDING (not claimed by the offline worker)
    # — mimics the post-release steady state the sweep should not
    # disturb.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (id, plan_id, status, priority)
            VALUES ($1, $2, 'PENDING', 'medium')
            """,
            task_id,
            plan_id,
        )

    await asyncio.sleep(_WAIT_FOR_SWEEP)

    async with db_pool.acquire() as conn:
        worker_status = await conn.fetchval(
            "SELECT status FROM workers WHERE worker_id = $1",
            worker_id,
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1",
            task_id,
        )

    assert worker_status == "offline", f"already-offline worker should stay offline: {worker_status!r}"
    assert event_count == 0, f"expected no events for an idempotent re-sweep, got {event_count}"


# ---------------------------------------------------------------------------
# Recovery — heartbeat resurrects a previously-offline worker.
# ---------------------------------------------------------------------------


async def test_heartbeat_resurrects_offline_worker(
    db_pool: asyncpg.Pool,
) -> None:
    """A heartbeat from an offline worker flips its status back to ``online``.

    Pins the cluster-restart contract: a worker process that briefly
    crashes (and gets flagged offline by the sweep) but comes back
    re-using the same ``worker_id`` rejoins the cluster transparently.
    The ``update_heartbeat`` SQL sets both ``last_heartbeat = NOW()``
    AND ``status = 'online'`` so the next offline-worker sweep does
    not re-release work the resurrected worker has just re-claimed.

    No lifespan needed — this exercises the heartbeat-side primitive,
    not the sweep loop. We seed the worker as offline, send one
    heartbeat through the repository, and read the workers row back.
    """
    from whilly.adapters.db import TaskRepository

    worker_id = "w-off-resurrect"
    await _seed_worker(
        db_pool,
        worker_id,
        heartbeat_age_seconds=10.0,
        status="offline",
    )

    repo = TaskRepository(db_pool)
    ok = await repo.update_heartbeat(worker_id)
    assert ok is True

    async with db_pool.acquire() as conn:
        worker_row = await conn.fetchrow(
            "SELECT status, last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )

    assert worker_row is not None
    assert worker_row["status"] == "online", f"heartbeat should resurrect to online, got {worker_row['status']!r}"


# ---------------------------------------------------------------------------
# Construction-time validation (no DB needed).
# ---------------------------------------------------------------------------


def test_create_app_rejects_zero_offline_sweep_interval(db_pool: asyncpg.Pool) -> None:
    """``offline_worker_sweep_interval_seconds <= 0`` raises :class:`ValueError`.

    Same rationale as the visibility-sweep guard: catching at
    construction (loud) beats spinning a tight loop in production
    (silent and disastrous). A future operator-facing CLI can map the
    ValueError to a clean exit code.
    """
    with pytest.raises(ValueError, match="offline_worker_sweep_interval_seconds"):
        create_app(
            db_pool,
            worker_token=_WORKER_TOKEN,
            bootstrap_token=_BOOTSTRAP_TOKEN,
            offline_worker_sweep_interval_seconds=0,
        )


def test_create_app_rejects_negative_heartbeat_timeout(db_pool: asyncpg.Pool) -> None:
    """``heartbeat_timeout_seconds < 0`` raises :class:`ValueError`.

    ``0`` is allowed (every online worker flipped on every tick — only
    useful in tests with controlled clocks); only genuinely negative
    values are rejected.
    """
    with pytest.raises(ValueError, match="heartbeat_timeout_seconds"):
        create_app(
            db_pool,
            worker_token=_WORKER_TOKEN,
            bootstrap_token=_BOOTSTRAP_TOKEN,
            heartbeat_timeout_seconds=-1,
        )


def test_create_app_default_offline_constants_match_prd(db_pool: asyncpg.Pool) -> None:
    """The exported offline-sweep constants stay aligned with the PRD reference values.

    PRD FR-1.4 / NFR-1 name "2 min heartbeat threshold" and the design
    note "30s sweep cadence — half the heartbeat interval" explicitly.
    A drift in either default would silently change the fault-tolerance
    window for every production deployment that doesn't pass the kwargs
    explicitly.
    """
    assert HEARTBEAT_TIMEOUT_DEFAULT_SECONDS == 2 * 60
    assert OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS == 30.0
