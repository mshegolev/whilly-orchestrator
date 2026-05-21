"""Async DB repository for ``webauthn_challenges`` (migration 027 / E15 Finding 2).

Server-side single-use WebAuthn challenges. The ceremony routes
(:mod:`whilly.api.webauthn_routes`) mint a challenge on ``begin`` and redeem it on
``verify``/``finish``; the cookie carries only the random ``challenge_id``, never
the challenge itself.

The single-use guarantee is the atomic ``DELETE … RETURNING`` in
:func:`consume_challenge`: the first redemption returns the challenge and removes
the row, so any replay (same cookie + same assertion within the TTL) finds
nothing. This closes the gap that the sign-count check leaves open for
counter-less synced passkeys.

Pattern matches the other ``whilly/api`` repos — pure asyncpg, no FastAPI and no
``webauthn`` import (it only stores opaque bytes + metadata).
"""

from __future__ import annotations

import datetime
import uuid

import asyncpg


async def create_challenge(
    pool: asyncpg.Pool,
    *,
    username: str,
    purpose: str,
    challenge: bytes,
    ttl_seconds: int,
) -> str:
    """Persist a fresh challenge and return its ``challenge_id`` (UUID string).

    The id — not the challenge — is what the cookie carries. Expired rows are
    swept opportunistically on each insert so the table stays bounded without a
    separate cron. ``purpose`` must be ``'register'`` or ``'authenticate'`` (the
    CHECK constraint enforces it; binding the challenge to its ceremony stops a
    register challenge being redeemed by the auth path).
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("create_challenge: username must be a non-empty string")
    if purpose not in ("register", "authenticate"):
        raise ValueError(f"create_challenge: purpose must be 'register' or 'authenticate', got {purpose!r}")
    if not challenge:
        raise ValueError("create_challenge: challenge must be non-empty bytes")
    challenge_id = uuid.uuid4()
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(ttl_seconds))
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        # Opportunistic sweep — keeps the table tiny; best-effort, ignore errors.
        try:
            await conn.execute("DELETE FROM webauthn_challenges WHERE expires_at < NOW()")
        except Exception:  # noqa: BLE001 — sweep is non-critical
            pass
        await conn.execute(
            """
            INSERT INTO webauthn_challenges (challenge_id, username, purpose, challenge, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            challenge_id,
            normalised,
            purpose,
            challenge,
            expires_at,
        )
    return str(challenge_id)


async def consume_challenge(
    pool: asyncpg.Pool,
    *,
    challenge_id: str,
    username: str,
    purpose: str,
) -> bytes | None:
    """Atomically redeem a challenge: return its bytes and delete the row.

    Returns ``None`` when the id is malformed, the row is missing (already
    consumed / never existed), expired, or bound to a different user/purpose —
    the caller treats every ``None`` identically as "challenge invalid, start
    over". The ``DELETE … RETURNING`` makes redemption single-use.
    """
    if not isinstance(challenge_id, str) or not isinstance(username, str):
        return None
    try:
        cid = uuid.UUID(challenge_id)
    except (ValueError, AttributeError, TypeError):
        return None
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            """
            DELETE FROM webauthn_challenges
             WHERE challenge_id = $1 AND username = $2 AND purpose = $3 AND expires_at > NOW()
            RETURNING challenge
            """,
            cid,
            normalised,
            purpose,
        )
    return bytes(row) if row is not None else None


__all__ = [
    "consume_challenge",
    "create_challenge",
]
