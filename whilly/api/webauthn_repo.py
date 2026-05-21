"""Async DB repository for the ``webauthn_credentials`` table (migration 026).

PRD-post-auth-hardening §Epic E Item 15 (E15) call-side data layer. The
WebAuthn routes (:mod:`whilly.api.webauthn_routes`) call these functions;
tests target this module directly with a real Postgres pool.

Pattern matches :mod:`whilly.api.totp_repo` — pure asyncpg, no FastAPI imports
and (deliberately) no import of the optional ``webauthn`` package: this layer
only stores/fetches opaque ``bytes`` (credential id + COSE public key) and the
signature counter, so it works whether or not the ceremony library is
installed. That keeps the schema and the data layer portable while the
protocol code stays behind the ``WHILLY_WEBAUTHN_ENABLED`` flag.

Username handling mirrors ``totp_repo``: the key is normalised to lowercase so
lookups are consistent regardless of how the caller cased it.
"""

from __future__ import annotations

import dataclasses
import datetime

import asyncpg


@dataclasses.dataclass(frozen=True)
class WebAuthnCredential:
    """A single row from ``webauthn_credentials`` (migration 026)."""

    username: str
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: list[str] | None
    created_at: datetime.datetime
    last_used_at: datetime.datetime | None


def _row_to_credential(row: asyncpg.Record) -> WebAuthnCredential:
    transports = row["transports"]
    return WebAuthnCredential(
        username=row["username"],
        credential_id=bytes(row["credential_id"]),
        public_key=bytes(row["public_key"]),
        sign_count=int(row["sign_count"]),
        transports=list(transports) if transports is not None else None,
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


async def insert_credential(
    pool: asyncpg.Pool,
    *,
    username: str,
    credential_id: bytes,
    public_key: bytes,
    sign_count: int = 0,
    transports: list[str] | None = None,
) -> None:
    """Persist a freshly-registered credential for ``username``.

    Raises :class:`asyncpg.UniqueViolationError` if ``credential_id`` is
    already enrolled (the route translates that into a user-facing "this key
    is already registered" message) and :class:`asyncpg.ForeignKeyViolationError`
    if ``username`` does not exist in ``users``.
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("insert_credential: username must be a non-empty string")
    if not credential_id:
        raise ValueError("insert_credential: credential_id must be non-empty bytes")
    if not public_key:
        raise ValueError("insert_credential: public_key must be non-empty bytes")
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO webauthn_credentials
                (username, credential_id, public_key, sign_count, transports)
            VALUES ($1, $2, $3, $4, $5)
            """,
            normalised,
            credential_id,
            public_key,
            int(sign_count),
            transports,
        )


async def get_credentials_by_username(pool: asyncpg.Pool, *, username: str) -> list[WebAuthnCredential]:
    """Return every credential enrolled for ``username`` (empty list if none).

    The begin-authentication ceremony uses this to build ``allow_credentials``;
    an empty list means the user has no passkey and the coordinator should not
    offer the WebAuthn branch.
    """
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT username, credential_id, public_key, sign_count, transports, created_at, last_used_at
            FROM webauthn_credentials
            WHERE username = $1
            ORDER BY created_at
            """,
            normalised,
        )
    return [_row_to_credential(row) for row in rows]


async def get_credential_by_id(pool: asyncpg.Pool, *, credential_id: bytes) -> WebAuthnCredential | None:
    """Look a credential up by its raw ``credential_id`` (used on assertion verify)."""
    if not credential_id:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT username, credential_id, public_key, sign_count, transports, created_at, last_used_at
            FROM webauthn_credentials
            WHERE credential_id = $1
            """,
            credential_id,
        )
    return _row_to_credential(row) if row is not None else None


async def bump_sign_count(pool: asyncpg.Pool, *, credential_id: bytes, new_sign_count: int) -> None:
    """Advance the stored signature counter and stamp ``last_used_at = NOW()``.

    Called only after a successful assertion whose returned counter has been
    verified to advance past the stored value (security gate #3). Raises
    :class:`LookupError` if the credential row vanished mid-flight.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE webauthn_credentials SET sign_count = $1, last_used_at = NOW() WHERE credential_id = $2",
            int(new_sign_count),
            credential_id,
        )
    if int((result or "UPDATE 0").split()[-1]) == 0:
        raise LookupError("bump_sign_count: no credential row for the given credential_id")


async def delete_credentials_for_user(pool: asyncpg.Pool, *, username: str) -> int:
    """Drop every passkey for ``username``. Returns the number of rows deleted.

    The admin key-loss recovery path: an admin resets the locked-out user, who
    then re-enrols. (User deletion already cascades via the FK; this is for the
    "still exists but lost the key" case.)
    """
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM webauthn_credentials WHERE username = $1", normalised)
    return int((result or "DELETE 0").split()[-1])


__all__ = [
    "WebAuthnCredential",
    "bump_sign_count",
    "delete_credentials_for_user",
    "get_credential_by_id",
    "get_credentials_by_username",
    "insert_credential",
]
