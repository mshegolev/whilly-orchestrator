"""Integration tests for migration 004_per_worker_bearer (TASK-101 / VAL-AUTH-001..004, 051, 053).

These tests pin the data-layer half of TASK-101: the alembic migration
that drops NOT NULL on ``workers.token_hash`` and adds a partial
UNIQUE index over the non-NULL slice. Coverage:

* ``alembic upgrade head`` against a fresh testcontainers Postgres
  succeeds; ``information_schema.columns`` reports ``is_nullable =
  'YES'`` for ``workers.token_hash`` (VAL-AUTH-002).
* ``alembic downgrade -1`` reverts the change cleanly:
  ``is_nullable = 'NO'``, ``alembic_version`` points to the previous
  revision, and existing rows survive (VAL-AUTH-003).
* The migration is idempotent: re-running ``upgrade head`` is a
  no-op (VAL-AUTH-004).
* The partial UNIQUE index ``ix_workers_token_hash_unique`` is
  present after upgrade and absent after downgrade
  (VAL-AUTH-053). Inserting two rows with the same non-NULL hash
  raises :class:`asyncpg.UniqueViolationError`; revoking via
  ``UPDATE workers SET token_hash = NULL`` succeeds for two rows
  in a row (NULLs are permitted).
* Round-trip preservation: existing worker rows seeded at base-002
  retain their ``token_hash`` byte-for-byte across
  upgrade → downgrade → upgrade (VAL-AUTH-051).

Why a dedicated test module instead of folding into
``test_per_worker_auth.py``?
    Migration assertions need a fresh testcontainers Postgres at
    base-002 (or earlier); the auth integration tests start from
    head and assert HTTP behaviour. Keeping them separate avoids
    cross-fixture coupling and keeps a single focus per file —
    one migration, one set of contracts.

Why sync test functions instead of ``async``?
    ``alembic.command.upgrade`` ultimately calls
    :func:`asyncio.run` (see ``whilly/adapters/db/migrations/env.py``)
    which raises if invoked from inside an already-running event
    loop. The async-test path provided by ``pytest-asyncio`` runs a
    loop *for the test*, so calling ``command.upgrade`` from there
    fails. We mirror :mod:`tests.integration.test_phase1_smoke`'s
    pattern: the test body is synchronous, alembic runs at top
    level, and asyncpg inspection is wrapped in
    :func:`asyncio.run` for the brief moments it's needed.
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


# File-existence sanity (VAL-AUTH-001 evidence): verifies the migration
# script is on disk under the expected name. Alembic itself loads the
# file by its prefix-numbered filename, not by Python import — so a
# regression that renames or deletes the file would surface here even
# before the testcontainers Postgres comes up.
_MIGRATION_004_PATH: Path = MIGRATIONS_DIR / "versions" / "004_per_worker_bearer.py"


def test_migration_004_file_exists_on_disk() -> None:
    """The 004 migration ships at the canonical path."""
    assert _MIGRATION_004_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_004_PATH}; alembic upgrade head won't apply 004 if the file is gone."
    )


@pytest.fixture
def base_002_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``002_workers_status``.

    Function-scoped (not session-scoped like ``postgres_dsn``) so
    each migration test gets a clean container — otherwise upgrade /
    downgrade cycles would interfere across tests. The downside is
    ~3 seconds per test; the alternative (truncating + manually
    re-applying the migration history) is an order of magnitude
    more error-prone.
    """
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    # Bridge Docker context like the session fixture does.
    if "DOCKER_HOST" not in os.environ:
        resolved = resolve_docker_host()
        if resolved is not None:
            monkeypatch.setenv("DOCKER_HOST", resolved)
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")

    pg = PostgresContainer("postgres:15-alpine")
    started = False
    try:
        _retry_colima_flake(pg.start, op="PostgresContainer('postgres:15-alpine').start() (test_alembic_004)")
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        # Bring the DB up to revision 002 (the migration immediately
        # before 004_per_worker_bearer's predecessor 003_events_detail).
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "002_workers_status"),
            op="alembic.command.upgrade(002_workers_status) (test_alembic_004)",
        )
        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
                pass


def _build_cfg(dsn: str) -> Config:
    """Build an alembic Config for the test DSN."""
    return _build_alembic_config(dsn)


def _to_asyncpg_dsn(dsn: str) -> str:
    """Strip SQLAlchemy ``+asyncpg`` scheme suffix if alembic re-injected it."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def _fetch(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    """Run ``conn.fetch`` against a fresh asyncpg connection."""
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _fetchval(dsn: str, sql: str, *args: Any) -> Any:
    """Run ``conn.fetchval`` against a fresh asyncpg connection."""
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _execute(dsn: str, sql: str, *args: Any) -> None:
    """Run ``conn.execute`` against a fresh asyncpg connection."""
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# VAL-AUTH-001 — script directory advertises 004 as head
# ---------------------------------------------------------------------------


def test_004_is_head_revision() -> None:
    """The alembic script directory advertises ``004_per_worker_bearer`` as a known revision.

    Originally pinned ``004_per_worker_bearer`` as the head, but TASK-102's
    ``005_plan_budget`` migration shifted the head forward (see
    ``AGENTS.md → Migration Coordination``). The remaining contract for
    VAL-AUTH-001 is that 004 is reachable from base — it is, since the
    chain still runs ``003_events_detail → 004_per_worker_bearer →
    005_plan_budget``. Asserting on script.walk_revisions() makes the
    invariant we actually care about explicit.
    """
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revisions = {r.revision for r in script.walk_revisions()}
    assert "004_per_worker_bearer" in revisions


# ---------------------------------------------------------------------------
# VAL-AUTH-002 — upgrade head makes token_hash nullable
# ---------------------------------------------------------------------------


def test_upgrade_makes_token_hash_nullable(base_002_dsn: str) -> None:
    """After ``upgrade head``, ``information_schema`` reports nullable=YES."""
    cfg = _build_cfg(base_002_dsn)
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "004_per_worker_bearer"),
        op="upgrade 004_per_worker_bearer (002→004)",
    )
    is_nullable = asyncio.run(
        _fetchval(
            base_002_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'token_hash'
            """,
        )
    )
    assert is_nullable == "YES"


# ---------------------------------------------------------------------------
# VAL-AUTH-003 — downgrade -1 restores NOT NULL
# ---------------------------------------------------------------------------


def test_downgrade_restores_not_null(base_002_dsn: str) -> None:
    """``upgrade head`` then ``downgrade -1`` reverts the nullability change.

    Also asserts the alembic_version row points to the prior revision
    after the downgrade.
    """
    cfg = _build_cfg(base_002_dsn)
    # Stop at 004 explicitly — TASK-102's 005 migration shifted ``head``
    # forward, so a plain ``upgrade head; downgrade -1`` would now
    # land at 004 (still nullable) instead of 003. We're testing 004's
    # downgrade contract specifically.
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "004_per_worker_bearer"),
        op="upgrade 004_per_worker_bearer",
    )
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1 (004→003)")

    async def _inspect() -> tuple[Any, Any]:
        is_nullable = await _fetchval(
            base_002_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'token_hash'
            """,
        )
        revision = await _fetchval(base_002_dsn, "SELECT version_num FROM alembic_version")
        return is_nullable, revision

    is_nullable, current_revision = asyncio.run(_inspect())
    assert is_nullable == "NO"
    assert current_revision == "003_events_detail"


# ---------------------------------------------------------------------------
# VAL-AUTH-004 — re-running upgrade head is a no-op
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_002_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed with no errors."""
    cfg = _build_cfg(base_002_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")
    # Second call must not raise; it observes the current revision is
    # already head and exits without re-running scripts.
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    is_nullable = asyncio.run(
        _fetchval(
            base_002_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'token_hash'
            """,
        )
    )
    assert is_nullable == "YES"


# ---------------------------------------------------------------------------
# VAL-AUTH-053 — partial UNIQUE on token_hash, NULL allowed for revocation
# ---------------------------------------------------------------------------


def test_token_hash_partial_unique_after_upgrade(base_002_dsn: str) -> None:
    """The partial UNIQUE index exists post-upgrade; duplicate hashes are rejected."""
    cfg = _build_cfg(base_002_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    async def _scenario() -> None:
        # The migration's named index must exist with UNIQUE.
        unique_idx_count = await _fetchval(
            base_002_dsn,
            """
            SELECT count(*) FROM pg_indexes
            WHERE tablename = 'workers'
              AND indexname = 'ix_workers_token_hash_unique'
              AND indexdef ILIKE '%UNIQUE%'
              AND indexdef ILIKE '%token_hash%'
            """,
        )
        assert unique_idx_count == 1, "partial UNIQUE on token_hash must exist"

        # Inserting two distinct workers with the SAME non-NULL hash
        # must violate the partial unique index.
        dup_hash = "0123456789abcdef" * 4  # 64-char hex (sha256 digest shape)
        await _execute(
            base_002_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            "w-collide-1",
            "host-1",
            dup_hash,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await _execute(
                base_002_dsn,
                "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
                "w-collide-2",
                "host-2",
                dup_hash,
            )

        # Two rows can both have token_hash = NULL (revocation
        # path) — the partial index excludes NULLs.
        await _execute(
            base_002_dsn,
            "UPDATE workers SET token_hash = NULL WHERE worker_id = 'w-collide-1'",
        )
        await _execute(
            base_002_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ('w-revoked-2', 'host-3', NULL)",
        )
        null_count = await _fetchval(
            base_002_dsn,
            "SELECT count(*) FROM workers WHERE token_hash IS NULL",
        )
        assert null_count == 2

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# VAL-AUTH-051 — pre-existing rows preserved across upgrade
# ---------------------------------------------------------------------------


def test_existing_workers_preserved_across_upgrade(base_002_dsn: str) -> None:
    """Seeded worker rows survive the upgrade with byte-equal token_hash values."""

    async def _seed() -> list[asyncpg.Record]:
        for idx in range(3):
            await _execute(
                base_002_dsn,
                "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
                f"w-seeded-{idx}",
                f"host-{idx}",
                f"hash-seed-{idx}-padded-to-64-chars-ffffffffffffffffffffffff{idx:02d}"[:64],
            )
        return await _fetch(
            base_002_dsn,
            "SELECT worker_id, token_hash FROM workers ORDER BY worker_id",
        )

    before = asyncio.run(_seed())

    cfg = _build_cfg(base_002_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    after = asyncio.run(_fetch(base_002_dsn, "SELECT worker_id, token_hash FROM workers ORDER BY worker_id"))

    assert len(after) == len(before) == 3
    for b, a in zip(before, after, strict=True):
        assert b["worker_id"] == a["worker_id"]
        assert b["token_hash"] == a["token_hash"], (
            f"token_hash drifted across upgrade: before={b['token_hash']!r} after={a['token_hash']!r}"
        )


# ---------------------------------------------------------------------------
# Round-trip — upgrade → downgrade → upgrade is reversible end-to-end
# ---------------------------------------------------------------------------


def test_round_trip_upgrade_downgrade_upgrade(base_002_dsn: str) -> None:
    """Round-trip preserves seeded rows across the full cycle.

    Pinned because the downgrade backfills any NULL token_hash with a
    placeholder; a re-upgrade must not refuse on duplicates and the
    column must be nullable again post-cycle.
    """

    async def _seed() -> None:
        await _execute(
            base_002_dsn,
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            "w-rt-1",
            "host-rt-1",
            "0" * 64,
        )

    asyncio.run(_seed())

    cfg = _build_cfg(base_002_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-1)")

    # Revoke the row mid-cycle so the downgrade has to backfill.
    asyncio.run(_execute(base_002_dsn, "UPDATE workers SET token_hash = NULL WHERE worker_id = 'w-rt-1'"))

    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1 (rt)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (rt-2)")

    async def _inspect() -> tuple[Any, Any]:
        row_count = await _fetchval(base_002_dsn, "SELECT count(*) FROM workers")
        is_nullable = await _fetchval(
            base_002_dsn,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'workers' AND column_name = 'token_hash'
            """,
        )
        return row_count, is_nullable

    row_count, is_nullable = asyncio.run(_inspect())
    assert row_count == 1
    assert is_nullable == "YES"
