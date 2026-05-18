"""Add ``user_totp_secrets`` table for second-factor TOTP enrolment.

Implements migration 024 from PRD-post-auth-hardening §Epic E, Item 14a
(table only — the routes ship in E14b). One TOTP secret per user, gated at
runtime behind the ``WHILLY_TOTP_ENABLED=1`` feature flag introduced in E14b;
this migration is pure DDL and reversible so it can land independently of the
flag.

Schema:

* ``username``    — primary key, foreign key to ``users(username)`` with
                    ``ON DELETE CASCADE`` so deleting a user removes their
                    secret atomically. The PRD prose says "user_id FK" but
                    the ``users`` PK introduced in migration 020 is
                    ``username TEXT`` — the column is named to match the
                    actual PK rather than the PRD's nominal label. Using
                    ``username`` as the PK of this table also encodes the
                    1:1 invariant ("each user has at most one TOTP secret")
                    directly in the schema.
* ``secret``      — base32-encoded TOTP shared secret (RFC 6238). Stored
                    plaintext in this migration; encryption-at-rest is
                    deferred to a follow-up (out of E14a scope).
* ``enabled``     — boolean, default ``FALSE``. A user can enroll (insert
                    a row) and then confirm by flipping ``enabled`` to TRUE
                    after they successfully echo back a valid TOTP code in
                    the setup ceremony from E14b.
* ``created_at``  — when the secret was minted. Used for "TOTP rotated N
                    days ago" admin tooling.

Revision ID: 024_user_totp_secrets
Revises: 023_worker_tags
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "024_user_totp_secrets"
down_revision: str | None = "023_worker_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


USER_TOTP_SECRETS_TABLE: str = "user_totp_secrets"


def upgrade() -> None:
    op.create_table(
        USER_TOTP_SECRETS_TABLE,
        sa.Column("username", sa.Text(), primary_key=True),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["username"],
            ["users.username"],
            name="fk_user_totp_secrets_username",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table(USER_TOTP_SECRETS_TABLE)
