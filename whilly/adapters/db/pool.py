"""asyncpg connection-pool management for Whilly v4.0 (PRD FR-2.1, NFR-1).

This module is the *only* place in the runtime that knows how to open and
close a Postgres connection pool. The control-plane app and tests both go
through :func:`create_pool` / :func:`close_pool` so DSN coercion, pool sizing
and the on-startup health check live in one place.

Why a thin wrapper around :func:`asyncpg.create_pool`?

* **Sizing from env.** ``WHILLY_DB_POOL_MIN`` / ``WHILLY_DB_POOL_MAX`` override
  the defaults (``min_size=2``, ``max_size=10``) without touching code. Defaults
  are deliberately tight — the control plane is a single async process; ten
  idle connections is plenty and two keeps the pool warm without holding
  more capacity than necessary against a single Postgres node.
* **Fail-fast health check.** asyncpg lazy-opens connections, so a bogus DSN
  or down database is invisible until the first :meth:`Pool.acquire`. We run
  ``SELECT 1`` once on startup so a misconfiguration crashes the orchestrator
  immediately instead of silently surfacing as a claim_task timeout 60s later.
* **DSN scheme normalisation.** Alembic's ``env.py`` (TASK-007) accepts both
  ``postgresql://`` and ``postgresql+asyncpg://`` — we accept the same set so
  the operator can copy ``WHILLY_DATABASE_URL`` between Alembic and the
  runtime without edits. asyncpg itself only understands plain
  ``postgresql://`` / ``postgres://``, so we strip any ``+asyncpg`` driver
  hint before handing the DSN over.

This module imports :mod:`asyncpg` and is therefore part of the adapter
layer (``whilly.adapters.*``) — never imported from ``whilly.core`` (PRD
SC-6, enforced by ``.importlinter``).
"""

from __future__ import annotations

import logging
import os

import asyncpg

__all__ = [
    "DEFAULT_POOL_MAX_SIZE",
    "DEFAULT_POOL_MIN_SIZE",
    "POOL_MAX_ENV",
    "POOL_MIN_ENV",
    "close_pool",
    "create_pool",
]

logger = logging.getLogger(__name__)

# Env-var names — exported as constants so tests can monkeypatch via the same
# symbols the runtime reads, instead of hard-coding the literal strings.
POOL_MIN_ENV: str = "WHILLY_DB_POOL_MIN"
POOL_MAX_ENV: str = "WHILLY_DB_POOL_MAX"

# PRD AC: min_size=2, max_size=10 by default. Override via env.
DEFAULT_POOL_MIN_SIZE: int = 2
DEFAULT_POOL_MAX_SIZE: int = 10


def _read_pool_size(env_name: str, default: int) -> int:
    """Parse a positive integer pool-size override from the environment.

    Empty string, missing var, non-integer or non-positive value all fall back
    to ``default`` rather than raising — mis-set env vars during local dev
    should not stop the orchestrator from booting, just log a warning.
    """
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r (not an integer); using default %d", env_name, raw, default)
        return default
    if value <= 0:
        logger.warning("Invalid %s=%d (must be > 0); using default %d", env_name, value, default)
        return default
    return value


def _normalise_dsn(dsn: str) -> str:
    """Coerce a ``postgresql+asyncpg://`` SQLAlchemy URL to plain ``postgresql://``.

    asyncpg only knows the canonical libpq schemes; the SQLAlchemy driver
    suffix is meaningful to Alembic but a syntax error to asyncpg. We accept
    both so operators can keep one DSN string in their environment.
    """
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn[len("postgresql+asyncpg://") :]
    if dsn.startswith("postgres+asyncpg://"):
        return "postgres://" + dsn[len("postgres+asyncpg://") :]
    return dsn


async def create_pool(
    dsn: str,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
) -> asyncpg.Pool:
    """Create and warm an :class:`asyncpg.Pool`, then run a ``SELECT 1`` health check.

    Sizing precedence: explicit ``min_size``/``max_size`` arguments win, then
    the ``WHILLY_DB_POOL_MIN`` / ``WHILLY_DB_POOL_MAX`` env vars, then the
    module defaults (2 / 10).

    The health check runs inside ``async with pool.acquire()`` so the
    connection is returned to the pool — a successful boot leaves the pool
    fully usable, with at least one connection already authenticated and
    ready (warm path for the very first claim_task call).

    On any failure (bad DSN, auth refusal, server down, version mismatch) the
    pool is closed before the exception propagates so a failed boot does not
    leak open sockets to Postgres.
    """
    resolved_min = min_size if min_size is not None else _read_pool_size(POOL_MIN_ENV, DEFAULT_POOL_MIN_SIZE)
    resolved_max = max_size if max_size is not None else _read_pool_size(POOL_MAX_ENV, DEFAULT_POOL_MAX_SIZE)
    if resolved_min > resolved_max:
        # asyncpg would raise a less-helpful ValueError; explain it.
        raise ValueError(f"min_size ({resolved_min}) must not exceed max_size ({resolved_max})")

    normalised_dsn = _normalise_dsn(dsn)

    logger.info(
        "Creating asyncpg pool (min_size=%d, max_size=%d) for %s",
        resolved_min,
        resolved_max,
        _redact_dsn(normalised_dsn),
    )
    pool = await asyncpg.create_pool(
        dsn=normalised_dsn,
        min_size=resolved_min,
        max_size=resolved_max,
    )
    if pool is None:  # pragma: no cover — asyncpg always returns a pool on success
        raise RuntimeError("asyncpg.create_pool returned None")

    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        if result != 1:  # pragma: no cover — defensive; SELECT 1 always returns 1
            raise RuntimeError(f"Postgres health check returned unexpected value: {result!r}")
    except BaseException:
        # Don't leak connections if the health check fails. ``close()`` is the
        # graceful path; if cancellation reaches us we still want to release
        # the underlying sockets, hence ``BaseException`` over ``Exception``.
        await pool.close()
        raise

    logger.info("asyncpg pool ready")
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    """Gracefully drain and close ``pool``.

    asyncpg's :meth:`Pool.close` waits for in-flight queries to finish before
    tearing connections down, which is exactly what we want at shutdown — the
    FastAPI ``lifespan`` hook (TASK-021a) calls this on SIGTERM so any
    handler that already started a SQL statement gets to finish.
    """
    logger.info("Closing asyncpg pool")
    await pool.close()


def _redact_dsn(dsn: str) -> str:
    """Strip the password from a libpq DSN before logging.

    A DSN like ``postgresql://user:secret@host:5432/db`` becomes
    ``postgresql://user:***@host:5432/db``. We only need this for log lines;
    the live DSN handed to asyncpg is unredacted.
    """
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    creds, host = rest.rsplit("@", 1)
    if ":" not in creds:
        return dsn
    user, _password = creds.split(":", 1)
    return f"{scheme}://{user}:***@{host}"
