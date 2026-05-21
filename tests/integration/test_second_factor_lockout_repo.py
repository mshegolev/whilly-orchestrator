"""Integration coverage for the server-side second-factor lockout helpers.

Real testcontainers Postgres (``db_pool`` applies ``alembic upgrade head``).
These two functions are the fix for the 2FA brute-force finding: the per-cookie
attempt counter was bypassable (replay an older signed ``a=0`` cookie), so the
authoritative budget now lives in the ``users`` row.

Pins:
* a fresh user is not locked;
* ``register_failed_second_factor`` increments and trips the lock at
  ``_MAX_FAILED_ATTEMPTS`` (sharing the password-path columns);
* ``update_last_login`` (the success path) clears the lock.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.api import users_repo
from whilly.api.users_repo import _MAX_FAILED_ATTEMPTS

pytestmark = DOCKER_REQUIRED

_USERNAME = "lockoutuser"


@pytest.fixture
async def _seeded_user(db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)
    await users_repo.create_user(
        db_pool, username=_USERNAME, initial_password="correct horse battery", email=None, role="admin"
    )
    yield _USERNAME
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)


@pytest.mark.asyncio
async def test_fresh_user_is_not_locked(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    assert await users_repo.is_account_locked(db_pool, username=_USERNAME) is False


@pytest.mark.asyncio
async def test_register_failures_trip_lock_at_threshold(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    # The first N-1 failures must not lock; the Nth must.
    for _ in range(_MAX_FAILED_ATTEMPTS - 1):
        locked = await users_repo.register_failed_second_factor(db_pool, username=_USERNAME)
        assert locked is False
        assert await users_repo.is_account_locked(db_pool, username=_USERNAME) is False
    locked = await users_repo.register_failed_second_factor(db_pool, username=_USERNAME)
    assert locked is True
    assert await users_repo.is_account_locked(db_pool, username=_USERNAME) is True


@pytest.mark.asyncio
async def test_successful_login_clears_lock(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    for _ in range(_MAX_FAILED_ATTEMPTS):
        await users_repo.register_failed_second_factor(db_pool, username=_USERNAME)
    assert await users_repo.is_account_locked(db_pool, username=_USERNAME) is True
    # The 2FA success path calls update_last_login, which clears the budget.
    await users_repo.update_last_login(db_pool, username=_USERNAME)
    assert await users_repo.is_account_locked(db_pool, username=_USERNAME) is False


@pytest.mark.asyncio
async def test_unknown_user_never_locks(db_pool: asyncpg.Pool) -> None:
    # Best-effort + no row → False (no crash, no enumeration signal).
    assert await users_repo.register_failed_second_factor(db_pool, username="ghost-no-such-user") is False
    assert await users_repo.is_account_locked(db_pool, username="ghost-no-such-user") is False
