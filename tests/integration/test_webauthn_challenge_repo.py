"""Integration coverage for ``whilly.api.webauthn_challenge_repo`` (migration 027).

Real testcontainers Postgres (``db_pool`` applies ``alembic upgrade head``, so
``webauthn_challenges`` exists). Pins the single-use guarantee that closes
Finding 2: a challenge is redeemable exactly once, only by the matching
user+purpose, and never after it expires.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.api import users_repo, webauthn_challenge_repo

pytestmark = DOCKER_REQUIRED

_USERNAME = "challuser"
_CHALLENGE = b"a-32-byte-server-side-challenge!"


@pytest.fixture
async def _seeded_user(db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM webauthn_challenges WHERE username = $1", _USERNAME)
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)
    await users_repo.create_user(
        db_pool, username=_USERNAME, initial_password="correct horse battery", email=None, role="admin"
    )
    yield _USERNAME
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM webauthn_challenges WHERE username = $1", _USERNAME)
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)


@pytest.mark.asyncio
async def test_create_then_consume_returns_bytes(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    cid = await webauthn_challenge_repo.create_challenge(
        db_pool, username=_USERNAME, purpose="authenticate", challenge=_CHALLENGE, ttl_seconds=300
    )
    got = await webauthn_challenge_repo.consume_challenge(
        db_pool, challenge_id=cid, username=_USERNAME, purpose="authenticate"
    )
    assert got == _CHALLENGE


@pytest.mark.asyncio
async def test_consume_is_single_use(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    cid = await webauthn_challenge_repo.create_challenge(
        db_pool, username=_USERNAME, purpose="authenticate", challenge=_CHALLENGE, ttl_seconds=300
    )
    first = await webauthn_challenge_repo.consume_challenge(
        db_pool, challenge_id=cid, username=_USERNAME, purpose="authenticate"
    )
    second = await webauthn_challenge_repo.consume_challenge(
        db_pool, challenge_id=cid, username=_USERNAME, purpose="authenticate"
    )
    assert first == _CHALLENGE
    assert second is None  # replay finds nothing


@pytest.mark.asyncio
async def test_consume_rejects_wrong_purpose_and_user(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    cid = await webauthn_challenge_repo.create_challenge(
        db_pool, username=_USERNAME, purpose="register", challenge=_CHALLENGE, ttl_seconds=300
    )
    # Wrong purpose → None (and the row survives for the correct purpose).
    assert (
        await webauthn_challenge_repo.consume_challenge(
            db_pool, challenge_id=cid, username=_USERNAME, purpose="authenticate"
        )
        is None
    )
    # Wrong user → None.
    assert (
        await webauthn_challenge_repo.consume_challenge(
            db_pool, challenge_id=cid, username="someone-else", purpose="register"
        )
        is None
    )
    # Correct purpose+user still works.
    assert (
        await webauthn_challenge_repo.consume_challenge(
            db_pool, challenge_id=cid, username=_USERNAME, purpose="register"
        )
        == _CHALLENGE
    )


@pytest.mark.asyncio
async def test_expired_challenge_not_consumable(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    cid = await webauthn_challenge_repo.create_challenge(
        db_pool, username=_USERNAME, purpose="authenticate", challenge=_CHALLENGE, ttl_seconds=-1
    )
    got = await webauthn_challenge_repo.consume_challenge(
        db_pool, challenge_id=cid, username=_USERNAME, purpose="authenticate"
    )
    assert got is None


@pytest.mark.asyncio
async def test_malformed_id_returns_none(db_pool: asyncpg.Pool) -> None:
    assert (
        await webauthn_challenge_repo.consume_challenge(
            db_pool, challenge_id="not-a-uuid", username=_USERNAME, purpose="authenticate"
        )
        is None
    )
