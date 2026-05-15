"""Async DB repository for the ``users`` table (migration 020).

The login route layer (:mod:`whilly.api.auth_routes`) calls
:func:`verify_credentials` on every ``POST /auth/login`` and
:func:`update_last_login` after a successful match. No FastAPI / Jinja
imports here — keeps the contract identical to :mod:`whilly.api.sessions`
so tests can target this module directly via testcontainers Postgres.

Username normalisation: all lookups lower-case the input. The DB CHECK
constraint enforces ``^[a-z0-9][a-z0-9_-]{0,63}$`` so the route layer
never has to validate format separately — it can rely on
``get_user_by_username("ADMIN")`` returning the same row as
``get_user_by_username("admin")``.
"""

from __future__ import annotations

import dataclasses
import datetime

import asyncpg

from whilly.api.passwords import verify_password


@dataclasses.dataclass(frozen=True)
class User:
    """A single row from ``users`` (sans the ``password_*`` columns)."""

    username: str
    email: str | None
    role: str
    created_at: datetime.datetime
    last_login_at: datetime.datetime | None


async def get_user_by_username(pool: asyncpg.Pool, *, username: str) -> User | None:
    """Return the ``User`` for ``username`` or ``None`` if missing.

    Username comparison is case-insensitive (lower-cased before lookup).
    """
    if not isinstance(username, str) or not username:
        return None
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT username, email, role, created_at, last_login_at
            FROM users WHERE username = $1
            """,
            normalised,
        )
        if row is None:
            return None
        return User(
            username=row["username"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )


async def verify_credentials(pool: asyncpg.Pool, *, username: str, password: str) -> User | None:
    """Validate ``username``/``password`` and return the ``User`` on success.

    On any mismatch (unknown username, wrong password, malformed inputs)
    returns ``None`` — by design, the caller renders the same generic
    "invalid credentials" message regardless of which factor failed, so
    the response shape doesn't leak whether an account exists.
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if not username.strip() or not password:
        return None
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT username, email, role, created_at, last_login_at,
                   password_hash, password_salt
            FROM users WHERE username = $1
            """,
            normalised,
        )
        if row is None:
            # Run a dummy verify against a constant-time-ish hash to keep
            # timing roughly equal for "no such user" vs "wrong password".
            # Without this an attacker can distinguish the two via response
            # latency. Verify against a known-impossible salt so it always
            # returns False.
            verify_password("__dummy__", salt_hex="00" * 16, hash_hex="00" * 32)
            return None
        if not verify_password(password, salt_hex=row["password_salt"], hash_hex=row["password_hash"]):
            return None
        return User(
            username=row["username"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )


async def update_last_login(pool: asyncpg.Pool, *, username: str) -> None:
    """Touch ``users.last_login_at`` for ``username``. Best-effort; never raises."""
    if not isinstance(username, str) or not username:
        return
    normalised = username.strip().lower()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_login_at = NOW() WHERE username = $1",
                normalised,
            )
    except Exception:  # noqa: BLE001 — best-effort, must never fail the login path
        return


__all__ = [
    "User",
    "get_user_by_username",
    "update_last_login",
    "verify_credentials",
]
