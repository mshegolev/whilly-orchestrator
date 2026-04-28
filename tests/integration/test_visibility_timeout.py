"""Integration tests for the periodic visibility-timeout sweep (TASK-025a, PRD FR-1.4 / NFR-1 / SC-2).

This module exercises the FastAPI lifespan's :class:`asyncio.TaskGroup`-
managed background task. The sweep is the *only* mechanism that recovers
claims orphaned by a hard-killed worker (PRD SC-2) — losing it silently
would leave PENDING work pinned forever, defeating the whole fault-
tolerance story. So we treat its behaviour as a wire contract worth
real integration coverage rather than mocking the loop.

What's covered
--------------
* Stale ``CLAIMED`` rows (``claimed_at`` aged past
  ``visibility_timeout_seconds``) are flipped back to ``PENDING`` by the
  next sweep tick. ``claimed_by`` and ``claimed_at`` are cleared,
  ``version`` advances by 1, and a ``RELEASE`` event row lands in
  ``events`` carrying ``payload['reason'] = 'visibility_timeout'`` —
  the same key the dashboard (TASK-027) and post-mortems read.
* Fresh claims are *not* released — the sweep filters on
  ``claimed_at < NOW() - timeout``, so a claim younger than the timeout
  budget rides through every tick untouched. This pins the contract
  that a healthy long-running task isn't spuriously cancelled while its
  worker is still alive and heartbeating.
* Lifespan shutdown drains the sweep cleanly: the ``sweep_stop`` event
  on ``app.state`` is set on exit and the TaskGroup unwinds without
  raising — no ``CancelledError`` plumbing, no ``BaseExceptionGroup``,
  no leaked task warnings. The shutdown happy path is the one TASK-026
  (worker-kill recovery) will piggyback on, so it has to be solid here
  first.

Why integration, not unit
-------------------------
The contract under test is "a periodic loop runs against a live database
and produces atomic SQL transitions + audit-event rows". Mocking the
repository or replacing :class:`asyncio.TaskGroup` would assert on the
*shape* of the calls, not on the resulting database state — and the
whole point of TASK-025a is that the database state ends up consistent
even when the sweep races against worker crashes / completions. Real
Postgres is the only way to verify that.

Sweep tuning for tests
----------------------
Production defaults are ``sweep_interval=60s`` and
``visibility_timeout=15min`` (PRD reference values). Both would
dominate the suite if used as-is; we override via ``create_app``
kwargs:

* ``sweep_interval_seconds=0.1`` — 10 sweep ticks per second.
* ``visibility_timeout_seconds=1`` — claim ages out after 1 second.

Combined, the "stale claim is released" assertion lands within ~0.2s
of the lifespan starting, well under any reasonable per-test budget.
The unit tests in :mod:`tests.unit.test_transport_server` (a future
TASK-025a follow-up may add them) cover the construction-time
validation of these knobs; this suite is the load-bearing end-to-end
proof.
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
    SWEEP_INTERVAL_DEFAULT_SECONDS,
    VISIBILITY_TIMEOUT_DEFAULT_SECONDS,
    create_app,
)

pytestmark = DOCKER_REQUIRED

_BOOTSTRAP_TOKEN = "bootstrap-tok-vt"
_WORKER_TOKEN = "worker-tok-vt"

# Aggressive timing knobs — keep the suite fast while still exercising
# the real sweep loop end-to-end. ``_SWEEP_INTERVAL`` × at least 3 ticks
# fits comfortably within ``_WAIT_FOR_SWEEP``, so a flaky scheduler that
# occasionally overshoots a single tick still surfaces the release
# rather than failing on timing variance.
_SWEEP_INTERVAL = 0.1
_VISIBILITY_TIMEOUT = 1
_WAIT_FOR_SWEEP = 0.5


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> None:
    """Insert ``(plan_id, "plan-{plan_id}")`` if not already present.

    ``ON CONFLICT DO NOTHING`` so multiple tests sharing a fixture-
    scoped pool can co-exist without a second seed colliding on the PK.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            plan_id,
            f"plan-{plan_id}",
        )


async def _seed_worker(pool: asyncpg.Pool, worker_id: str) -> None:
    """Insert a workers row directly so the FK on ``tasks.claimed_by`` is satisfied.

    Tests here don't go through ``POST /workers/register`` because they
    never need the bearer token round-trip — the sweep is a pure
    background-task / DB contract. Skipping the HTTP register path
    keeps each test in the sub-second budget.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3) "
            "ON CONFLICT (worker_id) DO NOTHING",
            worker_id,
            f"host-{worker_id}",
            f"hash-{worker_id}",
        )


async def _seed_claimed_task(
    pool: asyncpg.Pool,
    *,
    plan_id: str,
    task_id: str,
    worker_id: str,
    claimed_age_seconds: float,
) -> None:
    """Insert a ``CLAIMED`` task with ``claimed_at = NOW() - claimed_age_seconds``.

    ``make_interval(secs => $4)`` is the same primitive
    :data:`whilly.adapters.db.repository._RELEASE_STALE_SQL` uses; we
    age the row through the same Postgres clock the sweep reads from,
    so test timing failures can't mask a real bug behind a clock skew
    between the test driver and the database.

    The status is hard-coded to ``CLAIMED`` here (rather than parametrising
    on ``IN_PROGRESS``) — the sweep filter accepts both source states and
    the SQL is identical, so testing one is sufficient. ``priority``
    defaults to ``medium`` because it's irrelevant for the sweep
    (priority enters via :meth:`TaskRepository.claim_task`'s ORDER BY,
    not the visibility-timeout filter).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO tasks (id, plan_id, status, priority, claimed_by, claimed_at)
                VALUES ($1, $2, 'CLAIMED', 'medium', $3, NOW() - make_interval(secs => $4::float8))
                """,
                task_id,
                plan_id,
                worker_id,
                claimed_age_seconds,
            )


@pytest.fixture
async def lifespan_app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """Build a FastAPI app with aggressive sweep timings and run its lifespan.

    Per-test ``db_pool`` already truncates ``events`` / ``tasks`` /
    ``plans`` / ``workers`` (see :mod:`tests.conftest`) so each test
    starts from a clean slate. The fixture yields the app *inside* its
    lifespan context, so the sweep loop is actively running for the
    duration of the test body — assertions run mid-loop, not on a dead
    app.

    Tokens are passed explicitly to avoid mutating ``os.environ`` (which
    would race with parallel test workers). The transport / claim /
    register surface isn't exercised here, but ``create_app`` requires
    valid tokens to construct the auth dependencies.
    """
    app: FastAPI = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        sweep_interval_seconds=_SWEEP_INTERVAL,
        visibility_timeout_seconds=_VISIBILITY_TIMEOUT,
    )
    async with app.router.lifespan_context(app):
        yield app


# ---------------------------------------------------------------------------
# Happy path — the sweep releases a stale CLAIMED row.
# ---------------------------------------------------------------------------


async def test_sweep_releases_stale_claim(
    lifespan_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """A ``CLAIMED`` row aged past ``visibility_timeout`` is flipped back to ``PENDING``.

    Seeds a claim 5 seconds old against a 1-second timeout, runs the
    lifespan with a 100ms sweep cadence, waits 500ms, and asserts:

    * ``tasks.status`` flipped to ``PENDING``
    * ``tasks.claimed_by`` / ``claimed_at`` cleared
    * ``tasks.version`` advanced by 1
    * a ``RELEASE`` event row exists with
      ``payload['reason'] == 'visibility_timeout'`` and the post-update
      version

    This pins the full FR-1.4 contract: the SQL transition AND the
    audit row land atomically. A regression that flipped status without
    writing the event would let the dashboard (TASK-027) silently lose
    the "task bounced because the worker died" signal.
    """
    plan_id = "PLAN-VT-STALE"
    task_id = "T-vt-stale"
    worker_id = "w-vt-stale"
    await _seed_plan(db_pool, plan_id)
    await _seed_worker(db_pool, worker_id)
    await _seed_claimed_task(
        db_pool,
        plan_id=plan_id,
        task_id=task_id,
        worker_id=worker_id,
        claimed_age_seconds=5.0,  # well past the 1s timeout
    )

    # Wait for at least one sweep tick to land. ``_WAIT_FOR_SWEEP``
    # gives ~5x the interval as headroom against scheduler jitter.
    await asyncio.sleep(_WAIT_FOR_SWEEP)

    async with db_pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT status, claimed_by, claimed_at, version FROM tasks WHERE id = $1",
            task_id,
        )
        event_rows = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id ASC",
            task_id,
        )

    assert task_row is not None, f"task {task_id} disappeared"
    assert task_row["status"] == "PENDING", f"expected PENDING, got {task_row['status']}"
    assert task_row["claimed_by"] is None
    assert task_row["claimed_at"] is None
    # Seeded version was 0 (default), the sweep increments to 1.
    assert task_row["version"] == 1, f"expected version 1, got {task_row['version']}"

    # Exactly one RELEASE event with the visibility_timeout reason.
    release_events = [row for row in event_rows if row["event_type"] == "RELEASE"]
    assert len(release_events) == 1, f"expected 1 RELEASE event, got {len(release_events)}: {event_rows}"
    # asyncpg returns JSONB as raw JSON text; decode to assert structure.
    payload = json.loads(release_events[0]["payload"])
    assert payload["reason"] == "visibility_timeout"
    assert payload["version"] == 1


# ---------------------------------------------------------------------------
# Negative path — fresh claims are NOT touched.
# ---------------------------------------------------------------------------


async def test_sweep_skips_fresh_claim(
    db_pool: asyncpg.Pool,
) -> None:
    """A claim younger than ``visibility_timeout`` rides through every sweep untouched.

    Uses a *long* visibility timeout (60s) so the seeded 0-second-old
    claim is provably not eligible for release at any point during the
    test window. We still drive the lifespan with a fast sweep cadence
    so multiple ticks fire — a regression that filtered on the wrong
    side of the inequality (or accidentally released *all* CLAIMED
    rows) would surface as a status flip we then assert against.

    Pinned because the sweep is the only mechanism that touches
    CLAIMED rows asynchronously to the worker — if it spuriously
    released live work, every healthy long-running agent would hit a
    409 ``version_conflict`` on its eventual ``complete_task`` call.
    """
    plan_id = "PLAN-VT-FRESH"
    task_id = "T-vt-fresh"
    worker_id = "w-vt-fresh"
    await _seed_plan(db_pool, plan_id)
    await _seed_worker(db_pool, worker_id)
    await _seed_claimed_task(
        db_pool,
        plan_id=plan_id,
        task_id=task_id,
        worker_id=worker_id,
        claimed_age_seconds=0.0,  # just claimed
    )

    # Build a separate app with a long timeout — we don't reuse the
    # ``lifespan_app`` fixture because it pins ``visibility_timeout=1``.
    app = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        sweep_interval_seconds=_SWEEP_INTERVAL,
        visibility_timeout_seconds=60,  # claim won't age out within the test
    )
    async with app.router.lifespan_context(app):
        # Multiple sweep ticks fire; none should release the fresh claim.
        await asyncio.sleep(_WAIT_FOR_SWEEP)

    async with db_pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT status, claimed_by, version FROM tasks WHERE id = $1",
            task_id,
        )
        release_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND event_type = 'RELEASE'",
            task_id,
        )

    assert task_row is not None
    assert task_row["status"] == "CLAIMED", f"fresh claim was unexpectedly released: status={task_row['status']}"
    assert task_row["claimed_by"] == worker_id
    assert task_row["version"] == 0, f"version should not have changed; got {task_row['version']}"
    assert release_count == 0, f"expected no RELEASE events, got {release_count}"


# ---------------------------------------------------------------------------
# Shutdown — lifespan exit drains the sweep cleanly.
# ---------------------------------------------------------------------------


async def test_lifespan_shutdown_stops_sweep_cleanly(
    db_pool: asyncpg.Pool,
) -> None:
    """Lifespan teardown sets ``sweep_stop`` and unwinds the TaskGroup without raising.

    The shutdown contract matters because:

    * The lifespan ``async with TaskGroup`` would re-raise as a
      :class:`BaseExceptionGroup` if the sweep coroutine surfaced any
      exception — including a stray :class:`asyncio.CancelledError`
      produced by sloppy cancellation.
    * TASK-026 (worker-kill recovery test) and the demo script
      (TASK-024b) rely on starting / stopping the control plane many
      times in a single test process; a leaky sweep would surface as
      asyncio "Task was destroyed but it is pending" warnings or, worse,
      a hang on the second startup as the previous loop's connection
      sat unfinished.

    We exit the lifespan and assert ``app.state.sweep_stop`` is set —
    the stop event is also cleared on the way out (``app.state.sweep_stop
    = None``), so we capture a reference *before* exit and check it
    after.
    """
    app = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        sweep_interval_seconds=_SWEEP_INTERVAL,
        visibility_timeout_seconds=_VISIBILITY_TIMEOUT,
    )
    async with app.router.lifespan_context(app):
        # Capture the sweep-stop event *before* exit; the lifespan
        # zeroes ``app.state.sweep_stop`` on teardown so we can't read
        # it after the ``async with`` block.
        sweep_stop = app.state.sweep_stop
        assert isinstance(sweep_stop, asyncio.Event)
        assert not sweep_stop.is_set(), "sweep_stop should be clear while the lifespan is active"

    # Lifespan exited without re-raising any exception — ``async with``
    # would have surfaced it. The stop event is now set (the teardown
    # set it before awaiting the TaskGroup) and the lifespan dropped
    # its reference.
    assert sweep_stop.is_set(), "lifespan teardown should set sweep_stop"
    assert app.state.sweep_stop is None, "lifespan teardown should clear app.state.sweep_stop"


# ---------------------------------------------------------------------------
# Construction-time validation of the new kwargs (no DB needed).
# ---------------------------------------------------------------------------


def test_create_app_rejects_zero_sweep_interval(db_pool: asyncpg.Pool) -> None:
    """``sweep_interval_seconds <= 0`` must raise :class:`ValueError` at construction time.

    Caught at construction (loud) rather than spinning a tight loop in
    production (silent and disastrous). Also pins the error to
    :class:`ValueError` so a future operator-facing CLI surface can
    map it to a clean exit code rather than a 500.
    """
    with pytest.raises(ValueError, match="sweep_interval_seconds"):
        create_app(
            db_pool,
            worker_token=_WORKER_TOKEN,
            bootstrap_token=_BOOTSTRAP_TOKEN,
            sweep_interval_seconds=0,
        )


def test_create_app_rejects_negative_visibility_timeout(db_pool: asyncpg.Pool) -> None:
    """``visibility_timeout_seconds < 0`` must raise :class:`ValueError`.

    ``0`` is *allowed* (releases every active claim — useful in tests
    with controlled clocks); only genuinely negative values are
    rejected. Pins the asymmetry against future "tighten the bound"
    refactors that would break test fixtures.
    """
    with pytest.raises(ValueError, match="visibility_timeout_seconds"):
        create_app(
            db_pool,
            worker_token=_WORKER_TOKEN,
            bootstrap_token=_BOOTSTRAP_TOKEN,
            visibility_timeout_seconds=-1,
        )


def test_create_app_default_constants_match_prd(db_pool: asyncpg.Pool) -> None:
    """The exported constants stay aligned with the PRD reference values.

    PRD FR-1.4 names "60s sweep cadence" and "15min visibility timeout"
    explicitly. A drift in either default would silently change the
    fault-tolerance window for every production deployment that doesn't
    pass the kwargs explicitly — pin them here so a future bump has to
    update the test alongside the constant.
    """
    assert SWEEP_INTERVAL_DEFAULT_SECONDS == 60.0
    assert VISIBILITY_TIMEOUT_DEFAULT_SECONDS == 15 * 60
