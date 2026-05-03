"""Integration tests for migration 009_bootstrap_tokens (M2 mission).

Pins the data-layer half of the M2 ``m2-migration-009-and-repo``
feature: the alembic migration that creates the ``bootstrap_tokens``
table plus a partial index over the active slice. Mirrors the
structure of :mod:`tests.integration.test_alembic_008`:

* ``upgrade head`` from base-008 creates the table with the
  documented column shapes / nullability / defaults, and creates the
  partial index with the ``WHERE revoked_at IS NULL`` predicate.
* ``downgrade -1`` reverts cleanly back to revision 008 (table +
  index gone, ``alembic_version`` rolled back).
* ``upgrade head → downgrade base → upgrade head`` round-trip
  succeeds across the full chain.
* Re-running ``upgrade head`` is a no-op; existing rows are
  preserved across upgrades.
* The partial index is queryable and only indexes active rows.
* ``schema.sql`` is in sync with the migration (manual discipline
  per AGENTS.md "Migration discipline").

Why sync test functions instead of ``async``?
    ``alembic.command.upgrade`` ultimately calls :func:`asyncio.run`
    (see ``whilly/adapters/db/migrations/env.py``) which raises if
    invoked from inside an already-running event loop. Mirror the
    pattern used by :mod:`tests.integration.test_alembic_008`.
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


_MIGRATION_009_PATH: Path = MIGRATIONS_DIR / "versions" / "009_bootstrap_tokens.py"
_BOOTSTRAP_TOKENS_TABLE: str = "bootstrap_tokens"
_BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX: str = "ix_bootstrap_tokens_owner_email_active"


def test_migration_009_file_exists_on_disk() -> None:
    """The 009 migration ships at the canonical path (VAL-M2-MIGRATE-009-001)."""
    assert _MIGRATION_009_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_009_PATH}; alembic upgrade head won't apply 009 if the file is gone."
    )


@pytest.fixture
def base_008_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``008_workers_owner_email``."""
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
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_009)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "008_workers_owner_email"),
            op="alembic.command.upgrade(008_workers_owner_email) (test_alembic_009)",
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


async def _fetch(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(dsn: str, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Script-directory: 009 is the head revision after this migration ships
# ---------------------------------------------------------------------------


def test_009_in_chain_with_known_predecessor() -> None:
    """``009_bootstrap_tokens`` is a known revision in the alembic chain.

    The head revision moves forward as new migrations land (010 funnel_url,
    011 events notify trigger, …); pinning ``head == "009_bootstrap_tokens"``
    here would force every downstream worker to update this test. Instead we
    verify ``009_bootstrap_tokens`` is reachable via :class:`ScriptDirectory`
    and that its ``down_revision`` chain links to ``008``.
    """
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("009_bootstrap_tokens")
    assert revision is not None
    assert revision.down_revision == "008_workers_owner_email"


def test_009_depends_on_008() -> None:
    """Migration 009's ``down_revision`` is 008 (VAL-M2-MIGRATE-009-007)."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("009_bootstrap_tokens")
    assert revision is not None
    assert revision.down_revision == "008_workers_owner_email"


# ---------------------------------------------------------------------------
# Upgrade creates ``bootstrap_tokens`` table with the right column shape
# (VAL-M2-MIGRATE-009-002 / VAL-M2-MIGRATE-009-003)
# ---------------------------------------------------------------------------


def test_upgrade_creates_table_with_required_columns(base_008_dsn: str) -> None:
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (008→head)")

    rows = asyncio.run(
        _fetch(
            base_008_dsn,
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY column_name
            """,
            _BOOTSTRAP_TOKENS_TABLE,
        )
    )
    by_name = {row["column_name"]: row for row in rows}

    expected_columns = {
        "token_hash",
        "owner_email",
        "created_at",
        "expires_at",
        "revoked_at",
        "is_admin",
    }
    assert set(by_name) == expected_columns, (
        f"unexpected columns on bootstrap_tokens: {set(by_name) ^ expected_columns}"
    )

    # token_hash text NOT NULL
    assert by_name["token_hash"]["data_type"] == "text"
    assert by_name["token_hash"]["is_nullable"] == "NO"

    # owner_email text NOT NULL
    assert by_name["owner_email"]["data_type"] == "text"
    assert by_name["owner_email"]["is_nullable"] == "NO"

    # created_at timestamptz NOT NULL DEFAULT NOW()
    assert by_name["created_at"]["data_type"] == "timestamp with time zone"
    assert by_name["created_at"]["is_nullable"] == "NO"
    assert by_name["created_at"]["column_default"] is not None
    assert "now()" in by_name["created_at"]["column_default"].lower()

    # expires_at timestamptz NULL
    assert by_name["expires_at"]["data_type"] == "timestamp with time zone"
    assert by_name["expires_at"]["is_nullable"] == "YES"
    assert by_name["expires_at"]["column_default"] is None

    # revoked_at timestamptz NULL
    assert by_name["revoked_at"]["data_type"] == "timestamp with time zone"
    assert by_name["revoked_at"]["is_nullable"] == "YES"
    assert by_name["revoked_at"]["column_default"] is None

    # is_admin boolean NOT NULL DEFAULT false
    assert by_name["is_admin"]["data_type"] == "boolean"
    assert by_name["is_admin"]["is_nullable"] == "NO"
    assert by_name["is_admin"]["column_default"] is not None
    assert "false" in by_name["is_admin"]["column_default"].lower()


def test_upgrade_creates_primary_key_on_token_hash(base_008_dsn: str) -> None:
    """``token_hash`` is the PRIMARY KEY (VAL-M2-MIGRATE-009-002 — PK named on token_hash)."""
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "009_bootstrap_tokens"), op="upgrade 009_bootstrap_tokens")

    pk_columns = asyncio.run(
        _fetch(
            base_008_dsn,
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE c.relname = $1 AND i.indisprimary
            """,
            _BOOTSTRAP_TOKENS_TABLE,
        )
    )
    assert [row["column_name"] for row in pk_columns] == ["token_hash"]


# ---------------------------------------------------------------------------
# Partial index ix_bootstrap_tokens_owner_email_active exists with the right
# predicate (VAL-M2-MIGRATE-009-004)
# ---------------------------------------------------------------------------


def test_upgrade_creates_partial_index_with_predicate(base_008_dsn: str) -> None:
    """``ix_bootstrap_tokens_owner_email_active`` has predicate ``WHERE revoked_at IS NULL``."""
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "009_bootstrap_tokens"), op="upgrade 009_bootstrap_tokens")

    indexdef = asyncio.run(
        _fetchval(
            base_008_dsn,
            "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
            _BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX,
        )
    )
    assert indexdef is not None, f"partial index {_BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX!r} missing after upgrade head"
    assert "owner_email" in indexdef
    assert "revoked_at IS NULL" in indexdef
    # Partial index must NOT be UNIQUE (per-owner lookups, not uniqueness).
    assert "UNIQUE" not in indexdef.upper()


def test_partial_index_excludes_revoked_rows(base_008_dsn: str) -> None:
    """A revoked row coexists with an active row for the same owner_email."""
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "009_bootstrap_tokens"), op="upgrade 009_bootstrap_tokens")

    async def _scenario() -> None:
        await _execute(
            base_008_dsn,
            """
            INSERT INTO bootstrap_tokens (token_hash, owner_email)
            VALUES ($1, $2)
            """,
            "a" * 64,
            "alice@example.com",
        )
        await _execute(
            base_008_dsn,
            """
            INSERT INTO bootstrap_tokens (token_hash, owner_email, revoked_at)
            VALUES ($1, $2, NOW())
            """,
            "b" * 64,
            "alice@example.com",
        )
        active_count = await _fetchval(
            base_008_dsn,
            """
            SELECT count(*)::int FROM bootstrap_tokens
            WHERE owner_email = 'alice@example.com' AND revoked_at IS NULL
            """,
        )
        assert active_count == 1
        all_count = await _fetchval(
            base_008_dsn,
            "SELECT count(*)::int FROM bootstrap_tokens WHERE owner_email = 'alice@example.com'",
        )
        assert all_count == 2

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Downgrade -1 removes the table + index cleanly (VAL-M2-MIGRATE-009-006)
# ---------------------------------------------------------------------------


def test_downgrade_removes_table_and_index(base_008_dsn: str) -> None:
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "009_bootstrap_tokens"), op="upgrade 009_bootstrap_tokens")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, int, str | None]:
        table_count = await _fetchval(
            base_008_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
            """,
            _BOOTSTRAP_TOKENS_TABLE,
        )
        idx_count = await _fetchval(
            base_008_dsn,
            "SELECT count(*)::int FROM pg_indexes WHERE indexname = $1",
            _BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX,
        )
        version = await _fetchval(base_008_dsn, "SELECT version_num FROM alembic_version")
        return int(table_count), int(idx_count), version

    table_count, idx_count, version = asyncio.run(_inspect())
    assert table_count == 0
    assert idx_count == 0
    assert version == "008_workers_owner_email"


def test_round_trip_upgrade_downgrade_base_upgrade(base_008_dsn: str) -> None:
    """``upgrade 009`` → ``downgrade base`` → ``upgrade 009`` succeeds at every step."""
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "009_bootstrap_tokens"),
        op="upgrade 009_bootstrap_tokens (rt-1)",
    )
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "009_bootstrap_tokens"),
        op="upgrade 009_bootstrap_tokens (rt-2)",
    )

    async def _inspect() -> tuple[int, int]:
        table_count = await _fetchval(
            base_008_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
            """,
            _BOOTSTRAP_TOKENS_TABLE,
        )
        idx_count = await _fetchval(
            base_008_dsn,
            "SELECT count(*)::int FROM pg_indexes WHERE indexname = $1",
            _BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX,
        )
        return int(table_count), int(idx_count)

    table_count, idx_count = asyncio.run(_inspect())
    assert table_count == 1
    assert idx_count == 1


# ---------------------------------------------------------------------------
# Idempotent re-upgrade preserves existing rows (VAL-M2-MIGRATE-009-901)
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_008_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; existing rows preserved."""
    cfg = _build_cfg(base_008_dsn)
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "009_bootstrap_tokens"),
        op="upgrade 009_bootstrap_tokens (1)",
    )

    async def _seed() -> None:
        await _execute(
            base_008_dsn,
            """
            INSERT INTO bootstrap_tokens (token_hash, owner_email, is_admin)
            VALUES ($1, $2, $3)
            """,
            "f" * 64,
            "carol@example.com",
            True,
        )

    asyncio.run(_seed())

    _retry_colima_flake(
        lambda: command.upgrade(cfg, "009_bootstrap_tokens"),
        op="upgrade 009_bootstrap_tokens (2)",
    )

    persisted_owner = asyncio.run(
        _fetchval(
            base_008_dsn,
            "SELECT owner_email FROM bootstrap_tokens WHERE token_hash = $1",
            "f" * 64,
        )
    )
    assert persisted_owner == "carol@example.com"


# ---------------------------------------------------------------------------
# schema.sql parity check (VAL-M2-MIGRATE-009-005 / VAL-M2-MIGRATE-009-902)
# ---------------------------------------------------------------------------


def test_schema_sql_mentions_bootstrap_tokens_table_and_index() -> None:
    """The hand-maintained ``schema.sql`` reference declares the new table + index.

    AGENTS.md → "Migration discipline" requires every alembic migration
    in M2/M3 to hand-update ``schema.sql`` in the SAME commit as the
    migration. This test pins that invariant for migration 009 — if any
    of the table, columns, or partial index predicate is missing from
    ``schema.sql`` the test fails loudly, before drift propagates.
    """
    schema_sql_path = Path(__file__).resolve().parents[2] / "whilly" / "adapters" / "db" / "schema.sql"
    text = schema_sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE bootstrap_tokens" in text, (
        "schema.sql must declare the bootstrap_tokens table after migration 009 ships"
    )
    for required_column in ("token_hash", "owner_email", "created_at", "expires_at", "revoked_at", "is_admin"):
        assert required_column in text, f"schema.sql must declare bootstrap_tokens.{required_column}"
    assert _BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX in text, (
        f"schema.sql must declare the partial index {_BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX!r}"
    )
    assert "revoked_at IS NULL" in text, (
        "schema.sql must include the partial index predicate 'revoked_at IS NULL' for bootstrap_tokens"
    )
