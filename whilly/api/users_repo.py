"""Async DB repository for the ``users`` table (migrations 020–022).

The login route layer (:mod:`whilly.api.auth_routes`) calls
:func:`verify_credentials` on every ``POST /auth/login`` and
:func:`update_last_login` after a successful match. :func:`set_password`
is called by the change-password route to update the hash and clear the
``must_change_password`` flag atomically. Account lockout state
(``failed_attempts``, ``locked_until``) is managed inside
:func:`verify_credentials` — the route layer never sees "locked vs bad
password", only ``None`` in both cases (no enumeration leak). No FastAPI /
Jinja imports here — keeps the contract identical to :mod:`whilly.api.sessions`
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
import logging

import asyncpg

from whilly.api.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

#: Number of consecutive password misses before an account is locked.
_MAX_FAILED_ATTEMPTS: int = 5
#: Duration in minutes that a locked account stays locked.
_LOCKOUT_MINUTES: int = 15


@dataclasses.dataclass(frozen=True)
class User:
    """A single row from ``users`` (sans the ``password_*`` columns)."""

    username: str
    email: str | None
    role: str
    created_at: datetime.datetime
    last_login_at: datetime.datetime | None
    must_change_password: bool = False


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
            SELECT username, email, role, created_at, last_login_at, must_change_password
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
            must_change_password=bool(row["must_change_password"]),
        )


async def verify_credentials(pool: asyncpg.Pool, *, username: str, password: str) -> User | None:
    """Validate ``username``/``password`` and return the ``User`` on success.

    On any mismatch (unknown username, wrong password, locked account, or
    malformed inputs) returns ``None`` — the caller renders the same generic
    "invalid credentials" message regardless of which factor failed so the
    response shape does not leak whether an account exists or is locked.

    On password mismatch the ``failed_attempts`` counter is incremented.
    When it reaches :data:`_MAX_FAILED_ATTEMPTS` (5), ``locked_until`` is
    set to NOW() + 15 minutes and the counter is reset.  On success both
    ``failed_attempts`` and ``locked_until`` are cleared atomically alongside
    ``last_login_at`` via :func:`update_last_login`.
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
                   password_hash, password_salt, must_change_password,
                   failed_attempts, locked_until
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

        # P1.2: account lockout check.  Treat a locked account exactly like a
        # bad password — same ``None`` return, no enumeration leak.
        locked_until: datetime.datetime | None = row["locked_until"]
        if locked_until is not None:
            import datetime as _dt

            now_utc = _dt.datetime.now(_dt.timezone.utc)
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=_dt.timezone.utc)
            if now_utc < locked_until:
                logger.info(
                    "users_repo: login blocked for locked account username=%r (locked until %s)",
                    normalised,
                    locked_until.isoformat(),
                )
                verify_password("__dummy__", salt_hex="00" * 16, hash_hex="00" * 32)
                return None

        if not verify_password(password, salt_hex=row["password_salt"], hash_hex=row["password_hash"]):
            # P1.2: increment failure counter; lock if threshold reached.
            new_attempts: int = int(row["failed_attempts"]) + 1
            if new_attempts >= _MAX_FAILED_ATTEMPTS:
                await conn.execute(
                    f"""
                    UPDATE users
                       SET failed_attempts = 0,
                           locked_until    = NOW() + INTERVAL '{_LOCKOUT_MINUTES} minutes'
                     WHERE username = $1
                    """,
                    normalised,
                )
                logger.warning(
                    "users_repo: account locked for username=%r after %d failed attempts",
                    normalised,
                    new_attempts,
                )
            else:
                await conn.execute(
                    "UPDATE users SET failed_attempts = $1 WHERE username = $2",
                    new_attempts,
                    normalised,
                )
            return None

        return User(
            username=row["username"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
            must_change_password=bool(row["must_change_password"]),
        )


async def update_last_login(pool: asyncpg.Pool, *, username: str) -> None:
    """Touch ``last_login_at`` and reset lockout counters for ``username``.

    The counter reset (failed_attempts=0, locked_until=NULL) is included so a
    successful login always clears any residual lockout state atomically in a
    single round-trip.  Best-effort: never raises so the login path cannot fail
    after credentials are already validated.
    """
    if not isinstance(username, str) or not username:
        return
    normalised = username.strip().lower()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                   SET last_login_at    = NOW(),
                       failed_attempts  = 0,
                       locked_until     = NULL
                 WHERE username = $1
                """,
                normalised,
            )
    except Exception:  # noqa: BLE001 — best-effort, must never fail the login path
        return


async def set_password(pool: asyncpg.Pool, *, username: str, new_password: str) -> None:
    """Hash ``new_password`` and atomically update the users row.

    Clears ``must_change_password`` and bumps ``updated_at`` in the same
    single-round-trip UPDATE so there is no window where the password is
    changed but the flag is still set.  Raises ``ValueError`` on bad
    inputs (empty username / password).  Raises ``LookupError`` when the
    username does not exist (callers must verify the session before calling
    this).
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("set_password: username must be a non-empty string")
    if not isinstance(new_password, str) or not new_password:
        raise ValueError("set_password: new_password must be a non-empty string")
    normalised = username.strip().lower()
    salt_hex, hash_hex = hash_password(new_password)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE users
               SET password_hash        = $1,
                   password_salt        = $2,
                   must_change_password = FALSE,
                   updated_at           = NOW()
             WHERE username = $3
            """,
            hash_hex,
            salt_hex,
            normalised,
        )
    # asyncpg returns "UPDATE <count>" as a string status.
    updated_count = int((result or "UPDATE 0").split()[-1])
    if updated_count == 0:
        raise LookupError(f"set_password: no user found with username={normalised!r}")


async def list_users(pool: asyncpg.Pool) -> list[User]:
    """Return every row from ``users`` ordered by ``username`` (admin UI list)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT username, email, role, created_at, last_login_at, must_change_password FROM users ORDER BY username"
        )
    return [
        User(
            username=r["username"],
            email=r["email"],
            role=r["role"],
            created_at=r["created_at"],
            last_login_at=r["last_login_at"],
            must_change_password=bool(r["must_change_password"]),
        )
        for r in rows
    ]


async def create_user(
    pool: asyncpg.Pool,
    *,
    username: str,
    initial_password: str,
    email: str | None = None,
    role: str = "operator",
) -> None:
    """Insert a new user with ``must_change_password=TRUE``.

    Raises :class:`ValueError` if the username already exists (UniqueViolationError
    is translated for callers that don't want to depend on asyncpg's exception
    hierarchy).
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("create_user: username must be a non-empty string")
    if not isinstance(initial_password, str) or not initial_password:
        raise ValueError("create_user: initial_password must be a non-empty string")
    if role not in ("operator", "admin", "readonly"):
        raise ValueError(f"create_user: invalid role {role!r}")
    normalised = username.strip().lower()
    salt_hex, hash_hex = hash_password(initial_password)
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO users (username, password_hash, password_salt, email, role, must_change_password)
                VALUES ($1, $2, $3, $4, $5, TRUE)
                """,
                normalised,
                hash_hex,
                salt_hex,
                (email or None),
                role,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ValueError(f"create_user: username {normalised!r} already exists") from exc


async def set_role(pool: asyncpg.Pool, *, username: str, role: str) -> None:
    """Update ``users.role``. Raises :class:`LookupError` when no row matches."""
    if role not in ("operator", "admin", "readonly"):
        raise ValueError(f"set_role: invalid role {role!r}")
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET role = $1, updated_at = NOW() WHERE username = $2",
            role,
            normalised,
        )
    if int((result or "UPDATE 0").split()[-1]) == 0:
        raise LookupError(f"set_role: no user found with username={normalised!r}")


async def delete_user(pool: asyncpg.Pool, *, username: str) -> bool:
    """Drop a user row. Returns True if a row was deleted, False if absent.

    Returning a boolean (vs raising) lets the admin route give a clean 404
    on the second delete attempt without try/except gymnastics.
    """
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM users WHERE username = $1", normalised)
    return int((result or "DELETE 0").split()[-1]) > 0


async def reset_password_to_random(pool: asyncpg.Pool, *, username: str) -> str:
    """Generate a random URL-safe password, store it, set must_change_password=TRUE.

    Returns the plaintext password so the admin UI can display it ONCE for
    the operator to communicate to the user out-of-band. The user will be
    forced to change it on their next login by the must-change gate.
    """
    import secrets

    new_password = secrets.token_urlsafe(16)
    salt_hex, hash_hex = hash_password(new_password)
    normalised = username.strip().lower()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE users
               SET password_hash        = $1,
                   password_salt        = $2,
                   must_change_password = TRUE,
                   failed_attempts      = 0,
                   locked_until         = NULL,
                   updated_at           = NOW()
             WHERE username = $3
            """,
            hash_hex,
            salt_hex,
            normalised,
        )
    if int((result or "UPDATE 0").split()[-1]) == 0:
        raise LookupError(f"reset_password_to_random: no user found with username={normalised!r}")
    return new_password


__all__ = [
    "User",
    "create_user",
    "delete_user",
    "get_user_by_username",
    "list_users",
    "reset_password_to_random",
    "set_password",
    "set_role",
    "update_last_login",
    "verify_credentials",
]
