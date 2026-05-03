"""Integration tests for migration 010_funnel_url (M2 mission).

Pins the data-layer half of ``m2-localhostrun-funnel-sidecar``: the
alembic migration that creates the ``funnel_url`` singleton table
the sidecar upserts on every reconnect with the latest assigned
``https://<random>.lhr.life`` URL. Mirrors the structure of
:mod:`tests.integration.test_alembic_009`:

* ``upgrade head`` from base-009 creates the table with the
  documented column shapes / nullability / defaults and the
  ``CHECK (id = 1)`` singleton constraint.
* ``downgrade -1`` reverts cleanly back to revision 009 (table
  gone, ``alembic_version`` rolled back).
* ``upgrade head → downgrade base → upgrade head`` round-trip
  succeeds across the full chain.
* Re-running ``upgrade head`` is a no-op; existing rows preserved.
* The singleton invariant is enforced — inserting ``id != 1`` is
  rejected at the schema level.
* ``ON CONFLICT (id) DO UPDATE`` upsert (the sidecar's publish
  contract) leaves a single row per upsert.
* ``schema.sql`` is in sync with the migration (manual discipline
  per AGENTS.md "Migration discipline").
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


_MIGRATION_010_PATH: Path = MIGRATIONS_DIR / "versions" / "010_funnel_url.py"
_FUNNEL_URL_TABLE: str = "funnel_url"
_FUNNEL_URL_SINGLETON_CONSTRAINT: str = "funnel_url_singleton"


def test_migration_010_file_exists_on_disk() -> None:
    """The 010 migration ships at the canonical path."""
    assert _MIGRATION_010_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_010_PATH}; alembic upgrade head won't apply 010."
    )


@pytest.fixture
def base_009_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``009_bootstrap_tokens``."""
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
            op="PostgresContainer('postgres:15-alpine').start() (test_migration_010)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "009_bootstrap_tokens"),
            op="alembic.command.upgrade(009_bootstrap_tokens) (test_migration_010)",
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
# Script-directory: 010 is the head revision after this migration ships
# ---------------------------------------------------------------------------


def test_010_is_head_revision() -> None:
    """The alembic script directory reports ``010_funnel_url`` as the head."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "010_funnel_url"


def test_010_depends_on_009() -> None:
    """Migration 010's ``down_revision`` is 009."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("010_funnel_url")
    assert revision is not None
    assert revision.down_revision == "009_bootstrap_tokens"


# ---------------------------------------------------------------------------
# Upgrade creates ``funnel_url`` table with the right column shape
# ---------------------------------------------------------------------------


def test_upgrade_creates_table_with_required_columns(base_009_dsn: str) -> None:
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (009→head)")

    rows = asyncio.run(
        _fetch(
            base_009_dsn,
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY column_name
            """,
            _FUNNEL_URL_TABLE,
        )
    )
    by_name = {row["column_name"]: row for row in rows}

    expected_columns = {"id", "url", "updated_at"}
    assert set(by_name) == expected_columns, f"unexpected columns on funnel_url: {set(by_name) ^ expected_columns}"

    # id integer NOT NULL DEFAULT 1
    assert by_name["id"]["data_type"] == "integer"
    assert by_name["id"]["is_nullable"] == "NO"
    assert by_name["id"]["column_default"] is not None
    assert "1" in by_name["id"]["column_default"]

    # url text NOT NULL
    assert by_name["url"]["data_type"] == "text"
    assert by_name["url"]["is_nullable"] == "NO"

    # updated_at timestamptz NOT NULL DEFAULT NOW()
    assert by_name["updated_at"]["data_type"] == "timestamp with time zone"
    assert by_name["updated_at"]["is_nullable"] == "NO"
    assert by_name["updated_at"]["column_default"] is not None
    assert "now()" in by_name["updated_at"]["column_default"].lower()


def test_upgrade_creates_primary_key_on_id(base_009_dsn: str) -> None:
    """``id`` is the PRIMARY KEY (single-column PK)."""
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    pk_columns = asyncio.run(
        _fetch(
            base_009_dsn,
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE c.relname = $1 AND i.indisprimary
            """,
            _FUNNEL_URL_TABLE,
        )
    )
    assert [row["column_name"] for row in pk_columns] == ["id"]


def test_upgrade_creates_singleton_check_constraint(base_009_dsn: str) -> None:
    """``CHECK (id = 1)`` constraint exists and is named ``funnel_url_singleton``."""
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    constraint_count = asyncio.run(
        _fetchval(
            base_009_dsn,
            """
            SELECT count(*)::int FROM information_schema.table_constraints
            WHERE table_name = $1
              AND constraint_name = $2
              AND constraint_type = 'CHECK'
            """,
            _FUNNEL_URL_TABLE,
            _FUNNEL_URL_SINGLETON_CONSTRAINT,
        )
    )
    assert int(constraint_count) == 1


# ---------------------------------------------------------------------------
# Singleton invariant — only ``id = 1`` rows are accepted
# ---------------------------------------------------------------------------


def test_singleton_check_rejects_other_ids(base_009_dsn: str) -> None:
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _execute(
                base_009_dsn,
                "INSERT INTO funnel_url (id, url) VALUES (2, 'https://bad.lhr.life')",
            )

    asyncio.run(_scenario())


def test_default_id_is_1(base_009_dsn: str) -> None:
    """An ``INSERT`` without an explicit ``id`` defaults to 1."""
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        await _execute(
            base_009_dsn,
            "INSERT INTO funnel_url (url) VALUES ('https://abc.lhr.life')",
        )
        rid = await _fetchval(base_009_dsn, "SELECT id FROM funnel_url")
        assert rid == 1

    asyncio.run(_scenario())


def test_upsert_on_conflict_overwrites_in_place(base_009_dsn: str) -> None:
    """Sidecar publish contract: ``INSERT ... ON CONFLICT (id) DO UPDATE`` works."""
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        for url in (
            "https://aaa.lhr.life",
            "https://bbb.lhr.life",
            "https://ccc.lhr.life",
        ):
            await _execute(
                base_009_dsn,
                """
                INSERT INTO funnel_url (id, url) VALUES (1, $1)
                ON CONFLICT (id) DO UPDATE SET url=EXCLUDED.url, updated_at=NOW()
                """,
                url,
            )
        row_count = await _fetchval(base_009_dsn, "SELECT count(*)::int FROM funnel_url")
        assert int(row_count) == 1
        latest_url = await _fetchval(base_009_dsn, "SELECT url FROM funnel_url WHERE id = 1")
        assert latest_url == "https://ccc.lhr.life"

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Downgrade -1 removes the table cleanly
# ---------------------------------------------------------------------------


def test_downgrade_removes_table(base_009_dsn: str) -> None:
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, str | None]:
        table_count = await _fetchval(
            base_009_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
            """,
            _FUNNEL_URL_TABLE,
        )
        version = await _fetchval(base_009_dsn, "SELECT version_num FROM alembic_version")
        return int(table_count), version

    table_count, version = asyncio.run(_inspect())
    assert table_count == 0
    assert version == "009_bootstrap_tokens"


def test_round_trip_upgrade_downgrade_base_upgrade(base_009_dsn: str) -> None:
    """``upgrade head`` → ``downgrade base`` → ``upgrade head`` succeeds."""
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")

    table_count = asyncio.run(
        _fetchval(
            base_009_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
            """,
            _FUNNEL_URL_TABLE,
        )
    )
    assert int(table_count) == 1


# ---------------------------------------------------------------------------
# Idempotent re-upgrade preserves existing rows
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_009_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; existing row preserved."""
    cfg = _build_cfg(base_009_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")

    async def _seed() -> None:
        await _execute(
            base_009_dsn,
            "INSERT INTO funnel_url (id, url) VALUES (1, 'https://seed.lhr.life')",
        )

    asyncio.run(_seed())

    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    persisted_url = asyncio.run(_fetchval(base_009_dsn, "SELECT url FROM funnel_url WHERE id = 1"))
    assert persisted_url == "https://seed.lhr.life"


# ---------------------------------------------------------------------------
# schema.sql parity check
# ---------------------------------------------------------------------------


def test_schema_sql_mentions_funnel_url_table_and_check() -> None:
    """``schema.sql`` reference declares the new table + singleton CHECK.

    AGENTS.md → "Migration discipline" requires every alembic
    migration in M2/M3 to hand-update ``schema.sql`` in the SAME
    commit as the migration. This test pins that invariant for
    migration 010.
    """
    schema_sql_path = Path(__file__).resolve().parents[2] / "whilly" / "adapters" / "db" / "schema.sql"
    text = schema_sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE funnel_url" in text, "schema.sql must declare the funnel_url table after migration 010 ships"
    for required_column in ("id", "url", "updated_at"):
        assert required_column in text, f"schema.sql must declare funnel_url.{required_column}"
    assert _FUNNEL_URL_SINGLETON_CONSTRAINT in text, "schema.sql must declare the funnel_url_singleton CHECK constraint"
    assert "id = 1" in text or "id=1" in text, "schema.sql must declare the singleton predicate 'id = 1'"
