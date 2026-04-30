"""Integration tests for migration 006_plan_github_ref (TASK-108a).

Pins the data-layer half of TASK-108a: the alembic migration that
adds ``plans.github_issue_ref text NULL`` plus the partial UNIQUE
index ``ix_plans_github_issue_ref_unique``. Coverage:

* VAL-FORGE-001 — script directory advertises ``006_plan_github_ref``
  as the head revision; ``upgrade head`` adds the column with the
  documented nullability + default semantics.
* VAL-FORGE-002 — ``downgrade -1`` reverts cleanly; full
  ``upgrade → downgrade base → upgrade head`` round-trip succeeds.
* VAL-FORGE-020 — re-running ``upgrade head`` against an already-006
  database is a no-op; existing ``github_issue_ref`` values are
  preserved.
* The partial UNIQUE index is enforced (concurrent INSERTs of the
  same ``github_issue_ref`` collide on the index).

Mirrors the structure of :mod:`tests.integration.test_alembic_005`:
function-scoped pre-006 testcontainers Postgres, sync test functions,
asyncpg fetch via the synchronous :func:`asyncio.run` bridge.
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


_MIGRATION_006_PATH: Path = MIGRATIONS_DIR / "versions" / "006_plan_github_ref.py"


def test_migration_006_file_exists_on_disk() -> None:
    """The 006 migration ships at the canonical path (VAL-FORGE-001 evidence)."""
    assert _MIGRATION_006_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_006_PATH}; alembic upgrade head won't apply 006 if the file is gone."
    )


@pytest.fixture
def base_005_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``005_plan_budget``."""
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
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_006)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "005_plan_budget"),
            op="alembic.command.upgrade(005_plan_budget) (test_alembic_006)",
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
# VAL-FORGE-001 — script directory advertises 006 as head; column added
# ---------------------------------------------------------------------------


def test_006_is_head_revision() -> None:
    """The alembic script directory reports ``006_plan_github_ref`` as the head."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "006_plan_github_ref"


def test_upgrade_adds_github_issue_ref_nullable_text(base_005_dsn: str) -> None:
    """``plans.github_issue_ref`` exists with ``data_type=text`` and ``is_nullable=YES``.

    VAL-FORGE-001 evidence — pre-existing rows show ``github_issue_ref
    IS NULL`` because the column has no server default; the migration
    adds the column without backfilling.
    """

    async def _seed_pre_006_plan() -> None:
        await _execute(
            base_005_dsn,
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            "plan-pre-006",
            "Pre-006 Plan",
        )

    asyncio.run(_seed_pre_006_plan())

    cfg = _build_cfg(base_005_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (005→head)")

    row = asyncio.run(
        _fetchrow(
            base_005_dsn,
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'github_issue_ref'
            """,
        )
    )
    assert row is not None
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None

    # Pre-existing plan now has github_issue_ref = NULL (no backfill).
    pre_existing_ref = asyncio.run(
        _fetchval(
            base_005_dsn,
            "SELECT github_issue_ref FROM plans WHERE id = 'plan-pre-006'",
        )
    )
    assert pre_existing_ref is None


# ---------------------------------------------------------------------------
# VAL-FORGE-002 — downgrade -1 removes the column cleanly + round-trip
# ---------------------------------------------------------------------------


def test_downgrade_removes_github_issue_ref_column(base_005_dsn: str) -> None:
    """After ``upgrade head`` then ``downgrade -1``, the column + index are gone."""
    cfg = _build_cfg(base_005_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, int, str | None]:
        col_count = await _fetchval(
            base_005_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'github_issue_ref'
            """,
        )
        idx_count = await _fetchval(
            base_005_dsn,
            """
            SELECT count(*)::int FROM pg_indexes
            WHERE tablename = 'plans' AND indexname = 'ix_plans_github_issue_ref_unique'
            """,
        )
        version = await _fetchval(base_005_dsn, "SELECT version_num FROM alembic_version")
        return int(col_count), int(idx_count), version

    col_count, idx_count, version = asyncio.run(_inspect())
    assert col_count == 0
    assert idx_count == 0
    assert version == "005_plan_budget"


def test_round_trip_upgrade_downgrade_base_upgrade(base_005_dsn: str) -> None:
    """``upgrade head`` → ``downgrade base`` → ``upgrade head`` succeeds at every step."""
    cfg = _build_cfg(base_005_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")

    async def _inspect() -> tuple[Any, Any]:
        github_nullable = await _fetchval(
            base_005_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'github_issue_ref'
            """,
        )
        idx_count = await _fetchval(
            base_005_dsn,
            """
            SELECT count(*)::int FROM pg_indexes
            WHERE tablename = 'plans' AND indexname = 'ix_plans_github_issue_ref_unique'
            """,
        )
        return github_nullable, int(idx_count)

    github_nullable, idx_count = asyncio.run(_inspect())
    assert github_nullable == "YES"
    assert idx_count == 1


# ---------------------------------------------------------------------------
# VAL-FORGE-020 — idempotent re-upgrade preserves data
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_005_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; existing values preserved."""
    cfg = _build_cfg(base_005_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")

    async def _seed_with_ref() -> None:
        await _execute(
            base_005_dsn,
            "INSERT INTO plans (id, name, github_issue_ref) VALUES ($1, $2, $3)",
            "plan-with-ref",
            "Plan w/ ref",
            "owner/repo/42",
        )

    asyncio.run(_seed_with_ref())

    # Second ``upgrade head`` is a no-op against the alembic_version
    # row already at 006.
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    persisted_ref = asyncio.run(
        _fetchval(
            base_005_dsn,
            "SELECT github_issue_ref FROM plans WHERE id = 'plan-with-ref'",
        )
    )
    assert persisted_ref == "owner/repo/42"


# ---------------------------------------------------------------------------
# Partial UNIQUE index enforces idempotency at the schema level
# ---------------------------------------------------------------------------


def test_partial_unique_index_enforces_one_plan_per_ref(base_005_dsn: str) -> None:
    """Two INSERTs with the same non-NULL ``github_issue_ref`` collide on the partial UNIQUE."""
    cfg = _build_cfg(base_005_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        # First INSERT — succeeds.
        await _execute(
            base_005_dsn,
            "INSERT INTO plans (id, name, github_issue_ref) VALUES ($1, $2, $3)",
            "plan-a",
            "Plan A",
            "owner/repo/100",
        )
        # Second INSERT with the same ref but a different id — refused.
        with pytest.raises(asyncpg.UniqueViolationError):
            await _execute(
                base_005_dsn,
                "INSERT INTO plans (id, name, github_issue_ref) VALUES ($1, $2, $3)",
                "plan-b",
                "Plan B",
                "owner/repo/100",
            )
        # Two NULL refs MUST be allowed (partial WHERE github_issue_ref
        # IS NOT NULL excludes NULLs from the UNIQUE check).
        await _execute(
            base_005_dsn,
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            "plan-c",
            "Plan C",
        )
        await _execute(
            base_005_dsn,
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            "plan-d",
            "Plan D",
        )

    asyncio.run(_scenario())
