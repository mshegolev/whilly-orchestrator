"""Integration tests for the bootstrap-token repository methods (M2 mission).

Pins the application-layer half of the M2 ``m2-migration-009-and-repo``
feature: ``mint_bootstrap_token`` / ``revoke_bootstrap_token`` /
``get_bootstrap_token_owner`` / ``list_bootstrap_tokens`` on
:class:`whilly.adapters.db.repository.TaskRepository`. Mirrors the
canonical contract assertions VAL-M2-BOOTSTRAP-REPO-001..011 and
903..906.

The fixtures (``task_repo`` + ``db_pool``) are reused from
``tests/conftest.py``; ``db_pool`` truncates the ``bootstrap_tokens``
table at fixture setup so every test starts on a clean slate.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import (
    BootstrapTokenRecord,
    TaskRepository,
    hash_bootstrap_token,
)

pytestmark = DOCKER_REQUIRED


# ---------------------------------------------------------------------------
# hash_bootstrap_token helper
# ---------------------------------------------------------------------------


def test_hash_bootstrap_token_matches_sha256_hexdigest() -> None:
    """``hash_bootstrap_token`` is plain SHA-256 over UTF-8 bytes (VAL-M2-BOOTSTRAP-REPO-001)."""
    expected = hashlib.sha256(b"plain").hexdigest()
    assert hash_bootstrap_token("plain") == expected


# ---------------------------------------------------------------------------
# mint_bootstrap_token — happy path + plaintext discipline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_bootstrap_token_stores_sha256_hash(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """``mint_bootstrap_token`` stores ``sha256(plaintext)`` and returns the hash."""
    returned_hash = await task_repo.mint_bootstrap_token(
        "plaintext-secret-001",
        "alice@example.com",
    )
    assert returned_hash == hashlib.sha256(b"plaintext-secret-001").hexdigest()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT token_hash, owner_email, expires_at, revoked_at, is_admin FROM bootstrap_tokens"
        )
    assert row is not None
    assert row["token_hash"] == returned_hash
    assert row["owner_email"] == "alice@example.com"
    assert row["expires_at"] is None
    assert row["revoked_at"] is None
    assert row["is_admin"] is False


@pytest.mark.asyncio
async def test_mint_never_persists_plaintext(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """Plaintext bytes are never written into any column (VAL-M2-BOOTSTRAP-REPO-002)."""
    plaintext = "plaintext-leak-canary"
    await task_repo.mint_bootstrap_token(plaintext, "leak@example.com")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_hash, owner_email FROM bootstrap_tokens")
    for row in rows:
        for value in (row["token_hash"], row["owner_email"]):
            assert plaintext not in value


@pytest.mark.asyncio
async def test_mint_with_expires_at_persists_value(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """``expires_at`` is forwarded to the row when provided."""
    when = datetime.now(timezone.utc) + timedelta(days=30)
    await task_repo.mint_bootstrap_token(
        "expires-soon",
        "bob@example.com",
        expires_at=when,
        is_admin=True,
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires_at, is_admin FROM bootstrap_tokens WHERE owner_email = 'bob@example.com'"
        )
    assert row is not None
    assert row["expires_at"] is not None
    assert abs((row["expires_at"] - when).total_seconds()) < 1.0
    assert row["is_admin"] is True


@pytest.mark.asyncio
async def test_mint_duplicate_plaintext_raises_unique_violation(
    task_repo: TaskRepository,
) -> None:
    """``token_hash`` PK rejects duplicate plaintext (VAL-M2-BOOTSTRAP-REPO-011)."""
    await task_repo.mint_bootstrap_token("collide", "alice@example.com")
    with pytest.raises(asyncpg.UniqueViolationError):
        await task_repo.mint_bootstrap_token("collide", "bob@example.com")


# ---------------------------------------------------------------------------
# mint_bootstrap_token — input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_plaintext", ["", "   ", "\t\n"])
async def test_mint_rejects_empty_or_whitespace_plaintext(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    bad_plaintext: str,
) -> None:
    """Empty / whitespace plaintext is rejected pre-DB (VAL-M2-BOOTSTRAP-REPO-903)."""
    with pytest.raises(ValueError, match="plaintext"):
        await task_repo.mint_bootstrap_token(bad_plaintext, "alice@example.com")
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*)::int FROM bootstrap_tokens")
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_email",
    ["", "not-an-email", "@no-local", "alice@@x", "alice", "alice@", "@example.com", "alice@example"],
)
async def test_mint_rejects_malformed_owner_email(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    bad_email: str,
) -> None:
    """Malformed ``owner_email`` is rejected pre-DB (VAL-M2-BOOTSTRAP-REPO-904)."""
    with pytest.raises(ValueError, match="owner_email"):
        await task_repo.mint_bootstrap_token("good-plaintext", bad_email)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*)::int FROM bootstrap_tokens")
    assert count == 0


# ---------------------------------------------------------------------------
# revoke_bootstrap_token — happy path + idempotency + missing rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_flips_revoked_at_to_now(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """``revoke_bootstrap_token`` stamps ``revoked_at = NOW()`` (VAL-M2-BOOTSTRAP-REPO-003)."""
    token_hash = await task_repo.mint_bootstrap_token("to-be-revoked", "alice@example.com")
    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT revoked_at FROM bootstrap_tokens WHERE token_hash = $1",
            token_hash,
        )
    assert before is None

    await task_repo.revoke_bootstrap_token(token_hash)

    async with db_pool.acquire() as conn:
        after_row = await conn.fetchrow(
            "SELECT revoked_at, NOW() AS db_now FROM bootstrap_tokens WHERE token_hash = $1",
            token_hash,
        )
    assert after_row is not None
    assert after_row["revoked_at"] is not None
    delta = abs((after_row["revoked_at"] - after_row["db_now"]).total_seconds())
    assert delta < 5.0


@pytest.mark.asyncio
async def test_revoke_is_idempotent(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """Re-revoking does not clobber the original ``revoked_at`` (VAL-M2-BOOTSTRAP-REPO-004)."""
    token_hash = await task_repo.mint_bootstrap_token("re-revoke", "alice@example.com")
    await task_repo.revoke_bootstrap_token(token_hash)
    async with db_pool.acquire() as conn:
        first = await conn.fetchval(
            "SELECT revoked_at FROM bootstrap_tokens WHERE token_hash = $1",
            token_hash,
        )

    await task_repo.revoke_bootstrap_token(token_hash)

    async with db_pool.acquire() as conn:
        second = await conn.fetchval(
            "SELECT revoked_at FROM bootstrap_tokens WHERE token_hash = $1",
            token_hash,
        )
    assert first == second


@pytest.mark.asyncio
async def test_revoke_missing_token_does_not_raise(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """Revoking an unknown ``token_hash`` is a silent no-op."""
    await task_repo.revoke_bootstrap_token("missing-hash-canary")
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*)::int FROM bootstrap_tokens")
    assert count == 0


# ---------------------------------------------------------------------------
# get_bootstrap_token_owner — active / revoked / expired / missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_owner_returns_email_and_is_admin_for_active(
    task_repo: TaskRepository,
) -> None:
    """``get_bootstrap_token_owner`` returns ``(owner_email, is_admin)`` (VAL-M2-BOOTSTRAP-REPO-005)."""
    await task_repo.mint_bootstrap_token("active-plaintext", "alice@example.com", is_admin=True)
    result = await task_repo.get_bootstrap_token_owner("active-plaintext")
    assert result == ("alice@example.com", True)


@pytest.mark.asyncio
async def test_get_owner_returns_none_for_revoked(
    task_repo: TaskRepository,
) -> None:
    """Revoked tokens lookup as ``None`` (VAL-M2-BOOTSTRAP-REPO-006)."""
    token_hash = await task_repo.mint_bootstrap_token("revoked-pt", "alice@example.com")
    await task_repo.revoke_bootstrap_token(token_hash)
    assert await task_repo.get_bootstrap_token_owner("revoked-pt") is None


@pytest.mark.asyncio
async def test_get_owner_returns_none_for_expired(
    task_repo: TaskRepository,
) -> None:
    """Expired tokens lookup as ``None`` (VAL-M2-BOOTSTRAP-REPO-007)."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await task_repo.mint_bootstrap_token(
        "expired-pt",
        "alice@example.com",
        expires_at=past,
    )
    assert await task_repo.get_bootstrap_token_owner("expired-pt") is None


@pytest.mark.asyncio
async def test_get_owner_returns_none_for_missing_hash(
    task_repo: TaskRepository,
) -> None:
    """Unknown plaintext returns ``None`` (VAL-M2-BOOTSTRAP-REPO-008)."""
    assert await task_repo.get_bootstrap_token_owner("never-minted") is None


@pytest.mark.asyncio
async def test_get_owner_returns_none_for_empty_plaintext(
    task_repo: TaskRepository,
) -> None:
    """Empty / whitespace plaintext returns ``None`` without hashing."""
    assert await task_repo.get_bootstrap_token_owner("") is None
    assert await task_repo.get_bootstrap_token_owner("   ") is None


@pytest.mark.asyncio
async def test_get_owner_with_no_expires_at_means_never_expires(
    task_repo: TaskRepository,
) -> None:
    """``expires_at=None`` means the token never expires (VAL-M2-BOOTSTRAP-REPO-001)."""
    await task_repo.mint_bootstrap_token("forever", "alice@example.com")
    assert await task_repo.get_bootstrap_token_owner("forever") == ("alice@example.com", False)


# ---------------------------------------------------------------------------
# list_bootstrap_tokens — active + include_revoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_excludes_revoked_and_expired_by_default(
    task_repo: TaskRepository,
) -> None:
    """Default ``list_bootstrap_tokens`` excludes revoked + expired (VAL-M2-BOOTSTRAP-REPO-009)."""
    # Three active rows.
    await task_repo.mint_bootstrap_token("active-1", "alice@example.com")
    await task_repo.mint_bootstrap_token("active-2", "bob@example.com")
    await task_repo.mint_bootstrap_token("active-3", "carol@example.com")
    # One revoked.
    revoked_hash = await task_repo.mint_bootstrap_token("to-revoke", "dan@example.com")
    await task_repo.revoke_bootstrap_token(revoked_hash)
    # One expired.
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await task_repo.mint_bootstrap_token("expired", "eve@example.com", expires_at=past)

    rows = await task_repo.list_bootstrap_tokens()
    owners = {r.owner_email for r in rows}
    assert owners == {"alice@example.com", "bob@example.com", "carol@example.com"}
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_list_returns_only_metadata_no_plaintext(
    task_repo: TaskRepository,
) -> None:
    """``BootstrapTokenRecord`` exposes metadata only, no plaintext (VAL-M2-BOOTSTRAP-REPO-010)."""
    await task_repo.mint_bootstrap_token("plaintext-watch", "alice@example.com")
    [record] = await task_repo.list_bootstrap_tokens()
    fields = set(asdict(record).keys())
    assert fields == {
        "token_hash",
        "owner_email",
        "created_at",
        "expires_at",
        "revoked_at",
        "is_admin",
    }
    for value in asdict(record).values():
        assert "plaintext-watch" != value
        assert not (isinstance(value, str) and "plaintext-watch" in value)


@pytest.mark.asyncio
async def test_list_with_include_revoked_returns_all_rows(
    task_repo: TaskRepository,
) -> None:
    """``include_revoked=True`` returns active + revoked + expired (VAL-M2-BOOTSTRAP-REPO-906)."""
    await task_repo.mint_bootstrap_token("alive", "alice@example.com")
    revoked_hash = await task_repo.mint_bootstrap_token("dead", "bob@example.com")
    await task_repo.revoke_bootstrap_token(revoked_hash)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    await task_repo.mint_bootstrap_token("aged", "carol@example.com", expires_at=past)

    rows = await task_repo.list_bootstrap_tokens(include_revoked=True)
    owners = {r.owner_email for r in rows}
    assert owners == {"alice@example.com", "bob@example.com", "carol@example.com"}
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_list_returns_records_sorted_newest_first(
    task_repo: TaskRepository,
) -> None:
    """``list_bootstrap_tokens`` orders rows by ``created_at DESC, token_hash``."""
    await task_repo.mint_bootstrap_token("first", "alice@example.com")
    await task_repo.mint_bootstrap_token("second", "bob@example.com")
    rows = await task_repo.list_bootstrap_tokens()
    assert len(rows) == 2
    assert rows[0].created_at >= rows[1].created_at


# ---------------------------------------------------------------------------
# Concurrency / lifecycle scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_distinct_plaintexts_for_same_owner_succeed(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """Distinct plaintexts produce distinct rows for the same owner (VAL-M2-BOOTSTRAP-REPO-901)."""
    h1 = await task_repo.mint_bootstrap_token("rotate-a", "alice@example.com")
    h2 = await task_repo.mint_bootstrap_token("rotate-b", "alice@example.com")
    assert h1 != h2
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*)::int FROM bootstrap_tokens WHERE owner_email = 'alice@example.com'"
        )
    assert count == 2
    assert await task_repo.get_bootstrap_token_owner("rotate-a") == ("alice@example.com", False)
    assert await task_repo.get_bootstrap_token_owner("rotate-b") == ("alice@example.com", False)


@pytest.mark.asyncio
async def test_lookup_record_dataclass_is_immutable() -> None:
    """``BootstrapTokenRecord`` is a frozen dataclass — mutation raises."""
    record = BootstrapTokenRecord(
        token_hash="abc",
        owner_email="alice@example.com",
        created_at=datetime.now(timezone.utc),
        expires_at=None,
        revoked_at=None,
        is_admin=False,
    )
    with pytest.raises(Exception):
        record.owner_email = "bob@example.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-013 — token-collision-by-truncation impossible
# ---------------------------------------------------------------------------


# Pre-computed offline by deterministic ascending search over plaintexts of
# the form ``whilly-prefix-collision-fixture-<n>`` (n=0,1,2,...). The pair
# below is the first collision encountered where the two SHA-256 digests
# share their leading 8 hex characters. Hard-coding the pair makes this
# test O(2 hashes) and deterministic on every run; the previous probabilistic
# rejection-sampling implementation occasionally timed out under CI load
# (see fix-m2-prefix-collision-test-flaky).
_SHARED_PREFIX_HEX_CHARS = 8
_COLLIDING_PLAINTEXT_A = "whilly-prefix-collision-fixture-57857"
_COLLIDING_PLAINTEXT_B = "whilly-prefix-collision-fixture-102435"


def test_token_collision_by_truncation_impossible() -> None:
    """VAL-M2-ADMIN-AUTH-013 — two distinct plaintexts whose digests share
    a leading 8-hex prefix still produce DISTINCT full SHA-256 digests.
    """
    t1, t2 = _COLLIDING_PLAINTEXT_A, _COLLIDING_PLAINTEXT_B
    assert t1 != t2
    h1 = hash_bootstrap_token(t1)
    h2 = hash_bootstrap_token(t2)
    assert h1[:_SHARED_PREFIX_HEX_CHARS] == h2[:_SHARED_PREFIX_HEX_CHARS], (
        "static fixture pair must share a leading 8-hex SHA-256 prefix; "
        "regenerate the fixture if hash_bootstrap_token semantics change"
    )
    assert h1 != h2, "full SHA-256 digests must differ even when the leading 8 hex chars match"


# ---------------------------------------------------------------------------
# VAL-CROSS-AUTH-006 — bootstrap revocation does NOT invalidate per-worker bearers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_revoke_does_not_invalidate_existing_per_worker_bearer(
    task_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAL-CROSS-AUTH-006 — revocation gates ``/workers/register`` only.

    Steady-state RPCs (heartbeat / claim) carry per-worker bearers
    that authenticate against ``workers.token_hash`` — not against
    the bootstrap-token row. Revoking the bootstrap therefore must
    NOT cascade onto already-issued per-worker bearers; only fresh
    cluster-join attempts (``POST /workers/register``) with the
    same bootstrap plaintext should fail 401.

    Steps:
      1. Mint a per-operator bootstrap token (admin-CLI shape).
      2. Register worker A through the bootstrap; snapshot bearer A.
      3. Revoke the bootstrap.
      4. Bearer A continues to heartbeat + claim (3 cycles each per
         the contract's ``≥3 cycles`` evidence clause).
      5. A fresh ``POST /workers/register`` with the same bootstrap
         plaintext returns 401 with the RFC 6750
         ``WWW-Authenticate: Bearer realm="whilly"`` envelope.
    """
    # Local import to keep the test-file's existing import block
    # unchanged — the rest of this module is repo-only and avoids
    # the FastAPI / httpx dependency surface.
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from whilly.adapters.transport.auth import reset_legacy_warning_state
    from whilly.adapters.transport.server import CLAIM_PATH, REGISTER_PATH, create_app

    reset_legacy_warning_state()
    # Clear any test-runner-leaked legacy env so the assertion exercises the
    # pure DB-backed auth path: a fresh register attempt with the revoked
    # plaintext must surface 401 because no row matches AND no legacy
    # fallback can rescue it.
    monkeypatch.delenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("WHILLY_WORKER_TOKEN", raising=False)

    bootstrap_plaintext = "cross-auth-006-bootstrap"
    bootstrap_hash = await task_repo.mint_bootstrap_token(
        bootstrap_plaintext,
        owner_email="alice-cross-auth-006@example.com",
    )

    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=None,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Register worker A under the still-active bootstrap.
            register_a = await client.post(
                REGISTER_PATH,
                json={"hostname": "host-cross-auth-006-a"},
                headers={"Authorization": f"Bearer {bootstrap_plaintext}"},
            )
            assert register_a.status_code == 201, register_a.text
            worker_a_id = register_a.json()["worker_id"]
            bearer_a = register_a.json()["token"]

            # Revoke the bootstrap row — flips ``revoked_at`` to NOW().
            await task_repo.revoke_bootstrap_token(bootstrap_hash)
            revoked_at = await db_pool.fetchval(
                "SELECT revoked_at FROM bootstrap_tokens WHERE token_hash = $1",
                bootstrap_hash,
            )
            assert revoked_at is not None, "revoke_bootstrap_token must stamp revoked_at"

            # (a) Bearer A continues to heartbeat + claim for ≥3 cycles
            #     post-revocation. Seed three plan rows so each claim
            #     cycle has a PENDING task to bind to.
            plan_id = "PLAN-CROSS-AUTH-006"
            cycles = 3
            for cycle_idx in range(cycles):
                hb = await client.post(
                    f"/workers/{worker_a_id}/heartbeat",
                    json={"worker_id": worker_a_id},
                    headers={"Authorization": f"Bearer {bearer_a}"},
                )
                assert hb.status_code == 200, (
                    f"cycle {cycle_idx}: heartbeat must succeed despite revoked bootstrap; got {hb.status_code} {hb.text}"
                )

                task_id = f"T-cross-auth-006-{cycle_idx}"
                async with db_pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                            plan_id,
                            f"plan-{plan_id}",
                        )
                        await conn.execute(
                            "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', 'medium')",
                            task_id,
                            plan_id,
                        )

                claim = await client.post(
                    CLAIM_PATH,
                    json={"worker_id": worker_a_id, "plan_id": plan_id},
                    headers={"Authorization": f"Bearer {bearer_a}"},
                )
                assert claim.status_code == 200, (
                    f"cycle {cycle_idx}: claim must succeed despite revoked bootstrap; got {claim.status_code} {claim.text}"
                )
                assert claim.json()["task"]["id"] == task_id

            # (b) A fresh register with the (now revoked) bootstrap
            #     plaintext is refused 401 with the RFC 6750 envelope.
            register_replay = await client.post(
                REGISTER_PATH,
                json={"hostname": "host-cross-auth-006-replay"},
                headers={"Authorization": f"Bearer {bootstrap_plaintext}"},
            )
            assert register_replay.status_code == 401, register_replay.text
            assert register_replay.headers.get("WWW-Authenticate", "").startswith('Bearer realm="whilly"')
