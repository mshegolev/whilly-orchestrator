"""Integration tests for migration ``012_pull_requests_and_pr_events`` (M2).

Pins the data-layer half of the M2 ``m2-alembic-pull-requests-and-events``
feature: the alembic migration that creates the ``pull_requests`` table
plus a composite UNIQUE index on ``(plan_id, pr_number)``. Mirrors the
structure of :mod:`tests.integration.test_alembic_011`.

Coverage map (validation contract → test):

* VAL-PR-001 → :func:`test_upgrade_creates_table_with_required_columns` +
  :func:`test_upgrade_creates_primary_key_on_id`.
* VAL-PR-002 → :func:`test_foreign_key_to_plans_id_enforced`.
* VAL-PR-003 → :func:`test_unique_index_on_plan_id_pr_number` +
  :func:`test_same_pr_number_against_different_plan_succeeds`.
* VAL-PR-028 → :func:`test_round_trip_upgrade_downgrade_upgrade_is_deterministic`.

Why sync test functions instead of ``async``?
    ``alembic.command.upgrade`` ultimately calls :func:`asyncio.run`
    (see ``whilly/adapters/db/migrations/env.py``) which raises if
    invoked from inside an already-running event loop. Mirrors the
    pattern used by every other ``test_alembic_NNN.py``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
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


_MIGRATION_012_PATH: Path = MIGRATIONS_DIR / "versions" / "012_pull_requests_and_pr_events.py"
_PULL_REQUESTS_TABLE: str = "pull_requests"
_PLAN_PR_UNIQUE_INDEX: str = "ix_pull_requests_plan_id_pr_number_unique"


def test_migration_012_file_exists_on_disk() -> None:
    """The 012 migration ships at the canonical path."""
    assert _MIGRATION_012_PATH.is_file(), f"missing migration script at {_MIGRATION_012_PATH}"


@pytest.fixture
def base_011_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``011_events_notify_trigger``."""
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
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_012)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "011_events_notify_trigger"),
            op="alembic.command.upgrade(011_events_notify_trigger) (test_alembic_012)",
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
# Script-directory chain integrity
# ---------------------------------------------------------------------------


def test_012_is_head_revision() -> None:
    """The alembic script directory reports ``012_pull_requests_and_pr_events`` as head."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "012_pull_requests_and_pr_events"


def test_012_depends_on_011() -> None:
    """Migration 012's ``down_revision`` is 011 (chain integrity)."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("012_pull_requests_and_pr_events")
    assert revision is not None
    assert revision.down_revision == "011_events_notify_trigger"


# ---------------------------------------------------------------------------
# Upgrade creates ``pull_requests`` with the right shape (VAL-PR-001)
# ---------------------------------------------------------------------------


def test_upgrade_creates_table_with_required_columns(base_011_dsn: str) -> None:
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (011→head)")

    rows = asyncio.run(
        _fetch(
            base_011_dsn,
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY column_name
            """,
            _PULL_REQUESTS_TABLE,
        )
    )
    by_name = {row["column_name"]: row for row in rows}

    expected_columns = {
        "id",
        "plan_id",
        "task_id",
        "pr_number",
        "pr_url",
        "branch",
        "head_sha",
        "state",
        "review_decision",
        "last_synced_at",
        "last_seen_review_id",
        "last_seen_check_run_id",
        "created_at",
        "updated_at",
    }
    assert set(by_name) == expected_columns, f"unexpected pull_requests column set: {set(by_name) ^ expected_columns}"

    # id BIGINT NOT NULL (BIGSERIAL via IDENTITY).
    assert by_name["id"]["data_type"] == "bigint"
    assert by_name["id"]["is_nullable"] == "NO"

    # plan_id TEXT NOT NULL — matches plans.id (TEXT) so the FK can land.
    assert by_name["plan_id"]["data_type"] == "text"
    assert by_name["plan_id"]["is_nullable"] == "NO"

    # task_id TEXT NOT NULL.
    assert by_name["task_id"]["data_type"] == "text"
    assert by_name["task_id"]["is_nullable"] == "NO"

    # pr_number INT NOT NULL.
    assert by_name["pr_number"]["data_type"] == "integer"
    assert by_name["pr_number"]["is_nullable"] == "NO"

    # pr_url / branch TEXT NOT NULL.
    for column in ("pr_url", "branch"):
        assert by_name[column]["data_type"] == "text"
        assert by_name[column]["is_nullable"] == "NO"

    # head_sha TEXT NULL.
    assert by_name["head_sha"]["data_type"] == "text"
    assert by_name["head_sha"]["is_nullable"] == "YES"

    # state TEXT NOT NULL DEFAULT 'open'.
    assert by_name["state"]["data_type"] == "text"
    assert by_name["state"]["is_nullable"] == "NO"
    assert by_name["state"]["column_default"] is not None
    assert "open" in by_name["state"]["column_default"]

    # review_decision TEXT NULL.
    assert by_name["review_decision"]["data_type"] == "text"
    assert by_name["review_decision"]["is_nullable"] == "YES"

    # last_synced_at TIMESTAMPTZ NULL.
    assert by_name["last_synced_at"]["data_type"] == "timestamp with time zone"
    assert by_name["last_synced_at"]["is_nullable"] == "YES"

    # last_seen_review_id / last_seen_check_run_id BIGINT NULL.
    for column in ("last_seen_review_id", "last_seen_check_run_id"):
        assert by_name[column]["data_type"] == "bigint"
        assert by_name[column]["is_nullable"] == "YES"

    # created_at / updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW().
    for column in ("created_at", "updated_at"):
        assert by_name[column]["data_type"] == "timestamp with time zone"
        assert by_name[column]["is_nullable"] == "NO"
        assert by_name[column]["column_default"] is not None
        assert "now()" in by_name[column]["column_default"].lower()


def test_upgrade_creates_primary_key_on_id(base_011_dsn: str) -> None:
    """``id`` is the PRIMARY KEY (single-column)."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    pk_columns = asyncio.run(
        _fetch(
            base_011_dsn,
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE c.relname = $1 AND i.indisprimary
            """,
            _PULL_REQUESTS_TABLE,
        )
    )
    assert [row["column_name"] for row in pk_columns] == ["id"]


def test_upgrade_creates_state_check_constraint(base_011_dsn: str) -> None:
    """The ``state`` CHECK enumerates ('open','merged','closed','failed')."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _seed_plan() -> None:
        await _execute(
            base_011_dsn,
            "INSERT INTO plans (id, name) VALUES ('plan-state-check', 'state-check')",
        )

    asyncio.run(_seed_plan())

    async def _attempt_invalid_state() -> None:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _execute(
                base_011_dsn,
                """
                INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch, state)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                "plan-state-check",
                "task-1",
                1,
                "https://github.com/x/y/pull/1",
                "feat-1",
                "abandoned",  # not in the closed set
            )

    asyncio.run(_attempt_invalid_state())


def test_upgrade_creates_review_decision_check_constraint(base_011_dsn: str) -> None:
    """``review_decision`` CHECK enumerates the three GitHub literals + NULL."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _seed_plan() -> None:
        await _execute(
            base_011_dsn,
            "INSERT INTO plans (id, name) VALUES ('plan-review-check', 'review-check')",
        )

    asyncio.run(_seed_plan())

    async def _attempt_invalid_review() -> None:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _execute(
                base_011_dsn,
                """
                INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch, review_decision)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                "plan-review-check",
                "task-1",
                1,
                "https://github.com/x/y/pull/1",
                "feat-1",
                "MAYBE",  # not a GitHub literal
            )

    asyncio.run(_attempt_invalid_review())

    async def _accept_each_documented_value() -> None:
        for i, decision in enumerate(("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", None), start=1):
            await _execute(
                base_011_dsn,
                """
                INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch, review_decision)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                "plan-review-check",
                f"task-decision-{i}",
                100 + i,
                f"https://github.com/x/y/pull/{100 + i}",
                f"feat-{i}",
                decision,
            )

    asyncio.run(_accept_each_documented_value())


# ---------------------------------------------------------------------------
# Foreign key to plans(id) is enforced (VAL-PR-002)
# ---------------------------------------------------------------------------


def test_foreign_key_to_plans_id_enforced(base_011_dsn: str) -> None:
    """Inserting a pull_requests row whose plan_id is unknown raises FK violation 23503."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _attempt_dangling_fk() -> None:
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError) as excinfo:
            await _execute(
                base_011_dsn,
                """
                INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch)
                VALUES ($1, $2, $3, $4, $5)
                """,
                "plan-does-not-exist",
                "task-1",
                1,
                "https://github.com/x/y/pull/1",
                "feat-1",
            )
        # Postgres SQLSTATE for foreign_key_violation is 23503.
        assert excinfo.value.sqlstate == "23503"

    asyncio.run(_attempt_dangling_fk())


def test_foreign_key_cascade_on_plan_delete(base_011_dsn: str) -> None:
    """Deleting a plan cascades to its pull_requests rows."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        await _execute(
            base_011_dsn,
            "INSERT INTO plans (id, name) VALUES ('plan-cascade', 'cascade')",
        )
        await _execute(
            base_011_dsn,
            """
            INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch)
            VALUES ($1, $2, $3, $4, $5)
            """,
            "plan-cascade",
            "task-cascade",
            42,
            "https://github.com/x/y/pull/42",
            "feat-cascade",
        )
        before_count = await _fetchval(
            base_011_dsn,
            "SELECT count(*)::int FROM pull_requests WHERE plan_id = 'plan-cascade'",
        )
        assert before_count == 1
        await _execute(base_011_dsn, "DELETE FROM plans WHERE id = 'plan-cascade'")
        after_count = await _fetchval(
            base_011_dsn,
            "SELECT count(*)::int FROM pull_requests WHERE plan_id = 'plan-cascade'",
        )
        assert after_count == 0

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Composite UNIQUE on (plan_id, pr_number) (VAL-PR-003)
# ---------------------------------------------------------------------------


def test_unique_index_on_plan_id_pr_number(base_011_dsn: str) -> None:
    """Two rows with the same (plan_id, pr_number) raise unique-violation 23505."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    indexdef = asyncio.run(
        _fetchval(
            base_011_dsn,
            "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
            _PLAN_PR_UNIQUE_INDEX,
        )
    )
    assert indexdef is not None, f"unique index {_PLAN_PR_UNIQUE_INDEX!r} missing after upgrade head"
    assert "UNIQUE" in indexdef.upper()
    assert "plan_id" in indexdef and "pr_number" in indexdef

    async def _scenario() -> None:
        await _execute(
            base_011_dsn,
            "INSERT INTO plans (id, name) VALUES ('plan-uniq', 'uniq')",
        )
        await _execute(
            base_011_dsn,
            """
            INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch)
            VALUES ($1, $2, $3, $4, $5)
            """,
            "plan-uniq",
            "task-1",
            7,
            "https://github.com/x/y/pull/7",
            "feat-7",
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError) as excinfo:
            await _execute(
                base_011_dsn,
                """
                INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch)
                VALUES ($1, $2, $3, $4, $5)
                """,
                "plan-uniq",
                "task-2",
                7,
                "https://github.com/x/y/pull/7",
                "feat-7-redux",
            )
        assert excinfo.value.sqlstate == "23505"

    asyncio.run(_scenario())


def test_same_pr_number_against_different_plan_succeeds(base_011_dsn: str) -> None:
    """Same pr_number against a different plan_id is permitted (composite, not single-column)."""
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        await _execute(
            base_011_dsn,
            "INSERT INTO plans (id, name) VALUES ('plan-a', 'a'), ('plan-b', 'b')",
        )
        await _execute(
            base_011_dsn,
            """
            INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch)
            VALUES ($1, $2, $3, $4, $5)
            """,
            "plan-a",
            "task-1",
            42,
            "https://github.com/x/y/pull/42",
            "feat-42-a",
        )
        # Same pr_number on a different plan — no violation.
        await _execute(
            base_011_dsn,
            """
            INSERT INTO pull_requests (plan_id, task_id, pr_number, pr_url, branch)
            VALUES ($1, $2, $3, $4, $5)
            """,
            "plan-b",
            "task-1",
            42,
            "https://github.com/x/y/pull/42",
            "feat-42-b",
        )
        count = await _fetchval(
            base_011_dsn,
            "SELECT count(*)::int FROM pull_requests WHERE pr_number = 42",
        )
        assert count == 2

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Downgrade leaves no orphans (VAL-PR-001 reverse half)
# ---------------------------------------------------------------------------


def test_downgrade_removes_table_and_index(base_011_dsn: str) -> None:
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, int, str | None]:
        table_count = await _fetchval(
            base_011_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
            """,
            _PULL_REQUESTS_TABLE,
        )
        idx_count = await _fetchval(
            base_011_dsn,
            "SELECT count(*)::int FROM pg_indexes WHERE indexname = $1",
            _PLAN_PR_UNIQUE_INDEX,
        )
        version = await _fetchval(base_011_dsn, "SELECT version_num FROM alembic_version")
        return int(table_count), int(idx_count), version

    table_count, idx_count, version = asyncio.run(_inspect())
    assert table_count == 0
    assert idx_count == 0
    assert version == "011_events_notify_trigger"


# ---------------------------------------------------------------------------
# Round-trip determinism (VAL-PR-028)
# ---------------------------------------------------------------------------


def _normalise_schema_dump(text: str) -> str:
    """Strip pg_dump-version / timestamp / lines that are non-deterministic across runs."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("--"):
            # ``-- Dumped from database version 16.x`` and similar
            # vary between minor versions but contain no schema delta.
            continue
        if line.startswith("SET ") or line.startswith("SELECT pg_catalog"):
            # SET search_path / SET statement_timeout differ depending
            # on the dump invocation environment.
            continue
        if line.startswith("\\restrict ") or line.startswith("\\unrestrict "):
            # Postgres-17 pg_dump emits session-randomised restriction
            # tokens at the top / bottom of the dump; the suffix changes
            # every invocation but encodes no schema delta.
            continue
        lines.append(line)
    return "\n".join(lines)


def test_round_trip_upgrade_downgrade_upgrade_is_deterministic(base_011_dsn: str) -> None:
    """``upgrade head`` → ``pg_dump`` → ``downgrade base`` → ``upgrade head`` → ``pg_dump`` byte-equal.

    Pins VAL-PR-028: the migration is reversible and idempotent so a
    re-run after a clean wipe lands on a schema fingerprint identical
    to the original. Mirrors the discipline used by every other
    M2/M3 alembic test on this repo.
    """
    cfg = _build_cfg(base_011_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")
    asyncpg_dsn = _to_asyncpg_dsn(base_011_dsn)

    def _dump() -> str:
        # ``--no-owner --no-privileges`` strips out pg_user-id details
        # that are deterministic per-container but not per-test;
        # ``-s`` keeps the dump schema-only.
        result = subprocess.run(
            [
                "pg_dump",
                "--schema-only",
                "--no-owner",
                "--no-privileges",
                asyncpg_dsn,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return _normalise_schema_dump(result.stdout)

    if subprocess.run(["which", "pg_dump"], capture_output=True).returncode != 0:
        pytest.skip("pg_dump binary not available; skipping round-trip dump comparison")

    fingerprint_first = _dump()
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")
    fingerprint_second = _dump()

    assert fingerprint_first == fingerprint_second, (
        "schema fingerprint drifted between first and second upgrade — round-trip not deterministic"
    )


# ---------------------------------------------------------------------------
# schema.sql parity check
# ---------------------------------------------------------------------------


def test_schema_sql_mentions_pull_requests_table_and_index() -> None:
    """The hand-maintained ``schema.sql`` reference declares the new table + index.

    AGENTS.md → "Migration discipline" requires every alembic migration
    to hand-update ``schema.sql`` in the SAME commit as the migration.
    """
    schema_sql_path = Path(__file__).resolve().parents[2] / "whilly" / "adapters" / "db" / "schema.sql"
    text = schema_sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE pull_requests" in text, (
        "schema.sql must declare the pull_requests table after migration 012 ships"
    )
    for required_column in (
        "plan_id",
        "task_id",
        "pr_number",
        "pr_url",
        "branch",
        "head_sha",
        "state",
        "review_decision",
        "last_synced_at",
        "last_seen_review_id",
        "last_seen_check_run_id",
        "created_at",
        "updated_at",
    ):
        assert required_column in text, f"schema.sql must declare pull_requests.{required_column}"
    assert _PLAN_PR_UNIQUE_INDEX in text, (
        f"schema.sql must declare the composite unique index {_PLAN_PR_UNIQUE_INDEX!r}"
    )
    assert "REFERENCES plans (id) ON DELETE CASCADE" in text, (
        "schema.sql must declare the FK from pull_requests.plan_id → plans.id ON DELETE CASCADE"
    )
