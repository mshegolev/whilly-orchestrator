"""Make ``workers.token_hash`` nullable + add UNIQUE on non-NULL hashes (TASK-101).

This migration is the data-layer prerequisite for the per-worker bearer
authentication flow that replaces the shared ``WHILLY_WORKER_TOKEN``
secret. Two coupled changes are made in one atomic step so the auth
edge can land cleanly:

1. **Drop NOT NULL on ``workers.token_hash``.** Operators revoke a
   compromised worker by setting its hash to NULL
   (``UPDATE workers SET token_hash = NULL WHERE worker_id = $1`` —
   per VAL-AUTH-023). The original schema (migration 001) declared the
   column NOT NULL because the registration flow always populated it
   on insert; once the bearer dep treats NULL as "revoked → 401",
   nullability is the canonical revocation signal and the constraint
   becomes counter-productive.

2. **Add a partial UNIQUE index on ``workers (token_hash)`` WHERE
   token_hash IS NOT NULL.** Per-worker bearer validation issues
   ``SELECT worker_id FROM workers WHERE token_hash = $1`` on every
   RPC; without uniqueness on the hash column two registrations
   could in principle collide and authenticate as either worker.
   The collision odds for ``secrets.token_urlsafe(32)`` (~256 bits)
   are vanishingly small, but a UNIQUE constraint:

   * makes the auth lookup deterministic at the schema level (the
     SELECT can return at most one row, so the dep doesn't have to
     reason about ambiguity);
   * surfaces a (theoretically possible) entropy-source bug as a
     loud :class:`asyncpg.UniqueViolationError` at registration
     time, rather than silently letting two workers share a row at
     read time;
   * supports the VAL-AUTH-053 contract assertion verbatim ("the
     column is nullable but uniquely constrained on non-NULL
     values").

   The index is *partial* (``WHERE token_hash IS NOT NULL``) so
   revoked rows (token_hash = NULL) don't all collide on the
   "single NULL" pseudo-value Postgres would otherwise treat as
   distinct (Postgres treats NULLs as distinct in a regular UNIQUE
   index, but a partial index with the NOT NULL predicate makes the
   intent obvious to readers and the planner).

Why a partial UNIQUE index rather than a full UNIQUE constraint?
    A full UNIQUE constraint on a nullable column accepts multiple
    NULLs in Postgres (NULLs are distinct under the standard), so it
    *would* permit revocation. The partial form is cosmetically
    identical but pins the contract loudly: "uniqueness applies to
    issued hashes only". It also makes future ``CREATE INDEX
    CONCURRENTLY`` rebuilds (if we ever shard this table) cheaper —
    revoked rows aren't part of the index footprint.

Migration numbering
-------------------
This migration is **004** because TASK-104b shipped ``003_events_detail.py``
in PR #224. Earlier mission docs refer to ``003_per_worker_bearer.py``
— that contract was rebased onto ``004`` after the milestone-1 reshuffle
(see AGENTS.md → "Migration Coordination").

Reversibility
-------------
``downgrade()`` restores the original ``NOT NULL`` constraint after
backfilling any NULL ``token_hash`` rows with a sentinel placeholder
(``'REVOKED:<worker_id>'``). The placeholder is per-row and
deliberately unique-violating safe (it embeds the PK, which is
already unique), so the partial UNIQUE index on the upgrade path
doesn't refuse to drop — though we drop the index *first* so the
ordering doesn't matter. The placeholder is a string that no
``hashlib.sha256(...).hexdigest()`` output can equal (hex digests
match ``[0-9a-f]{64}``; ``REVOKED:`` contains uppercase letters and
a colon), so a downgrade-then-re-upgrade cycle cannot accidentally
authenticate a revoked worker — even if the auth dep hashed the
literal string ``"REVOKED:<id>"`` and presented it, the comparison
would still fail because the real bearer would hash to a 64-char
hex string.

Revision ID: 004_per_worker_bearer
Revises: 003_events_detail
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_per_worker_bearer"
down_revision: str | None = "003_events_detail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Index name — exported as a module-level constant so tests can assert on
# it via ``information_schema.indexes`` / ``pg_indexes`` introspection
# without duplicating the literal at the call site (a typo would make
# the test pass against the wrong index).
TOKEN_HASH_UNIQUE_INDEX: str = "ix_workers_token_hash_unique"


def upgrade() -> None:
    """Drop NOT NULL on ``workers.token_hash`` and add the partial UNIQUE index.

    Order matters: the column relaxation happens first so the index
    creation observes the post-DDL column type. Both DDL statements
    run in the same alembic-managed transaction (Postgres supports
    transactional DDL), so a mid-migration crash leaves the schema
    untouched at revision 003.
    """
    # Step 1 — drop NOT NULL. ``existing_type=sa.Text()`` matches the
    # column's declared type from migration 001; alembic uses it to
    # re-render the ALTER COLUMN DDL without changing the type, only
    # the nullable bit.
    op.alter_column(
        "workers",
        "token_hash",
        existing_type=sa.Text(),
        nullable=True,
    )

    # Step 2 — partial UNIQUE on the non-NULL slice. ``op.create_index``
    # with ``unique=True`` + ``postgresql_where`` is the canonical
    # alembic recipe; the resulting DDL is
    # ``CREATE UNIQUE INDEX ... WHERE token_hash IS NOT NULL``.
    op.create_index(
        TOKEN_HASH_UNIQUE_INDEX,
        "workers",
        ["token_hash"],
        unique=True,
        postgresql_where="token_hash IS NOT NULL",
    )


def downgrade() -> None:
    """Restore the original ``NOT NULL`` constraint and drop the partial UNIQUE.

    Strict reversibility (VAL-AUTH-003): after ``downgrade -1``,
    ``information_schema`` reports ``is_nullable = 'NO'`` for
    ``workers.token_hash`` and the partial unique index is gone.

    Backfill strategy for NULL rows
    -------------------------------
    A revoked worker (``token_hash = NULL`` per VAL-AUTH-023) would
    block the ``ALTER COLUMN ... SET NOT NULL`` because Postgres
    refuses to install a NOT NULL constraint on a column with NULL
    rows. We backfill with ``REVOKED:<worker_id>`` so:

    * the row count is preserved (``count(*) FROM workers`` is byte-
      equal across upgrade/downgrade — pinned by VAL-AUTH-051);
    * each backfilled value is unique (PK ``worker_id`` is unique),
      so a future re-upgrade that re-creates the partial UNIQUE
      index doesn't refuse on duplicate placeholders;
    * the backfilled string is *not* a valid SHA-256 hex digest, so
      no plaintext bearer can ever authenticate against it (hex
      digests are ``[0-9a-f]{64}``; the placeholder contains an
      uppercase prefix and a colon).

    The placeholder choice is documented in the module docstring so
    operators reading the migration log understand why a row's
    ``token_hash`` is suddenly populated with a non-hash sentinel.
    """
    # Drop the partial UNIQUE index first. ``ix_workers_token_hash_unique``
    # is the upgrade-side name; using the constant guards against a
    # future rename-without-also-updating-downgrade bug.
    op.drop_index(TOKEN_HASH_UNIQUE_INDEX, table_name="workers")

    # Backfill any revoked rows with a deterministic placeholder so the
    # subsequent SET NOT NULL doesn't fail on existing NULLs. The
    # placeholder embeds the PK ``worker_id`` to keep each value
    # unique without inventing extra entropy.
    op.execute(
        """
        UPDATE workers
        SET token_hash = 'REVOKED:' || worker_id
        WHERE token_hash IS NULL
        """
    )

    # Restore NOT NULL.
    op.alter_column(
        "workers",
        "token_hash",
        existing_type=sa.Text(),
        nullable=False,
    )
