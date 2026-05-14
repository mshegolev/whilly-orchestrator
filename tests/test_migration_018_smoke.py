"""Migration smoke test for sessions + magic_links + plans.archived_at (Block 6).

Covers SC-5.2 from PRD-wui-multi-plan v2: migrations 018 +
019a leave the schema in a state where:

* ``magic_links`` and ``sessions`` tables exist with the columns and
  partial indexes the application reads.
* ``plans.archived_at`` and ``plans.last_event_at`` are nullable
  ``timestamptz`` columns.
* The ``uq_magic_links_active_email`` partial unique index
  (``WHERE consumed_at IS NULL``) actually rejects a second
  unconsumed row for the same email.
* The ``ix_plans_active_last_event`` partial index
  (``WHERE archived_at IS NULL``) exists with the documented
  predicate.

Why a dedicated smoke test instead of relying on the existing
``test_alembic_full_chain``?
    The full-chain test asserts the upgrade graph runs end-to-end but
    does NOT verify behavioural invariants of individual revisions.
    SC-5.2 specifically promises operators that "applying 018 +
    019a leaves the partial unique index enforcing one unconsumed
    link per email" — the only safe place to pin that is a behavioural
    INSERT test like the one below.

Uses the session-scoped ``db_pool`` fixture from ``tests/conftest.py``
which applies ``alembic upgrade head`` once, so 018 and 019a are both
already live by the time we run.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED

pytestmark = DOCKER_REQUIRED


# ─── Magic_links table shape ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_magic_links_table_has_expected_columns(db_pool: asyncpg.Pool) -> None:
    """All five columns the repository writes must exist with the right types."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'magic_links'
            ORDER BY ordinal_position
            """,
        )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    # Required non-null cols
    assert "token_hash" in cols
    assert cols["token_hash"][1] == "NO"
    assert "email" in cols
    assert cols["email"][1] == "NO"
    # timestamptz columns — Postgres reports as "timestamp with time zone"
    for ts_col in ("issued_at", "expires_at"):
        assert ts_col in cols, f"missing magic_links.{ts_col}"
        assert cols[ts_col][0] == "timestamp with time zone", cols[ts_col]
        assert cols[ts_col][1] == "NO", f"magic_links.{ts_col} should be NOT NULL"
    # consumed_at is nullable by design (NULL means "unconsumed").
    assert "consumed_at" in cols
    assert cols["consumed_at"][0] == "timestamp with time zone"
    assert cols["consumed_at"][1] == "YES"


@pytest.mark.asyncio
async def test_magic_links_indexes_present(db_pool: asyncpg.Pool) -> None:
    """The three indexes 018 declares must all be installed."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'magic_links'
            """,
        )
    names = {r["indexname"]: r["indexdef"] for r in rows}
    assert "uq_magic_links_active_email" in names, names
    assert "consumed_at IS NULL" in names["uq_magic_links_active_email"], names["uq_magic_links_active_email"]
    assert "UNIQUE" in names["uq_magic_links_active_email"].upper()
    assert "ix_magic_links_email_issued" in names
    assert "ix_magic_links_expires_at" in names
    assert "consumed_at IS NULL" in names["ix_magic_links_expires_at"]


# ─── Sessions table shape ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sessions_table_has_expected_columns(db_pool: asyncpg.Pool) -> None:
    """All six columns the repository writes must exist with the right types."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'sessions'
            ORDER BY ordinal_position
            """,
        )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert "session_id" in cols and cols["session_id"][1] == "NO"
    assert "email" in cols and cols["email"][1] == "NO"
    for ts_col in ("created_at", "last_seen_at", "expires_at"):
        assert ts_col in cols, f"missing sessions.{ts_col}"
        assert cols[ts_col][0] == "timestamp with time zone"
        assert cols[ts_col][1] == "NO"
    assert "revoked_at" in cols
    assert cols["revoked_at"][0] == "timestamp with time zone"
    assert cols["revoked_at"][1] == "YES"


@pytest.mark.asyncio
async def test_sessions_indexes_present(db_pool: asyncpg.Pool) -> None:
    """Both partial indexes (active email, active expires_at) must be installed."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'sessions'
            """,
        )
    names = {r["indexname"]: r["indexdef"] for r in rows}
    assert "ix_sessions_email_active" in names, names
    assert "revoked_at IS NULL" in names["ix_sessions_email_active"]
    assert "ix_sessions_expires_at" in names
    assert "revoked_at IS NULL" in names["ix_sessions_expires_at"]


# ─── plans.archived_at + last_event_at (019a) ───────────────────────────────


@pytest.mark.asyncio
async def test_plans_archived_at_and_last_event_at_columns(db_pool: asyncpg.Pool) -> None:
    """Both 019a columns must be timestamptz NULL on the plans table."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'plans'
              AND column_name IN ('archived_at', 'last_event_at')
            """,
        )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert "archived_at" in cols, "plans.archived_at not added by 019a"
    assert cols["archived_at"][0] == "timestamp with time zone"
    assert cols["archived_at"][1] == "YES", "archived_at must be nullable"
    assert "last_event_at" in cols, "plans.last_event_at not added by 019a"
    assert cols["last_event_at"][0] == "timestamp with time zone"
    assert cols["last_event_at"][1] == "YES", "last_event_at must be nullable"


@pytest.mark.asyncio
async def test_plans_active_last_event_index_present(db_pool: asyncpg.Pool) -> None:
    """``ix_plans_active_last_event`` must exist with ``archived_at IS NULL`` predicate."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'plans'
              AND indexname = 'ix_plans_active_last_event'
            """,
        )
    assert row is not None, "ix_plans_active_last_event missing — 019a not applied"
    indexdef = row["indexdef"]
    assert "archived_at IS NULL" in indexdef, indexdef
    assert "last_event_at" in indexdef.lower(), indexdef


# ─── Behavioural invariant: partial unique index actually rejects ───────────


@pytest.mark.asyncio
async def test_partial_unique_index_rejects_second_unconsumed_row(db_pool: asyncpg.Pool) -> None:
    """Two unconsumed rows for the same email must collide on the partial unique index.

    This is the load-bearing invariant for the "reuse recent magic link"
    pattern in :mod:`whilly.api.sessions`. If the partial unique index is
    missing the ``WHERE consumed_at IS NULL`` predicate (or is missing
    entirely), the app's reuse logic would silently fail and operators
    would receive duplicate emails. SC-5.2 promises this index is
    enforced.
    """
    email = "smoke-018@example.com"
    async with db_pool.acquire() as conn:
        # First insert: unconsumed link for the email — must succeed.
        await conn.execute(
            """
            INSERT INTO magic_links (token_hash, email, expires_at)
            VALUES ($1, $2, NOW() + INTERVAL '15 minutes')
            """,
            "smoke-hash-1",
            email,
        )
        # Second insert: another unconsumed link for the same email
        # must collide on uq_magic_links_active_email.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO magic_links (token_hash, email, expires_at)
                VALUES ($1, $2, NOW() + INTERVAL '15 minutes')
                """,
                "smoke-hash-2",
                email,
            )
        # Marking the first row consumed must free the slot — a third
        # unconsumed insert should now succeed (the index predicate
        # excludes consumed rows).
        await conn.execute(
            "UPDATE magic_links SET consumed_at = NOW() WHERE token_hash = $1",
            "smoke-hash-1",
        )
        await conn.execute(
            """
            INSERT INTO magic_links (token_hash, email, expires_at)
            VALUES ($1, $2, NOW() + INTERVAL '15 minutes')
            """,
            "smoke-hash-3",
            email,
        )
