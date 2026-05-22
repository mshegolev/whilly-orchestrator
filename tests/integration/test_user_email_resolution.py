"""Integration tests for the session-identity resolver (post-E15 review, Finding 8).

The seeded admin (migration 020) has a **real** email ``admin@whilly.local`` — not
the ``<username>@local`` synthetic form. Several auth call sites used to recover
the username by ``removesuffix("@local")``, which leaves ``admin@whilly.local``
(no ``users`` row) — so the must-change gate silently bypassed enforcement for
the admin and the admin could never change its password. ``get_user_by_session_email``
is the single resolver that fixes all of them; these pins exercise it against the
real seeded row.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.api import users_repo

pytestmark = DOCKER_REQUIRED


@pytest.mark.asyncio
async def test_seeded_admin_resolves_by_real_email(db_pool: asyncpg.Pool) -> None:
    # The exact bug: a real email must resolve to the admin row (was None before).
    user = await users_repo.get_user_by_session_email(db_pool, session_email="admin@whilly.local")
    assert user is not None
    assert user.username == "admin"


@pytest.mark.asyncio
async def test_get_user_by_email_exact_match(db_pool: asyncpg.Pool) -> None:
    user = await users_repo.get_user_by_email(db_pool, email="admin@whilly.local")
    assert user is not None and user.username == "admin"


@pytest.mark.asyncio
async def test_synthetic_local_email_still_resolves_by_username(db_pool: asyncpg.Pool) -> None:
    # The <username>@local path (password users with no email set) is unchanged.
    user = await users_repo.get_user_by_session_email(db_pool, session_email="admin@local")
    assert user is not None and user.username == "admin"


@pytest.mark.asyncio
async def test_unknown_identity_returns_none(db_pool: asyncpg.Pool) -> None:
    # Magic-link-only user with no users row → None → gate fail-opens (correct).
    assert await users_repo.get_user_by_session_email(db_pool, session_email="ghost@nowhere.example") is None
    assert await users_repo.get_user_by_session_email(db_pool, session_email="ghost@local") is None
