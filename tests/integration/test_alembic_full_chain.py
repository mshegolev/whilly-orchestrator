"""End-to-end alembic chain test (TASK-108a + M3 fix-feature).

Pins the assertion that ``alembic upgrade head`` applies migrations
``001 → 002 → 003 → 004 → 005 → 006 → 007`` in order on a fresh
Postgres and ``alembic downgrade base`` reverts every step cleanly.
Mirrors the per-migration tests but exercises the whole linear
chain in one go so a single broken edge between revisions surfaces
here even when each per-migration test passes in isolation.

Note that ``information_schema`` is the source of truth for column
shape; ``alembic_version`` is the source of truth for the recorded
revision. After ``downgrade base`` the version table itself is
empty (alembic deletes the row when downgraded past the first
migration).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from alembic import command

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


# Ordered chain of migrations the suite expects on disk. If a future
# migration shifts the chain this test calls it out loudly rather than
# silently letting the chain grow without coverage.
EXPECTED_CHAIN: tuple[str, ...] = (
    "001_initial_schema",
    "002_workers_status",
    "003_events_detail",
    "004_per_worker_bearer",
    "005_plan_budget",
    "006_plan_github_ref",
    "007_plan_prd_file",
    "008_workers_owner_email",
    "009_bootstrap_tokens",
)


def test_expected_chain_files_exist_on_disk() -> None:
    """Every expected migration file ships at the canonical path."""
    versions_dir = MIGRATIONS_DIR / "versions"
    for revision in EXPECTED_CHAIN:
        path = versions_dir / f"{revision}.py"
        assert path.is_file(), f"Missing migration file at {path}"


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
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_full_chain)",
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


async def _fetchall(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def test_full_chain_upgrade_then_full_downgrade(empty_postgres_dsn: str) -> None:
    """``alembic upgrade head`` then ``alembic downgrade base`` round-trips cleanly.

    Steps:
      1. Empty Postgres (no whilly tables).
      2. Apply ``upgrade head`` — every migration in :data:`EXPECTED_CHAIN`
         lands; alembic_version reports ``006_plan_github_ref``.
      3. Verify the migration-006 deltas exist (column +
         partial UNIQUE index).
      4. Apply ``downgrade base`` — every migration's downgrade runs
         in reverse order; alembic_version table is left empty (no
         applied revisions).
      5. Verify the whilly tables and the migration-006 column are
         gone — schema returned to the pre-001 baseline.
    """
    cfg = _build_alembic_config(empty_postgres_dsn)

    # ── Step 2: upgrade head ──────────────────────────────────────────
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (chain)")

    head_version = asyncio.run(_fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version"))
    assert head_version == "009_bootstrap_tokens"

    # ── Step 3: 006- 007- and 008-specific deltas exist ─────────────
    column_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'github_issue_ref'
            """,
        )
    )
    assert int(column_count) == 1

    index_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM pg_indexes
            WHERE tablename = 'plans'
              AND indexname = 'ix_plans_github_issue_ref_unique'
            """,
        )
    )
    assert int(index_count) == 1

    # 007: ``plans.prd_file`` text NULL exists.
    prd_file_column_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'prd_file'
            """,
        )
    )
    assert int(prd_file_column_count) == 1

    # 008: ``workers.owner_email`` text NULL + partial index exist.
    owner_email_column_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'owner_email'
            """,
        )
    )
    assert int(owner_email_column_count) == 1
    owner_email_index_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM pg_indexes
            WHERE tablename = 'workers'
              AND indexname = 'ix_workers_owner_email'
            """,
        )
    )
    assert int(owner_email_index_count) == 1

    # 009: ``bootstrap_tokens`` table + partial index exist.
    bootstrap_tokens_table_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'bootstrap_tokens'
            """,
        )
    )
    assert int(bootstrap_tokens_table_count) == 1
    bootstrap_tokens_index_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM pg_indexes
            WHERE tablename = 'bootstrap_tokens'
              AND indexname = 'ix_bootstrap_tokens_owner_email_active'
            """,
        )
    )
    assert int(bootstrap_tokens_index_count) == 1

    # Confirm the whilly tables are present (sanity).
    tables = {
        row["table_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('workers', 'plans', 'tasks', 'events', 'bootstrap_tokens')
                """,
            )
        )
    }
    assert tables == {"workers", "plans", "tasks", "events", "bootstrap_tokens"}

    # ── Step 4: downgrade base ────────────────────────────────────────
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (chain)")

    base_version = asyncio.run(_fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version"))
    # ``downgrade base`` removes all rows from alembic_version (the
    # default behaviour — no revisions applied).
    assert base_version is None

    # ── Step 5: schema returned to pre-001 baseline ──────────────────
    post_downgrade_tables = {
        row["table_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('workers', 'plans', 'tasks', 'events', 'bootstrap_tokens')
                """,
            )
        )
    }
    assert post_downgrade_tables == set()


def test_full_chain_then_re_upgrade_idempotent(empty_postgres_dsn: str) -> None:
    """``upgrade head`` → ``upgrade head`` is a no-op (alembic-version unchanged).

    Pins VAL-FORGE-020 across the full chain: re-running ``upgrade
    head`` against an already-006 database is safe (same alembic
    contract every migration relies on, but spelled out here so a
    broken upgrade idempotency edge would surface in CI).
    """
    cfg = _build_alembic_config(empty_postgres_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")
    first_version = asyncio.run(_fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version"))
    assert first_version == "009_bootstrap_tokens"

    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")
    second_version = asyncio.run(_fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version"))
    assert second_version == "009_bootstrap_tokens"
