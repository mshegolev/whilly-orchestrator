"""Integration coverage for ``webauthn_repo.get_or_create_user_handle`` (migration 028).

Real testcontainers Postgres. Pins the opaque-handle guarantee from Finding 3:
a random 32-byte handle is created once and is stable across calls, distinct per
user, and never equal to the (PII) username.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.api import users_repo, webauthn_repo

pytestmark = DOCKER_REQUIRED

_USER_A = "handleusera"
_USER_B = "handleuserb"


@pytest.fixture
async def _seeded_users(db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        for u in (_USER_A, _USER_B):
            await conn.execute("DELETE FROM webauthn_user_handles WHERE username = $1", u)
            await conn.execute("DELETE FROM users WHERE username = $1", u)
    for u in (_USER_A, _USER_B):
        await users_repo.create_user(
            db_pool, username=u, initial_password="correct horse battery", email=None, role="admin"
        )
    yield
    async with db_pool.acquire() as conn:
        for u in (_USER_A, _USER_B):
            await conn.execute("DELETE FROM webauthn_user_handles WHERE username = $1", u)
            await conn.execute("DELETE FROM users WHERE username = $1", u)


@pytest.mark.asyncio
async def test_handle_is_stable_and_opaque(db_pool: asyncpg.Pool, _seeded_users: None) -> None:
    first = await webauthn_repo.get_or_create_user_handle(db_pool, username=_USER_A)
    second = await webauthn_repo.get_or_create_user_handle(db_pool, username=_USER_A)
    assert first == second  # stable across calls
    assert len(first) == 32  # opaque, fixed-width
    assert first != _USER_A.encode("utf-8")  # not the username


@pytest.mark.asyncio
async def test_handles_differ_per_user(db_pool: asyncpg.Pool, _seeded_users: None) -> None:
    a = await webauthn_repo.get_or_create_user_handle(db_pool, username=_USER_A)
    b = await webauthn_repo.get_or_create_user_handle(db_pool, username=_USER_B)
    assert a != b
