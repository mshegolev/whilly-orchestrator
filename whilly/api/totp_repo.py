"""Async DB repository for the ``user_totp_secrets`` table (migration 024).

PRD-post-auth-hardening §Epic E Item 14b call-side data layer. The TOTP
routes (:mod:`whilly.api.totp_routes`) call these functions; tests target
this module directly with a real Postgres pool.

Pattern matches :mod:`whilly.api.users_repo` — pure asyncpg, no FastAPI
imports, idempotent on duplicate enrolment (an operator who runs the
setup flow twice REPLACES the prior secret rather than collides on the
PK; the alternative would be a confusing "secret already enrolled"
error mid-onboarding).
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

import asyncpg

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class UserTotpSecret:
    """A single row from ``user_totp_secrets`` (migration 024)."""

    username: str
    secret: str
    enabled: bool
    created_at: datetime.datetime


async def get_totp_secret(pool: asyncpg.Pool, *, username: str) -> UserTotpSecret | None:
    """Return the user's TOTP secret row, or ``None`` if not enrolled."""
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, secret, enabled, created_at FROM user_totp_secrets WHERE username = $1",
            normalised,
        )
    if row is None:
        return None
    return UserTotpSecret(
        username=row["username"],
        secret=row["secret"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


async def upsert_totp_secret(pool: asyncpg.Pool, *, username: str, secret: str, enabled: bool = False) -> None:
    """Insert or replace the TOTP secret row for ``username``.

    Idempotent on re-enrolment: the second call wipes the prior secret
    (intentional — re-running setup is the standard "I lost my phone"
    recovery path).
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("upsert_totp_secret: username must be a non-empty string")
    if not isinstance(secret, str) or not secret:
        raise ValueError("upsert_totp_secret: secret must be a non-empty string")
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_totp_secrets (username, secret, enabled)
            VALUES ($1, $2, $3)
            ON CONFLICT (username) DO UPDATE
            SET secret = EXCLUDED.secret,
                enabled = EXCLUDED.enabled,
                created_at = NOW()
            """,
            normalised,
            secret,
            enabled,
        )


async def set_totp_enabled(pool: asyncpg.Pool, *, username: str, enabled: bool) -> None:
    """Flip the ``enabled`` flag without rotating the secret. Raises
    :class:`LookupError` when the user has no TOTP row at all."""
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE user_totp_secrets SET enabled = $1 WHERE username = $2",
            enabled,
            normalised,
        )
    if int((result or "UPDATE 0").split()[-1]) == 0:
        raise LookupError(f"set_totp_enabled: no TOTP row for username={normalised!r}")


async def delete_totp_secret(pool: asyncpg.Pool, *, username: str) -> bool:
    """Drop the TOTP enrolment entirely. Returns True iff a row was deleted."""
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM user_totp_secrets WHERE username = $1", normalised)
    return int((result or "DELETE 0").split()[-1]) > 0


__all__ = [
    "UserTotpSecret",
    "delete_totp_secret",
    "get_totp_secret",
    "set_totp_enabled",
    "upsert_totp_secret",
]
