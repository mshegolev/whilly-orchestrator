"""Database repository for magic_links and sessions tables.

PRD-wui-multi-plan v2 §6.1 mandates this module has NO FastAPI / Jinja
imports — pure asyncpg queries against the schema introduced in migration
018. The route layer (:mod:`whilly.api.auth_routes`) consumes these
functions; tests target this module directly via testcontainers Postgres
(SC-2.3).

Two contracts deserve callout:

* ``create_magic_link`` implements the "reuse recent unconsumed link"
  pattern from Architect F7. If an unconsumed, unexpired row exists with
  ``issued_at > now() - REUSE_WINDOW_SECONDS``, the existing row is
  returned and no INSERT happens. This means repeated ``POST /auth/login``
  for the same email within the reuse window does not produce a new event
  on each call — SC-2.3 verifies "exactly once per login submission".
  Within the same call the application still has the *raw* token because
  it has just been minted (the route layer mints, then asks the repo to
  store the hash; on reuse the repo signals the route to mint nothing).

* ``consume_magic_link`` is idempotent on already-consumed tokens — it
  returns ``None`` rather than raising. The route layer renders a "link
  already used" page on ``None``. This matches Frontend F2 (A5).

Functions return plain dataclasses, never DB rows. The dataclasses live
here (not in a separate ``schemas.py``) because they are not shared
across modules — the route layer immediately translates them to HTTP
responses.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
from typing import Final

import asyncpg

from whilly.api.auth_tokens import (
    DEFAULT_MAGIC_LINK_TTL_SECONDS,
    DEFAULT_SESSION_TTL_SECONDS,
    generate_session_id,
    mint_magic_link_token,
)

REUSE_WINDOW_SECONDS: Final[int] = DEFAULT_MAGIC_LINK_TTL_SECONDS // 3
"""When a fresh magic-link request comes in within this window of the
previous one, reuse the existing row. Default: 5 minutes (= 15 min TTL / 3).
"""


@dataclasses.dataclass(frozen=True)
class MagicLink:
    """A single row from ``magic_links`` plus the freshly minted raw token.

    ``raw_token`` is populated only on insert — verify-and-consume paths
    operate on the hash and never reconstruct the raw value (it has already
    been delivered to the operator).
    """

    token_hash: str
    email: str
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    consumed_at: datetime.datetime | None
    raw_token: str | None = None


@dataclasses.dataclass(frozen=True)
class Session:
    """A single row from ``sessions``."""

    session_id: str
    email: str
    created_at: datetime.datetime
    last_seen_at: datetime.datetime
    expires_at: datetime.datetime
    revoked_at: datetime.datetime | None


async def create_magic_link(
    pool: asyncpg.Pool,
    *,
    email: str,
    secret: bytes,
    ttl_seconds: int = DEFAULT_MAGIC_LINK_TTL_SECONDS,
    reuse_window_seconds: int = REUSE_WINDOW_SECONDS,
) -> MagicLink:
    """Mint or reuse a magic link for ``email``.

    Algorithm:

    1. Acquire connection; in a transaction:

       a. Delete any expired-but-unconsumed rows for this email (lazy
          cleanup so the partial unique index never blocks).
       b. Look for an unconsumed row with ``issued_at >= now() - reuse_window_seconds``.
          If found, return it unchanged — ``raw_token`` is ``None`` because
          we cannot reconstruct it from the hash. The route layer interprets
          ``raw_token is None`` as "no new link to log — operator should use
          the one we already sent."
       c. Otherwise mint a fresh ``(raw_token, token_hash)`` pair via
          :func:`mint_magic_link_token`, INSERT, and return the row with
          ``raw_token`` populated.

    The reuse path means SC-2.3 holds: rapid repeat submissions produce
    one event-log entry total.
    """
    normalised_email = email.strip().lower()
    if not normalised_email:
        raise ValueError("create_magic_link: email is empty")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM magic_links WHERE email = $1 AND consumed_at IS NULL AND expires_at <= NOW()",
                normalised_email,
            )
            existing_row = await conn.fetchrow(
                """
                SELECT token_hash, email, issued_at, expires_at, consumed_at
                FROM magic_links
                WHERE email = $1
                  AND consumed_at IS NULL
                  AND expires_at > NOW()
                  AND issued_at >= NOW() - ($2::int || ' seconds')::interval
                ORDER BY issued_at DESC
                LIMIT 1
                """,
                normalised_email,
                int(reuse_window_seconds),
            )
            if existing_row is not None:
                return _row_to_magic_link(existing_row, raw_token=None)

            raw_token, token_hash = mint_magic_link_token(secret, email=normalised_email, ttl_seconds=ttl_seconds)
            new_row = await conn.fetchrow(
                """
                INSERT INTO magic_links (token_hash, email, expires_at)
                VALUES ($1, $2, NOW() + ($3::int || ' seconds')::interval)
                RETURNING token_hash, email, issued_at, expires_at, consumed_at
                """,
                token_hash,
                normalised_email,
                int(ttl_seconds),
            )
            assert new_row is not None  # INSERT ... RETURNING guarantees a row
            return _row_to_magic_link(new_row, raw_token=raw_token)


async def consume_magic_link(pool: asyncpg.Pool, *, token_hash: str) -> MagicLink | None:
    """Mark a magic link consumed and return its row.

    Returns ``None`` when:

    * the hash is not present (forged or never-issued token);
    * the row is already consumed (replay / double-click);
    * the row has expired.

    The route layer renders ``login_consumed.html.j2`` for all ``None``
    outcomes — the operator cannot tell which case happened, by design.

    Idempotent: re-consuming a token that has already been consumed
    returns ``None`` rather than raising.
    """
    if not isinstance(token_hash, str) or not token_hash:
        raise ValueError("consume_magic_link: token_hash is empty")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE magic_links
            SET consumed_at = NOW()
            WHERE token_hash = $1
              AND consumed_at IS NULL
              AND expires_at > NOW()
            RETURNING token_hash, email, issued_at, expires_at, consumed_at
            """,
            token_hash,
        )
        if row is None:
            return None
        return _row_to_magic_link(row, raw_token=None)


async def create_session(
    pool: asyncpg.Pool,
    *,
    email: str,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> Session:
    """Create a new ``sessions`` row.

    Returns the row with a freshly-generated ``session_id``. The caller is
    responsible for minting and setting the cookie value via
    :func:`whilly.api.auth_tokens.mint_session_cookie_value` carrying this
    ``session_id``.
    """
    normalised_email = email.strip().lower()
    if not normalised_email:
        raise ValueError("create_session: email is empty")
    session_id = generate_session_id()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO sessions (session_id, email, expires_at)
            VALUES ($1, $2, NOW() + ($3::int || ' seconds')::interval)
            RETURNING session_id, email, created_at, last_seen_at, expires_at, revoked_at
            """,
            session_id,
            normalised_email,
            int(ttl_seconds),
        )
        assert row is not None
        return _row_to_session(row)


async def verify_session(pool: asyncpg.Pool, *, session_id: str) -> Session | None:
    """Look up an active session by id and bump ``last_seen_at``.

    Returns ``None`` if no row exists, the row is revoked, or it has
    expired. Otherwise updates ``last_seen_at`` and returns the row.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE sessions
            SET last_seen_at = NOW()
            WHERE session_id = $1
              AND revoked_at IS NULL
              AND expires_at > NOW()
            RETURNING session_id, email, created_at, last_seen_at, expires_at, revoked_at
            """,
            session_id,
        )
        if row is None:
            return None
        return _row_to_session(row)


async def revoke_session(pool: asyncpg.Pool, *, session_id: str) -> bool:
    """Mark a session revoked. Returns True on the first revoke, False on subsequent calls."""
    if not isinstance(session_id, str) or not session_id:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE sessions
            SET revoked_at = NOW()
            WHERE session_id = $1
              AND revoked_at IS NULL
            """,
            session_id,
        )
        # asyncpg returns "UPDATE <n>" on completion; n==0 means already revoked
        return result.endswith(" 1")


async def purge_expired(pool: asyncpg.Pool) -> tuple[int, int]:
    """Delete expired magic_links and sessions. Returns (links_deleted, sessions_deleted)."""
    async with pool.acquire() as conn:
        links_result = await conn.execute("DELETE FROM magic_links WHERE expires_at <= NOW()")
        sessions_result = await conn.execute("DELETE FROM sessions WHERE expires_at <= NOW() AND revoked_at IS NULL")
    return _parse_delete_count(links_result), _parse_delete_count(sessions_result)


def _row_to_magic_link(row: asyncpg.Record, *, raw_token: str | None) -> MagicLink:
    return MagicLink(
        token_hash=row["token_hash"],
        email=row["email"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        raw_token=raw_token,
    )


def _row_to_session(row: asyncpg.Record) -> Session:
    return Session(
        session_id=row["session_id"],
        email=row["email"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def _parse_delete_count(asyncpg_result: str) -> int:
    parts = asyncpg_result.split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


__all__ = [
    "MagicLink",
    "REUSE_WINDOW_SECONDS",
    "Session",
    "consume_magic_link",
    "create_magic_link",
    "create_session",
    "purge_expired",
    "revoke_session",
    "verify_session",
]


# Mark ``asyncio`` as intentionally imported so static analysis does not flag
# it — it is reserved for a future ``purge_expired`` background coroutine.
_ = asyncio
