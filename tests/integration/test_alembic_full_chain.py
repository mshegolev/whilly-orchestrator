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
import datetime
import json
import os
from collections.abc import Iterator
from pathlib import Path
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


# Per-test outcome accumulator for the machine-readable evidence file
# (MIG-01 / MIG-02). Each test flips its own flag to True only after its
# assertions pass; the session-scoped ``_write_evidence`` fixture below
# emits the file with ``.get(..., False)`` defaults so a skipped or
# failed test produces honest ``false`` flags instead of fabricated
# ``true`` constants.
_RESULTS: dict[str, bool] = {}


@pytest.fixture(scope="session", autouse=True)
def _write_evidence() -> Iterator[None]:
    """Write migration-chain evidence after all tests in this module ran.

    Flags reflect actual outcomes recorded in :data:`_RESULTS` — a flag
    is ``true`` only if the corresponding test ran to completion. No DSN
    / connection string is included (it carries the ephemeral container
    password). Under xdist each worker writes its own view; the
    dedicated ``make migrate-chain`` gate is single-process, where this
    is exact.
    """
    yield
    evidence = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "head_revision": EXPECTED_CHAIN[-1],
        "migration_count": len(EXPECTED_CHAIN),
        "upgrade_ok": _RESULTS.get("upgrade_ok", False),
        "downgrade_ok": _RESULTS.get("downgrade_ok", False),
        "idempotent_ok": _RESULTS.get("idempotent_ok", False),
    }
    Path("migration-chain-evidence.json").write_text(json.dumps(evidence, indent=2))


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
    "010_funnel_url",
    "011_events_notify_trigger",
    "012_pull_requests_and_pr_events",
    "013_work_intents_repo_targets",
    "014_control_state",
    "015_plan_verification_commands",
    "016_jira_work_sessions",
    "017_scheduler_rules_and_cycles",
    "018_sessions_and_magic_links",
    "019a_plans_archived_at",  # 'a' suffix is intentional — not a typo
    "020_users",
    "021_users_must_change_password",
    "022_users_failed_login_counters",
    "023_worker_tags",
    "024_user_totp_secrets",
    "025_auth_audit",
    "026_webauthn_credentials",
    "027_webauthn_challenges",
    "028_webauthn_user_handles",
)


def test_expected_chain_files_exist_on_disk() -> None:
    """On-disk migrations match :data:`EXPECTED_CHAIN` exactly (set equality).

    Subset checking is not enough: a newly added migration that is not
    listed here would otherwise pass silently, and the only assertion
    that would catch it lives in the Docker-gated tests that auto-skip
    on machines without Docker — exactly how the chain went stale at
    016. This test always runs and fails loudly on any drift.
    """
    versions_dir = MIGRATIONS_DIR / "versions"
    on_disk = {p.stem for p in versions_dir.glob("*.py") if p.stem != "__init__"}
    assert on_disk == set(EXPECTED_CHAIN), (
        f"Chain drift — extra on disk: {sorted(on_disk - set(EXPECTED_CHAIN))}, "
        f"missing on disk: {sorted(set(EXPECTED_CHAIN) - on_disk)}"
    )


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
    assert head_version == EXPECTED_CHAIN[-1]

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

    # 010: ``funnel_url`` singleton table exists with the singleton check.
    funnel_url_table_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'funnel_url'
            """,
        )
    )
    assert int(funnel_url_table_count) == 1
    funnel_url_singleton_check = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.table_constraints
            WHERE table_name = 'funnel_url'
              AND constraint_name = 'funnel_url_singleton'
              AND constraint_type = 'CHECK'
            """,
        )
    )
    assert int(funnel_url_singleton_check) == 1

    # 011: ``whilly_notify_event`` plpgsql function + ``tr_events_notify``
    # AFTER INSERT trigger on ``events`` exist.
    notify_fn_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'whilly_notify_event'
            """,
        )
    )
    assert int(notify_fn_count) == 1
    notify_trigger_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = 'events'
              AND t.tgname = 'tr_events_notify'
              AND NOT t.tgisinternal
            """,
        )
    )
    assert int(notify_trigger_count) == 1

    # Confirm the whilly tables are present (sanity).
    tables = {
        row["table_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                    'workers',
                    'plans',
                    'tasks',
                    'events',
                    'bootstrap_tokens',
                    'funnel_url',
                    'work_intents',
                    'plan_origins',
                    'repo_targets',
                    'plan_repo_targets',
                    'task_repo_targets',
                    'control_state',
                    'jira_work_sessions',
                    'jira_work_events'
                  )
                """,
            )
        )
    }
    assert tables == {
        "workers",
        "plans",
        "tasks",
        "events",
        "bootstrap_tokens",
        "funnel_url",
        "work_intents",
        "plan_origins",
        "repo_targets",
        "plan_repo_targets",
        "task_repo_targets",
        "control_state",
        "jira_work_sessions",
        "jira_work_events",
    }

    control_state_columns = [
        row["column_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'control_state'
                ORDER BY ordinal_position
                """,
            )
        )
    ]
    assert control_state_columns == [
        "id",
        "paused",
        "pause_reason",
        "paused_by",
        "paused_at",
        "updated_at",
    ]

    verification_commands_column_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans' AND column_name = 'verification_commands'
            """,
        )
    )
    assert int(verification_commands_column_count) == 1

    jira_work_tables = {
        row["table_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('jira_work_sessions', 'jira_work_events')
                """,
            )
        )
    }
    assert jira_work_tables == {"jira_work_sessions", "jira_work_events"}

    # 017: ``scheduler_rules`` and ``scheduler_poll_cycles`` tables exist.
    scheduler_tables = {
        row["table_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('scheduler_rules', 'scheduler_poll_cycles')
                """,
            )
        )
    }
    assert scheduler_tables == {"scheduler_rules", "scheduler_poll_cycles"}, (
        f"Migration 017 tables missing: {scheduler_tables}"
    )

    # 019a: ``plans.archived_at`` and ``plans.last_event_at`` columns exist.
    archived_at_count = asyncio.run(
        _fetchval(
            empty_postgres_dsn,
            """
            SELECT count(*)::int FROM information_schema.columns
            WHERE table_name = 'plans'
              AND column_name IN ('archived_at', 'last_event_at')
            """,
        )
    )
    assert int(archived_at_count) == 2  # 019a added both columns

    # Upgrade-side assertions all passed — record the real outcome.
    _RESULTS["upgrade_ok"] = True

    # ── Step 4: downgrade base ────────────────────────────────────────
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base (chain)")

    base_version = asyncio.run(_fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version"))
    # ``downgrade base`` removes all rows from alembic_version (the
    # default behaviour — no revisions applied).
    assert base_version is None

    # ── Step 5: schema returned to pre-001 baseline ──────────────────
    # No hand-curated table list: after ``downgrade base`` *no* user
    # table may remain (only alembic's own bookkeeping table). This is
    # self-maintaining — a future migration whose downgrade leaves a
    # table behind fails here without anyone updating a list.
    post_downgrade_tables = sorted(
        row["table_name"]
        for row in asyncio.run(
            _fetchall(
                empty_postgres_dsn,
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name <> 'alembic_version'
                """,
            )
        )
    )
    assert post_downgrade_tables == [], f"Tables left behind after downgrade base: {post_downgrade_tables}"

    # Downgrade round-trip passed — record the real outcome.
    _RESULTS["downgrade_ok"] = True


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
    assert first_version == EXPECTED_CHAIN[-1]

    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")
    second_version = asyncio.run(_fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version"))
    assert second_version == EXPECTED_CHAIN[-1]

    # Idempotency assertions passed — record the real outcome. The
    # evidence file itself is written by the ``_write_evidence``
    # session fixture from actual per-test outcomes.
    _RESULTS["idempotent_ok"] = True
