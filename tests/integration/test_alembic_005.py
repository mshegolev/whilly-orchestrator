"""Integration tests for migration 005_plan_budget (TASK-102).

Pins the data-layer half of TASK-102: the alembic migration that adds
``plans.budget_usd``, ``plans.spent_usd``, ``events.plan_id``, and
relaxes ``events.task_id`` to nullable. Coverage:

* VAL-BUDGET-001 — script directory advertises ``005_plan_budget`` as
  the head revision.
* VAL-BUDGET-002 / 003 — ``upgrade head`` adds both ``plans`` columns
  with the documented nullability + default semantics.
* VAL-BUDGET-004 / 070 — ``downgrade -1`` reverts cleanly; full
  ``upgrade → downgrade base → upgrade head`` round-trip succeeds.
* VAL-BUDGET-073 — re-running ``upgrade head`` against an already-005
  database is a no-op.
* VAL-BUDGET-074 — after the migration, normal CLAIM / COMPLETE event
  inserts (with ``task_id IS NOT NULL``) still succeed.

Mirrors the structure of :mod:`tests.integration.test_alembic_004`:
function-scoped pre-005 testcontainers Postgres, sync test functions
(alembic env.py uses :func:`asyncio.run` which clashes with
pytest-asyncio's running loop), asyncpg fetch via the synchronous
:func:`asyncio.run` bridge.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.conftest import (
    DOCKER_REQUIRED,
    HAS_TESTCONTAINERS,
    _build_alembic_config,
    _retry_colima_flake,
    docker_available,
    resolve_docker_host,
)
from whilly.adapters.db import MIGRATIONS_DIR

pytestmark = DOCKER_REQUIRED


_MIGRATION_005_PATH: Path = MIGRATIONS_DIR / "versions" / "005_plan_budget.py"


def test_migration_005_file_exists_on_disk() -> None:
    """The 005 migration ships at the canonical path (VAL-BUDGET-001 evidence)."""
    assert _MIGRATION_005_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_005_PATH}; alembic upgrade head won't apply 005 if the file is gone."
    )


@pytest.fixture
def base_004_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``004_per_worker_bearer``."""
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    if "DOCKER_HOST" not in os.environ:
        resolved = resolve_docker_host()
        if resolved is not None:
            monkeypatch.setenv("DOCKER_HOST", resolved)
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")

    pg = PostgresContainer("postgres:15-alpine")
    started = False
    try:
        _retry_colima_flake(pg.start, op="PostgresContainer('postgres:15-alpine').start() (test_alembic_005)")
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "004_per_worker_bearer"),
            op="alembic.command.upgrade(004_per_worker_bearer) (test_alembic_005)",
        )
        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001
                pass


def _build_cfg(dsn: str) -> Config:
    return _build_alembic_config(dsn)


def _to_asyncpg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def _fetchval(dsn: str, sql: str, *args: Any) -> Any:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _fetchrow(dsn: str, sql: str, *args: Any) -> asyncpg.Record | None:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchrow(sql, *args)
    finally:
        await conn.close()


async def _execute(dsn: str, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# VAL-BUDGET-001 — script directory advertises 005 as head
# ---------------------------------------------------------------------------


def test_005_is_in_chain_with_known_down_revision() -> None:
    """``005_plan_budget`` is reachable from 004 (chain pinning).

    Before TASK-108a's migration 006 landed, ``005`` was head. After
    006, head is ``006_plan_github_ref`` and 005 is its
    ``down_revision``. We pin both relationships here:

    * 005 has ``down_revision == "004_per_worker_bearer"`` (unchanged).
    * 005 is *not* head anymore — head is 006.

    This keeps VAL-BUDGET-001 pinning (005 sits in the chain at the
    expected position) without making the chain head an immutable
    invariant that every future migration would have to update.
    """
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    rev_005 = script.get_revision("005_plan_budget")
    assert rev_005 is not None
    assert rev_005.down_revision == "004_per_worker_bearer"
    # 005 is no longer the head (006 took it over).
    assert script.get_current_head() != "005_plan_budget"


# ---------------------------------------------------------------------------
# VAL-BUDGET-002 — upgrade adds plans.budget_usd as nullable numeric
# ---------------------------------------------------------------------------


def test_upgrade_adds_budget_usd_nullable_numeric(base_004_dsn: str) -> None:
    """``plans.budget_usd`` exists with ``data_type=numeric`` and ``is_nullable=YES``."""
    cfg = _build_cfg(base_004_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (004→head)")

    row = asyncio.run(
        _fetchrow(
            base_004_dsn,
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'budget_usd'
            """,
        )
    )
    assert row is not None
    assert row["data_type"] == "numeric"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None


# ---------------------------------------------------------------------------
# VAL-BUDGET-003 — upgrade adds plans.spent_usd numeric NOT NULL DEFAULT 0
# ---------------------------------------------------------------------------


def test_upgrade_adds_spent_usd_not_null_default_zero(base_004_dsn: str) -> None:
    """``plans.spent_usd`` exists, ``NOT NULL``, with default 0; pre-005 plans default to 0."""

    async def _seed_pre_005_plan() -> None:
        # Seed a plan row at base-004 (no budget columns yet) to verify
        # the migration applies cleanly to populated tables (per
        # VAL-BUDGET-003's "without a backfill error" clause).
        await _execute(
            base_004_dsn,
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            "plan-pre-005",
            "Pre-005 Plan",
        )

    asyncio.run(_seed_pre_005_plan())

    cfg = _build_cfg(base_004_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (004→head)")

    col_row = asyncio.run(
        _fetchrow(
            base_004_dsn,
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'spent_usd'
            """,
        )
    )
    assert col_row is not None
    assert col_row["data_type"] == "numeric"
    assert col_row["is_nullable"] == "NO"
    # The column_default is the SQL expression that produces 0 — Postgres
    # may render it as ``0`` or ``'0'::numeric`` depending on alembic's
    # quoting; the assertion just pins that it's a 0-valued literal.
    assert col_row["column_default"] is not None
    assert col_row["column_default"].startswith("0")

    # Pre-005 plan now has spent_usd=0 (the default backfill).
    spent = asyncio.run(_fetchval(base_004_dsn, "SELECT spent_usd FROM plans WHERE id = 'plan-pre-005'"))
    assert spent == Decimal("0")


# ---------------------------------------------------------------------------
# VAL-BUDGET-004 — downgrade -1 removes both columns cleanly
# ---------------------------------------------------------------------------


def test_downgrade_removes_budget_columns(base_004_dsn: str) -> None:
    """After ``upgrade 005`` then ``downgrade -1``, neither column exists.

    Pinned to revision ``005_plan_budget`` rather than ``head`` so that
    later migrations (006+) don't perturb the per-migration test: we
    are exercising the 005 downgrade path specifically, not "whatever
    head is".
    """
    cfg = _build_cfg(base_004_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "005_plan_budget"), op="upgrade 005")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, str | None]:
        col_count = await _fetchval(
            base_004_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name IN ('budget_usd', 'spent_usd')
            """,
        )
        version = await _fetchval(base_004_dsn, "SELECT version_num FROM alembic_version")
        return int(col_count), version

    col_count, version = asyncio.run(_inspect())
    assert col_count == 0
    assert version == "004_per_worker_bearer"


# ---------------------------------------------------------------------------
# VAL-BUDGET-073 — idempotent re-upgrade
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_004_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; column metadata is preserved."""
    cfg = _build_cfg(base_004_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")

    async def _seed_row_with_value() -> None:
        await _execute(
            base_004_dsn,
            "INSERT INTO plans (id, name, budget_usd, spent_usd) VALUES ($1, $2, $3, $4)",
            "plan-idempotent",
            "Idempotent Plan",
            Decimal("10.0000"),
            Decimal("3.5000"),
        )

    asyncio.run(_seed_row_with_value())

    # Second ``upgrade head`` is a no-op against the alembic_version
    # row already at 005.
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    spent = asyncio.run(_fetchval(base_004_dsn, "SELECT spent_usd FROM plans WHERE id = 'plan-idempotent'"))
    assert spent == Decimal("3.5000")
    budget = asyncio.run(_fetchval(base_004_dsn, "SELECT budget_usd FROM plans WHERE id = 'plan-idempotent'"))
    assert budget == Decimal("10.0000")


# ---------------------------------------------------------------------------
# VAL-BUDGET-070 — full upgrade → downgrade base → upgrade head round-trip
# ---------------------------------------------------------------------------


def test_round_trip_upgrade_downgrade_base_upgrade(base_004_dsn: str) -> None:
    """``upgrade head`` → ``downgrade base`` → ``upgrade head`` succeeds at every step."""
    cfg = _build_cfg(base_004_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")
    # Strip back to base, then re-upgrade. Each step exercises a
    # different migration's downgrade / upgrade, but we only assert
    # the final shape is what an immediate upgrade-head would produce.
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")

    # Final shape must match an immediate upgrade-head: budget_usd
    # nullable, spent_usd NOT NULL DEFAULT 0, events.plan_id present.
    async def _inspect() -> tuple[Any, Any, Any]:
        budget_nullable = await _fetchval(
            base_004_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'budget_usd'
            """,
        )
        spent_nullable = await _fetchval(
            base_004_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'spent_usd'
            """,
        )
        events_plan_id = await _fetchval(
            base_004_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'events' AND column_name = 'plan_id'
            """,
        )
        return budget_nullable, spent_nullable, events_plan_id

    budget_nullable, spent_nullable, events_plan_id = asyncio.run(_inspect())
    assert budget_nullable == "YES"
    assert spent_nullable == "NO"
    assert events_plan_id == 1


# ---------------------------------------------------------------------------
# VAL-BUDGET-074 — sentinel-shape events insert succeeds; per-task events
# remain valid; events.task_id is now nullable.
# ---------------------------------------------------------------------------


def test_events_table_supports_sentinel_and_per_task_rows(base_004_dsn: str) -> None:
    """After 005, both per-task events (task_id NOT NULL) and plan-level sentinel events (task_id IS NULL) succeed.

    Pins the migration's ``ALTER COLUMN events.task_id DROP NOT NULL``
    + ``ADD COLUMN events.plan_id`` pair in a single integration
    scenario.
    """
    cfg = _build_cfg(base_004_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        # Seed plan + task; events FK requires both.
        await _execute(
            base_004_dsn,
            "INSERT INTO plans (id, name, budget_usd) VALUES ($1, $2, $3)",
            "plan-evt",
            "Events Plan",
            Decimal("1.0000"),
        )
        await _execute(
            base_004_dsn,
            "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', 'medium')",
            "T-evt-1",
            "plan-evt",
        )

        # Per-task event still works (task_id IS NOT NULL).
        await _execute(
            base_004_dsn,
            "INSERT INTO events (task_id, event_type, payload) VALUES ($1, 'CLAIM', '{}'::jsonb)",
            "T-evt-1",
        )

        # Plan-level sentinel succeeds (task_id IS NULL, plan_id set).
        await _execute(
            base_004_dsn,
            (
                "INSERT INTO events (task_id, plan_id, event_type, payload) "
                "VALUES (NULL, $1, 'plan.budget_exceeded', '{}'::jsonb)"
            ),
            "plan-evt",
        )

        per_task_count = await _fetchval(
            base_004_dsn,
            "SELECT count(*)::int FROM events WHERE task_id = 'T-evt-1' AND event_type = 'CLAIM'",
        )
        sentinel_count = await _fetchval(
            base_004_dsn,
            "SELECT count(*)::int FROM events WHERE task_id IS NULL AND plan_id = 'plan-evt'",
        )
        assert per_task_count == 1
        assert sentinel_count == 1

    asyncio.run(_scenario())
