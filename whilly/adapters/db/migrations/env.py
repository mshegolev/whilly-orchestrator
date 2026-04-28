"""Alembic env.py for Whilly v4.0 (PRD FR-2.1, FR-2.4).

The DSN is read at runtime from ``WHILLY_DATABASE_URL`` (declared in
``.env.example``, default ``postgresql://whilly:whilly@localhost:5432/whilly``).
Whatever scheme the user supplies, we coerce it to ``postgresql+asyncpg://``
because v4.0 only ships the asyncpg driver — there is no sync psycopg2 in the
runtime dep set, by design (TASK-002).

The async path is the one Alembic itself recommends for asyncpg-based
projects: open an :class:`AsyncEngine`, hand a :class:`Connection` over to
:func:`Context.configure` via ``connection.run_sync``, then run the migration
synchronously inside that callback. See Alembic's
``templates/async/env.py`` for the canonical pattern.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ─── Alembic Config object ────────────────────────────────────────────────
config = context.config

# Configure Python logging from the [loggers] section in alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# v4.0 has no SQLAlchemy ORM models — the schema lives in raw migrations and
# the runtime code uses asyncpg directly (PRD TC-4). Setting
# ``target_metadata = None`` disables ``--autogenerate`` (which would silently
# produce empty diffs), making any accidental autogen run a clear no-op
# instead of a confusing success.
target_metadata: Any = None


def _resolve_dsn() -> str:
    """Resolve the runtime DSN.

    Precedence (matches the rest of v4.0):
      1. ``WHILLY_DATABASE_URL`` env var (set by ``.env.example`` / docker-up).
      2. The placeholder in ``alembic.ini`` (only useful for ``alembic check``
         and similar offline introspection commands; pointed at a fake host so
         it never accidentally talks to a real DB).

    The URL is coerced from the bare ``postgresql://`` scheme to
    ``postgresql+asyncpg://`` so SQLAlchemy picks the asyncpg dialect. Users
    can also pass ``postgresql+asyncpg://`` explicitly and we leave it alone.
    """
    dsn = os.environ.get("WHILLY_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if dsn is None:  # pragma: no cover — defensive; alembic.ini always sets one
        raise RuntimeError("WHILLY_DATABASE_URL is unset and alembic.ini has no sqlalchemy.url")
    if dsn.startswith("postgresql://"):
        dsn = "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    elif dsn.startswith("postgres://"):  # libpq legacy alias
        dsn = "postgresql+asyncpg://" + dsn[len("postgres://") :]
    return dsn


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DB connection).

    Invoked by ``alembic upgrade head --sql > schema.sql`` and by tests that
    want to inspect the rendered DDL without a live Postgres. asyncpg is never
    contacted in this mode.
    """
    context.configure(
        url=_resolve_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Sync callback executed inside ``AsyncConnection.run_sync``.

    Alembic's migration engine is sync; we bridge by handing it an already-open
    sync :class:`Connection` view of the underlying asyncpg connection.
    """
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an :class:`AsyncEngine`, run migrations on a single connection."""
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = _resolve_dsn()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # one-shot connection — no pooling needed
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — drives the async runner."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
