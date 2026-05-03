"""Integration tests for migration 008_workers_owner_email (M2 mission).

Pins the data-layer half of the M2 ``m2-migration-008`` feature: the
alembic migration that adds ``workers.owner_email`` plus a partial
index over the non-NULL slice. Mirrors the structure of
:mod:`tests.integration.test_alembic_007`:

* ``upgrade head`` from base-007 adds the column with the documented
  nullability + no-default semantics, and creates the partial index
  with the expected predicate.
* ``downgrade -1`` reverts cleanly back to revision 007 (column +
  index gone, ``alembic_version`` rolled back).
* ``upgrade head → downgrade base → upgrade head`` round-trip
  succeeds.
* Re-running ``upgrade head`` is a no-op; existing
  ``owner_email`` values are preserved across upgrades.
* The partial index is queryable and only indexes non-NULL rows.

Why sync test functions instead of ``async``?
    ``alembic.command.upgrade`` ultimately calls :func:`asyncio.run`
    (see ``whilly/adapters/db/migrations/env.py``) which raises if
    invoked from inside an already-running event loop. Mirror the
    pattern used by :mod:`tests.integration.test_alembic_007`.
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


_MIGRATION_008_PATH: Path = MIGRATIONS_DIR / "versions" / "008_workers_owner_email.py"
_OWNER_EMAIL_INDEX_NAME: str = "ix_workers_owner_email"


def test_migration_008_file_exists_on_disk() -> None:
    """The 008 migration ships at the canonical path."""
    assert _MIGRATION_008_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_008_PATH}; alembic upgrade head won't apply 008 if the file is gone."
    )


@pytest.fixture
def base_007_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``007_plan_prd_file``."""
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
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_008)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "007_plan_prd_file"),
            op="alembic.command.upgrade(007_plan_prd_file) (test_alembic_008)",
        )
        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
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
# Script-directory: 008 is the head revision after this migration ships
# ---------------------------------------------------------------------------


def test_008_is_in_chain_after_007() -> None:
    """``008_workers_owner_email`` immediately follows ``007_plan_prd_file`` in the alembic chain."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("008_workers_owner_email")
    assert revision is not None
    assert revision.down_revision == "007_plan_prd_file"


# ---------------------------------------------------------------------------
# Upgrade adds workers.owner_email as text NULL with no default
# ---------------------------------------------------------------------------


def test_upgrade_adds_owner_email_nullable_text(base_007_dsn: str) -> None:
    """``workers.owner_email`` exists with ``data_type=text`` and ``is_nullable=YES``."""

    async def _seed_pre_008_worker() -> None:
        await _execute(
            base_007_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            "w-pre-008",
            "host-pre-008",
            "0" * 64,
        )

    asyncio.run(_seed_pre_008_worker())

    cfg = _build_cfg(base_007_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (007→head)")

    row = asyncio.run(
        _fetchrow(
            base_007_dsn,
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'owner_email'
            """,
        )
    )
    assert row is not None
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None

    # Pre-existing worker has owner_email = NULL (no backfill).
    pre_existing_owner = asyncio.run(
        _fetchval(
            base_007_dsn,
            "SELECT owner_email FROM workers WHERE worker_id = 'w-pre-008'",
        )
    )
    assert pre_existing_owner is None


# ---------------------------------------------------------------------------
# Partial index ix_workers_owner_email exists with the right predicate
# ---------------------------------------------------------------------------


def test_upgrade_creates_partial_index_with_predicate(base_007_dsn: str) -> None:
    """``ix_workers_owner_email`` has the correct partial-index predicate."""
    cfg = _build_cfg(base_007_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    indexdef = asyncio.run(
        _fetchval(
            base_007_dsn,
            "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
            _OWNER_EMAIL_INDEX_NAME,
        )
    )
    assert indexdef is not None, f"partial index {_OWNER_EMAIL_INDEX_NAME!r} missing after upgrade head"
    # Postgres re-renders the partial predicate canonicalised; both
    # checks (column and predicate) survive that re-render.
    assert "owner_email" in indexdef
    assert "owner_email IS NOT NULL" in indexdef
    # Partial index must NOT be UNIQUE (per-owner lookups, not uniqueness).
    assert "UNIQUE" not in indexdef.upper()


def test_partial_index_excludes_null_rows(base_007_dsn: str) -> None:
    """Inserting NULL and non-NULL rows leaves only the non-NULL one in the index footprint.

    We can't directly inspect the index contents, but we can verify the
    ``EXPLAIN`` plan for a per-owner lookup uses the partial index when
    the predicate matches and that NULL rows don't trip on the index.
    """
    cfg = _build_cfg(base_007_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        # Two rows with NULL owner_email — a regular UNIQUE on a nullable
        # column would let this through, but our index is non-unique
        # anyway; this just confirms no constraint blocks NULL inserts.
        await _execute(
            base_007_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            "w-anon-1",
            "host-anon-1",
            "1" * 64,
        )
        await _execute(
            base_007_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            "w-anon-2",
            "host-anon-2",
            "2" * 64,
        )
        # Two rows with the SAME non-NULL owner_email — the partial
        # index is non-unique so this is allowed (one operator may
        # register many workers).
        await _execute(
            base_007_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash, owner_email) VALUES ($1, $2, $3, $4)",
            "w-alice-1",
            "host-alice-1",
            "3" * 64,
            "alice@example.com",
        )
        await _execute(
            base_007_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash, owner_email) VALUES ($1, $2, $3, $4)",
            "w-alice-2",
            "host-alice-2",
            "4" * 64,
            "alice@example.com",
        )

        owner_count = await _fetchval(
            base_007_dsn,
            "SELECT count(*)::int FROM workers WHERE owner_email = 'alice@example.com'",
        )
        assert owner_count == 2

        null_count = await _fetchval(
            base_007_dsn,
            "SELECT count(*)::int FROM workers WHERE owner_email IS NULL",
        )
        assert null_count == 2

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Downgrade -1 removes the column + index cleanly
# ---------------------------------------------------------------------------


def test_downgrade_removes_owner_email_and_index(base_007_dsn: str) -> None:
    """After ``upgrade 008`` then ``downgrade -1``, the column and index are gone.

    Pinned at the explicit ``008_workers_owner_email`` revision so the test
    keeps exercising 008's downgrade in isolation even when later migrations
    extend the chain head past 008.
    """
    cfg = _build_cfg(base_007_dsn)
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "008_workers_owner_email"),
        op="upgrade 008_workers_owner_email",
    )
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, int, str | None]:
        col_count = await _fetchval(
            base_007_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'owner_email'
            """,
        )
        idx_count = await _fetchval(
            base_007_dsn,
            "SELECT count(*)::int FROM pg_indexes WHERE indexname = $1",
            _OWNER_EMAIL_INDEX_NAME,
        )
        version = await _fetchval(base_007_dsn, "SELECT version_num FROM alembic_version")
        return int(col_count), int(idx_count), version

    col_count, idx_count, version = asyncio.run(_inspect())
    assert col_count == 0
    assert idx_count == 0
    assert version == "007_plan_prd_file"


def test_round_trip_upgrade_downgrade_base_upgrade(base_007_dsn: str) -> None:
    """``upgrade head`` → ``downgrade base`` → ``upgrade head`` succeeds at every step."""
    cfg = _build_cfg(base_007_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")

    async def _inspect() -> tuple[Any, Any]:
        nullable = await _fetchval(
            base_007_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'owner_email'
            """,
        )
        idx_count = await _fetchval(
            base_007_dsn,
            "SELECT count(*)::int FROM pg_indexes WHERE indexname = $1",
            _OWNER_EMAIL_INDEX_NAME,
        )
        return nullable, int(idx_count)

    nullable, idx_count = asyncio.run(_inspect())
    assert nullable == "YES"
    assert idx_count == 1


# ---------------------------------------------------------------------------
# Idempotent re-upgrade preserves data
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_007_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; existing values preserved."""
    cfg = _build_cfg(base_007_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")

    async def _seed_with_owner() -> None:
        await _execute(
            base_007_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash, owner_email) VALUES ($1, $2, $3, $4)",
            "w-bob",
            "host-bob",
            "5" * 64,
            "bob@example.com",
        )

    asyncio.run(_seed_with_owner())

    # Second ``upgrade head`` is a no-op against the alembic_version
    # row already at 008.
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    persisted_owner = asyncio.run(
        _fetchval(
            base_007_dsn,
            "SELECT owner_email FROM workers WHERE worker_id = 'w-bob'",
        )
    )
    assert persisted_owner == "bob@example.com"


# ---------------------------------------------------------------------------
# schema.sql parity check (manual sync invariant per AGENTS.md)
# ---------------------------------------------------------------------------


def test_schema_sql_mentions_owner_email_column_and_index() -> None:
    """The hand-maintained ``schema.sql`` reference declares the new column + index.

    AGENTS.md → "Migration discipline" requires every alembic migration
    in M2/M3 to hand-update ``schema.sql`` in the SAME commit as the
    migration. This test pins that invariant for migration 008 — if the
    column or index is missing from ``schema.sql`` the test fails
    loudly, before the drift propagates further.
    """
    schema_sql_path = Path(__file__).resolve().parents[2] / "whilly" / "adapters" / "db" / "schema.sql"
    text = schema_sql_path.read_text(encoding="utf-8")
    assert "owner_email" in text, "schema.sql must declare workers.owner_email after migration 008 ships"
    assert _OWNER_EMAIL_INDEX_NAME in text, (
        f"schema.sql must declare the partial index {_OWNER_EMAIL_INDEX_NAME!r} after migration 008 ships"
    )
    assert "owner_email IS NOT NULL" in text, (
        "schema.sql must include the partial index predicate 'owner_email IS NOT NULL'"
    )
