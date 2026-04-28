"""Postgres adapter for Whilly v4.0 (PRD FR-2.1, FR-2.4).

Owns the schema (Alembic migrations under :mod:`whilly.adapters.db.migrations`),
the asyncpg connection pool (TASK-009a), and the :class:`TaskRepository`
(TASK-009b/c/d). The reference DDL also lives here as ``schema.sql`` so reviewers
can read the contract without parsing migration scripts.
"""

from pathlib import Path

from whilly.adapters.db.pool import close_pool, create_pool
from whilly.adapters.db.repository import TaskRepository, VersionConflictError

# Anchor used by :mod:`whilly.adapters.db.migrations.env` to find the Alembic
# script directory regardless of the caller's working directory. Exported here
# (rather than computed inline in env.py) so test fixtures and tooling can
# import it without bootstrapping Alembic.
MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"
SCHEMA_SQL_PATH: Path = Path(__file__).parent / "schema.sql"

__all__ = [
    "MIGRATIONS_DIR",
    "SCHEMA_SQL_PATH",
    "TaskRepository",
    "VersionConflictError",
    "close_pool",
    "create_pool",
]
