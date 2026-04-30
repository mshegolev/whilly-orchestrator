"""Integration tests for migration 007_plan_prd_file (M3 fix-feature).

Pins the data-layer half of the M3 fix-feature for VAL-FORGE-005:
the alembic migration that adds ``plans.prd_file text NULL``.
Mirrors the structure of :mod:`tests.integration.test_alembic_006`:

* ``upgrade head`` from base-006 adds the column with the documented
  nullability + no-default semantics.
* ``downgrade -1`` reverts cleanly back to revision 006.
* ``upgrade head → downgrade base → upgrade head`` round-trip
  succeeds.
* Re-running ``upgrade head`` is a no-op; existing ``prd_file``
  values are preserved.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
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


_MIGRATION_007_PATH: Path = MIGRATIONS_DIR / "versions" / "007_plan_prd_file.py"


def test_migration_007_file_exists_on_disk() -> None:
    """The 007 migration ships at the canonical path."""
    assert _MIGRATION_007_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_007_PATH}; alembic upgrade head won't apply 007 if the file is gone."
    )


@pytest.fixture
def base_006_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``006_plan_github_ref``."""
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
        _retry_colima_flake(
            pg.start,
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_007)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "006_plan_github_ref"),
            op="alembic.command.upgrade(006_plan_github_ref) (test_alembic_007)",
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
# Script-directory: 007 is the head revision
# ---------------------------------------------------------------------------


def test_007_is_head_revision() -> None:
    """The alembic script directory reports ``007_plan_prd_file`` as the head."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "007_plan_prd_file"


# ---------------------------------------------------------------------------
# Upgrade adds plans.prd_file as text NULL with no default
# ---------------------------------------------------------------------------


def test_upgrade_adds_prd_file_nullable_text(base_006_dsn: str) -> None:
    """``plans.prd_file`` exists with ``data_type=text`` and ``is_nullable=YES``.

    Pre-existing rows seeded at revision 006 retain ``prd_file IS
    NULL`` because the column has no server default — the migration
    adds the column without backfilling.
    """

    async def _seed_pre_007_plan() -> None:
        await _execute(
            base_006_dsn,
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            "plan-pre-007",
            "Pre-007 Plan",
        )

    asyncio.run(_seed_pre_007_plan())

    cfg = _build_cfg(base_006_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (006→head)")

    row = asyncio.run(
        _fetchrow(
            base_006_dsn,
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'prd_file'
            """,
        )
    )
    assert row is not None
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None

    # Pre-existing plan now has prd_file = NULL (no backfill).
    pre_existing_prd_file = asyncio.run(
        _fetchval(
            base_006_dsn,
            "SELECT prd_file FROM plans WHERE id = 'plan-pre-007'",
        )
    )
    assert pre_existing_prd_file is None


# ---------------------------------------------------------------------------
# Downgrade -1 removes the column cleanly + round-trip
# ---------------------------------------------------------------------------


def test_downgrade_removes_prd_file_column(base_006_dsn: str) -> None:
    """After ``upgrade head`` then ``downgrade -1``, the column is gone."""
    cfg = _build_cfg(base_006_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, str | None]:
        col_count = await _fetchval(
            base_006_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'prd_file'
            """,
        )
        version = await _fetchval(base_006_dsn, "SELECT version_num FROM alembic_version")
        return int(col_count), version

    col_count, version = asyncio.run(_inspect())
    assert col_count == 0
    assert version == "006_plan_github_ref"


def test_round_trip_upgrade_downgrade_base_upgrade(base_006_dsn: str) -> None:
    """``upgrade head`` → ``downgrade base`` → ``upgrade head`` succeeds at every step."""
    cfg = _build_cfg(base_006_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")

    nullable = asyncio.run(
        _fetchval(
            base_006_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'prd_file'
            """,
        )
    )
    assert nullable == "YES"


# ---------------------------------------------------------------------------
# Idempotent re-upgrade preserves data
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_006_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; existing values preserved."""
    cfg = _build_cfg(base_006_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")

    async def _seed_with_prd_file() -> None:
        await _execute(
            base_006_dsn,
            "INSERT INTO plans (id, name, prd_file) VALUES ($1, $2, $3)",
            "plan-with-prd",
            "Plan w/ PRD",
            "/tmp/docs/PRD-issue-42.md",
        )

    asyncio.run(_seed_with_prd_file())

    # Second ``upgrade head`` is a no-op against the alembic_version
    # row already at 007.
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    persisted_prd_file = asyncio.run(
        _fetchval(
            base_006_dsn,
            "SELECT prd_file FROM plans WHERE id = 'plan-with-prd'",
        )
    )
    assert persisted_prd_file == "/tmp/docs/PRD-issue-42.md"
