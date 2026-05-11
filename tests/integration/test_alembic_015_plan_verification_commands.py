"""Smoke tests for migration ``015_plan_verification_commands``."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from alembic import command
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


@pytest.fixture
def empty_postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at the empty / pre-001 baseline."""
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
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_015_plan_verification_commands)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001
                pass


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


def test_015_revision_links_from_014() -> None:
    cfg = _build_alembic_config("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)

    revision = script.get_revision("015_plan_verification_commands")
    assert revision is not None
    assert revision.down_revision == "014_control_state"


def test_upgrade_adds_jsonb_column_default_and_downgrade_drops_it(empty_postgres_dsn: str) -> None:
    cfg = _build_alembic_config(empty_postgres_dsn)

    _retry_colima_flake(lambda: command.upgrade(cfg, "014_control_state"), op="upgrade 014 before 015")
    before_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'verification_commands'
            """,
        )
    )
    assert int(before_count) == 0

    _retry_colima_flake(lambda: command.upgrade(cfg, "015_plan_verification_commands"), op="upgrade 015")

    column = asyncio.run(
        _fetchrow(
            empty_postgres_dsn,
            """
            SELECT data_type, udt_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'verification_commands'
            """,
        )
    )
    assert column is not None
    assert column["data_type"] == "jsonb"
    assert column["udt_name"] == "jsonb"
    assert column["is_nullable"] == "NO"
    assert column["column_default"] == "'[]'::jsonb"

    default_value = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            INSERT INTO plans (id, name)
            VALUES ('plan-default-commands', 'Default Commands')
            RETURNING verification_commands
            """,
        )
    )
    decoded = json.loads(default_value) if isinstance(default_value, str) else default_value
    assert decoded == []

    _retry_colima_flake(lambda: command.downgrade(cfg, "014_control_state"), op="downgrade 015")
    after_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'verification_commands'
            """,
        )
    )
    assert int(after_count) == 0


def test_schema_sql_mentions_plan_verification_commands_column() -> None:
    text = (MIGRATIONS_DIR.parent / "schema.sql").read_text(encoding="utf-8")

    assert "verification_commands JSONB NOT NULL DEFAULT '[]'::jsonb" in text
    assert "ordered plan-level profile verification metadata" in text
