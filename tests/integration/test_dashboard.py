"""Integration tests for ``whilly dashboard`` (TASK-027, AC: "Reads from DB only").

What we cover
-------------
* :func:`fetch_dashboard_rows` reads the canonical projection (id,
  status, priority, claimed_by, claimed_at, updated_at) from a real
  Postgres seeded via the ``db_pool`` testcontainer fixture.
* The polling loop driven through the public CLI surface
  (:func:`run_dashboard_command`) exits cleanly when ``--max-iterations``
  is reached, against a populated plan.
* A missing ``plan_id`` surfaces as exit ``2`` with a ``not found``
  diagnostic — same shape as ``whilly plan show`` so operators see the
  same message regardless of which read view they hit first.

What we deliberately *don't* cover here
---------------------------------------
* Hotkey behaviour and pure rendering live in
  :mod:`tests.unit.test_dashboard` — they are pure-function properties
  and re-pinning them here would force a Docker reboot for every layout
  tweak.
* Live-render screen ownership / terminal raw mode — the integration
  test runs in pytest's non-TTY environment, where Rich's Live falls
  back to a non-interactive renderer. We assert the loop *terminates*
  cleanly; the visual layout is the unit test's job.

Why ``--max-iterations 1`` is the right cap
-------------------------------------------
The polling loop's contract is "fetch every interval until ``stop`` or
``max_iterations``". One iteration is enough to prove the SQL fires,
the renderer assembles a table, and the loop unwinds — anything more
would be testing :class:`asyncio.sleep`, not our code. The interval is
set very small so the test doesn't pay a real second per iteration on
the off chance a future regression bumps the loop above one tick.

Why a fresh ``asyncpg.connect`` for seeding rather than the ``db_pool``?
------------------------------------------------------------------------
Same reasoning as :mod:`tests.integration.test_plan_show`: the pool
fixture is bound to pytest-asyncio's per-test event loop, and calling
:func:`run_dashboard_command` (which uses :func:`asyncio.run`) opens
its own loop. Sharing the same pool across two loops surfaces an
``InterfaceError: another operation in progress``. Opening a transient
connection scoped to *our* :func:`asyncio.run` call mirrors what the
production CLI does (one-shot connect / disconnect per command).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.cli.dashboard import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    DashboardRow,
    fetch_dashboard_rows,
    run_dashboard_command,
)
from whilly.core.models import Priority, TaskStatus

# Module-level skip — every test in this file boots a Postgres container via
# the session-scoped ``postgres_dsn`` fixture, so a Docker-less CI runner
# should skip collection rather than fail per-test.
pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    Same shape as the fixture in :mod:`tests.integration.test_plan_show`.
    The handler reads the DSN from the environment because
    :func:`run_dashboard_command` is a one-shot CLI; threading the DSN
    through every call site would explode the public surface.
    """
    prior = os.environ.get(DATABASE_URL_ENV)
    os.environ[DATABASE_URL_ENV] = postgres_dsn
    try:
        yield postgres_dsn
    finally:
        if prior is None:
            os.environ.pop(DATABASE_URL_ENV, None)
        else:
            os.environ[DATABASE_URL_ENV] = prior


# Worker_id used to back the ``claimed_by`` FK on tasks below. The schema
# requires a row in ``workers`` before a task can be CLAIMED, so each test
# inserts one synthetic worker.
_TEST_WORKER_ID = "dashboard-test-worker"


async def _seed_three_tasks(dsn: str, plan_id: str) -> None:
    """Insert one plan, one worker, and three tasks at three statuses.

    Bypasses ``whilly plan import`` deliberately:

    * The import path lives downstream — its tests are in
      ``tests/integration/test_plan_io.py`` — and pulling it in here
      would couple this test to that pipeline's contract.
    * We need to set ``status``, ``claimed_by`` and ``claimed_at`` to
      cover the dashboard's full column space. Import only sets
      ``status=PENDING`` and leaves the claim fields NULL (which is the
      production-correct shape for a freshly-imported plan but useless
      for asserting "Claimed by" rendering).

    Inserts:

    * ``T-DONE`` — DONE, claimed by ``_TEST_WORKER_ID``, claimed_at set.
    * ``T-RUN`` — IN_PROGRESS, claimed by ``_TEST_WORKER_ID``, claimed_at set.
    * ``T-IDLE`` — PENDING, no claim.
    """
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2)",
                plan_id,
                f"Dashboard Test {plan_id}",
            )
            await conn.execute(
                "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
                _TEST_WORKER_ID,
                "test-host",
                "test-token-hash",
            )
            # The schema's ck_tasks_claim_pair_consistent CHECK requires
            # ``claimed_by`` and ``claimed_at`` to be both NULL or both
            # set on the same row. We insert the owned rows with
            # ``claimed_at = NOW() - <offset>`` directly so the row is
            # constraint-clean from the moment of INSERT — separate
            # INSERT-then-UPDATE would fail the CHECK at INSERT time.
            owned_insert = """
                INSERT INTO tasks (
                    id, plan_id, status, dependencies, key_files,
                    priority, description, acceptance_criteria,
                    test_steps, prd_requirement, version,
                    claimed_by, claimed_at
                )
                VALUES (
                    $1, $2, $3, $4::jsonb, $5::jsonb,
                    $6, $7, $8::jsonb, $9::jsonb, $10, $11,
                    $12, NOW() - make_interval(secs => $13::int)
                )
            """
            unowned_insert = """
                INSERT INTO tasks (
                    id, plan_id, status, dependencies, key_files,
                    priority, description, acceptance_criteria,
                    test_steps, prd_requirement, version,
                    claimed_by, claimed_at
                )
                VALUES (
                    $1, $2, $3, $4::jsonb, $5::jsonb,
                    $6, $7, $8::jsonb, $9::jsonb, $10, $11,
                    NULL, NULL
                )
            """
            await conn.execute(
                owned_insert,
                "T-DONE",
                plan_id,
                TaskStatus.DONE.value,
                json.dumps([]),
                json.dumps([]),
                Priority.CRITICAL.value,
                "first task — finished",
                json.dumps([]),
                json.dumps([]),
                "",
                3,
                _TEST_WORKER_ID,
                60,  # claimed 60 s ago
            )
            await conn.execute(
                owned_insert,
                "T-RUN",
                plan_id,
                TaskStatus.IN_PROGRESS.value,
                json.dumps([]),
                json.dumps([]),
                Priority.HIGH.value,
                "second task — running",
                json.dumps([]),
                json.dumps([]),
                "",
                2,
                _TEST_WORKER_ID,
                15,  # claimed 15 s ago
            )
            await conn.execute(
                unowned_insert,
                "T-IDLE",
                plan_id,
                TaskStatus.PENDING.value,
                json.dumps([]),
                json.dumps([]),
                Priority.LOW.value,
                "third task — waiting",
                json.dumps([]),
                json.dumps([]),
                "",
                0,
            )
    finally:
        await conn.close()


# ─── fetch_dashboard_rows: pure read path ───────────────────────────────


def test_fetch_dashboard_rows_returns_seeded_projection(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — TRUNCATE makes the DB clean for this test
    database_url: str,
) -> None:
    """One SELECT round-trip surfaces every seeded row with the right fields.

    Drives the read path the dashboard uses on every tick. We open our
    own pool inside the test's :func:`asyncio.run` (rather than reusing
    the ``db_pool`` fixture) for the same reason the seed helper opens a
    transient connection — see module docstring's "Why a fresh
    asyncpg.connect for seeding" section.
    """
    plan_id = "dash-fetch-001"

    asyncio.run(_seed_three_tasks(database_url, plan_id))

    async def _fetch() -> tuple[DashboardRow, ...]:
        from whilly.adapters.db import close_pool, create_pool

        pool = await create_pool(database_url)
        try:
            return await fetch_dashboard_rows(pool, plan_id)
        finally:
            await close_pool(pool)

    rows = asyncio.run(_fetch())
    by_id = {row.task_id: row for row in rows}

    # All three seeded rows surface.
    assert set(by_id.keys()) == {"T-DONE", "T-RUN", "T-IDLE"}

    # Status mapping.
    assert by_id["T-DONE"].status is TaskStatus.DONE
    assert by_id["T-RUN"].status is TaskStatus.IN_PROGRESS
    assert by_id["T-IDLE"].status is TaskStatus.PENDING

    # Priority mapping.
    assert by_id["T-DONE"].priority is Priority.CRITICAL
    assert by_id["T-RUN"].priority is Priority.HIGH
    assert by_id["T-IDLE"].priority is Priority.LOW

    # claimed_by survives across CLAIMED → IN_PROGRESS → DONE; clears for PENDING.
    assert by_id["T-DONE"].claimed_by == _TEST_WORKER_ID
    assert by_id["T-RUN"].claimed_by == _TEST_WORKER_ID
    assert by_id["T-IDLE"].claimed_by is None

    # started_at (= claimed_at): set for owned rows, None for the unowned PENDING.
    assert by_id["T-DONE"].started_at is not None
    assert by_id["T-RUN"].started_at is not None
    assert by_id["T-IDLE"].started_at is None

    # Updated_at is always populated (schema default NOW()).
    assert all(row.updated_at is not None for row in rows)


def test_fetch_dashboard_rows_orders_by_priority_then_id(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,
) -> None:
    """Row order matches the worker visit order (priority bucket, then id).

    Same tiebreaker as :data:`whilly.adapters.db.repository._CLAIM_SQL`,
    so an operator skimming the dashboard sees tasks in the same order
    workers will pick them up. The seed helper inserts CRITICAL → HIGH
    → LOW, and the SELECT must surface that order regardless of insert
    sequence (Postgres has no implicit ordering guarantee on bare
    SELECTs).
    """
    plan_id = "dash-order-001"
    asyncio.run(_seed_three_tasks(database_url, plan_id))

    async def _fetch() -> tuple[DashboardRow, ...]:
        from whilly.adapters.db import close_pool, create_pool

        pool = await create_pool(database_url)
        try:
            return await fetch_dashboard_rows(pool, plan_id)
        finally:
            await close_pool(pool)

    rows = asyncio.run(_fetch())
    assert [row.task_id for row in rows] == ["T-DONE", "T-RUN", "T-IDLE"], (
        "expected priority-then-id ordering: CRITICAL T-DONE, HIGH T-RUN, LOW T-IDLE"
    )


def test_fetch_dashboard_rows_empty_plan_returns_empty_tuple(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,
) -> None:
    """A plan with zero tasks returns an empty tuple — not an error."""

    async def _setup_and_fetch() -> tuple[DashboardRow, ...]:
        from whilly.adapters.db import close_pool, create_pool

        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2)",
                "dash-empty-001",
                "Empty Plan",
            )
        finally:
            await conn.close()

        pool = await create_pool(database_url)
        try:
            return await fetch_dashboard_rows(pool, "dash-empty-001")
        finally:
            await close_pool(pool)

    rows = asyncio.run(_setup_and_fetch())
    assert rows == ()


# ─── full CLI surface ────────────────────────────────────────────────────


def test_run_dashboard_command_finishes_cleanly_with_max_iterations(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — TRUNCATE seeds a clean DB
    database_url: str,  # noqa: ARG001
) -> None:
    """End-to-end: seed plan → run dashboard with a 1-iteration cap → exit 0.

    Drives the entire CLI surface (argparse → DSN check → pool open →
    plan-existence probe → polling loop → render → unwind) in one shot
    against a real Postgres. The test passes a no-op ``key_source`` so
    the listener exits immediately on its first ``await`` and the
    polling loop is the sole driver of the TaskGroup termination.

    ``--interval 0.01`` keeps the per-tick sleep negligible; ``--no-color``
    sidesteps Rich's terminal autodetection (we're already non-TTY under
    pytest, but pinning it explicitly removes any platform-dependent
    behaviour). ``--max-iterations 1`` proves the loop honours its cap;
    a regression that ignored it would hang the test.
    """
    plan_id = "dash-cli-001"
    asyncio.run(_seed_three_tasks(database_url, plan_id))

    async def _no_keys() -> str | None:
        return None

    rc = run_dashboard_command(
        [
            "--plan",
            plan_id,
            "--interval",
            "0.01",
            "--max-iterations",
            "1",
            "--no-color",
        ],
        key_source=_no_keys,
    )
    assert rc == EXIT_OK


def test_run_dashboard_command_missing_plan_returns_exit_2(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,  # noqa: ARG001
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-existent ``plan_id`` exits ``2`` with a helpful stderr message.

    Same contract as ``whilly plan show`` for consistency: the SELECT
    path is shared, the operator-visible error is shaped the same way.
    """

    async def _no_keys() -> str | None:
        return None

    rc = run_dashboard_command(
        [
            "--plan",
            "no-such-plan-99999",
            "--interval",
            "0.01",
            "--max-iterations",
            "1",
            "--no-color",
        ],
        key_source=_no_keys,
    )
    assert rc == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert "no-such-plan-99999" in captured.err
    assert "not found" in captured.err.lower()
