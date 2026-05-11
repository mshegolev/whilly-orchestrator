"""Add ``bootstrap_tokens`` table for per-user worker registration (M2 mission).

Adds a new ``bootstrap_tokens`` table that pins the per-user
bootstrap-auth path: ``POST /workers/register`` carrying a
``Bearer <plaintext>`` looks up the SHA-256 hash of the plaintext in
this table, returning the owning ``owner_email`` (and ``is_admin``
bit) when the row is active (``revoked_at IS NULL`` AND
(``expires_at IS NULL OR expires_at > NOW()``)). The legacy single
``WHILLY_WORKER_BOOTSTRAP_TOKEN`` env-var fallback continues to work
for one minor version (M2 → M3 transition window) per AGENTS.md.

Schema shape
------------
* ``token_hash text PRIMARY KEY`` — SHA-256 hex digest of the
  operator-issued plaintext bearer. Plaintext NEVER reaches Postgres
  (PRD NFR-3 / mirrors ``workers.token_hash`` discipline).
* ``owner_email text NOT NULL`` — operator email. Required so the
  audit log can attribute every bootstrap-issued worker registration
  to a human (M2 dashboard groups workers by owner).
* ``created_at timestamptz NOT NULL DEFAULT NOW()`` — immutable mint
  timestamp (server clock).
* ``expires_at timestamptz NULL`` — optional TTL. ``NULL`` means
  "never expires"; a non-NULL value gates lookup via
  ``expires_at > NOW()``.
* ``revoked_at timestamptz NULL`` — when a token is revoked, this
  column stamps NOW(); active tokens have ``revoked_at IS NULL``.
* ``is_admin boolean NOT NULL DEFAULT false`` — flips the bearer
  into the admin gate (``make_admin_auth`` will read this column to
  authorize ``whilly admin …`` calls in the M2 admin-CLI feature).

Why a primary-keyed hash (vs. a synthetic id + UNIQUE on hash)?
---------------------------------------------------------------
The auth lookup is keyed by the hash of the presented plaintext
(``SELECT … WHERE token_hash = $1``); a synthetic ``id`` column
would be dead weight on the read path. Making ``token_hash`` the PK
also gives us the uniqueness contract for free — VAL-M2-BOOTSTRAP-
REPO-011 ("Mint is unique on token_hash") is enforced at the
schema level rather than at the application layer.

Why a *partial* index ``WHERE revoked_at IS NULL``?
---------------------------------------------------
The per-owner listing (``list_bootstrap_tokens``) and the per-owner
lookup paths only ever care about active tokens; revoked rows are
forensic-only. The partial index footprint stays bounded to
currently-issued tokens, and the lookup planner never touches
revoked rows. Mirrors the pattern used by ``ix_workers_owner_email``
(migration 008).

Why no FK to ``workers.owner_email``?
-------------------------------------
``workers.owner_email`` is a per-row attribute (1:N: one operator
issues many workers); the FK direction would be backwards. The
operator-side identity model is "owner_email is a string we trust
the operator to type correctly"; a future ``operators`` table would
introduce an FK both columns referenced.

Reversibility
-------------
``downgrade()`` drops the partial index first, then the table.
After ``downgrade -1`` the schema is byte-equal to revision 008:
both objects are gone, ``alembic_version`` rolls back to
``008_workers_owner_email``. Pinned by the alembic full-chain test
and by ``tests/integration/test_alembic_009.py``.

Revision ID: 009_bootstrap_tokens
Revises: 008_workers_owner_email
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009_bootstrap_tokens"
down_revision: str | None = "008_workers_owner_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BOOTSTRAP_TOKENS_TABLE: str = "bootstrap_tokens"
BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX: str = "ix_bootstrap_tokens_owner_email_active"


def upgrade() -> None:
    """Create ``bootstrap_tokens`` and the per-owner active-only partial index."""
    op.create_table(
        BOOTSTRAP_TOKENS_TABLE,
        sa.Column("token_hash", sa.Text(), primary_key=True, nullable=False),
        sa.Column("owner_email", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_index(
        BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX,
        BOOTSTRAP_TOKENS_TABLE,
        ["owner_email"],
        unique=False,
        postgresql_where="revoked_at IS NULL",
    )


def downgrade() -> None:
    """Reverse the upgrade: drop the partial index, then the table.

    Strict reversibility: after ``downgrade -1`` the schema is
    byte-equal to revision 008 — both the table and the index are
    gone.
    """
    op.drop_index(BOOTSTRAP_TOKENS_OWNER_ACTIVE_INDEX, table_name=BOOTSTRAP_TOKENS_TABLE)
    op.drop_table(BOOTSTRAP_TOKENS_TABLE)
