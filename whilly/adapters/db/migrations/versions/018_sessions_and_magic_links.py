"""Add sessions and magic_links tables for operator-facing WUI auth.

Implements Migration 018 from PRD-wui-multi-plan v2 §6.3. Two new tables for
the magic-link login flow (Epic A): ``magic_links`` stores single-use tokens
keyed by their HMAC hash (never the raw token), ``sessions`` stores active
HTTP-only-cookie sessions. Partial unique index on ``magic_links`` enforces
the "reuse recent unconsumed link" pattern (Architect F7) so repeat
``POST /auth/login`` from the same email within ``auth_magic_link_ttl/3``
returns the same row instead of minting a new one.

Revision ID: 018_sessions_and_magic_links
Revises: 017_scheduler_rules_and_cycles
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "018_sessions_and_magic_links"
down_revision: str | None = "017_scheduler_rules_and_cycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MAGIC_LINKS_TABLE: str = "magic_links"
SESSIONS_TABLE: str = "sessions"

MAGIC_LINKS_ACTIVE_EMAIL_INDEX: str = "uq_magic_links_active_email"
MAGIC_LINKS_EMAIL_ISSUED_INDEX: str = "ix_magic_links_email_issued"
MAGIC_LINKS_EXPIRES_INDEX: str = "ix_magic_links_expires_at"
SESSIONS_ACTIVE_EMAIL_INDEX: str = "ix_sessions_email_active"
SESSIONS_EXPIRES_INDEX: str = "ix_sessions_expires_at"


def upgrade() -> None:
    op.create_table(
        MAGIC_LINKS_TABLE,
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "issued_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    # Partial unique index enforces "one unconsumed link per email" — the
    # application checks expires_at on every read (NOW() is VOLATILE and
    # rejected by Postgres in index predicates: "functions in index predicate
    # must be marked IMMUTABLE"). Expired-but-unconsumed rows are cleaned up
    # lazily by the magic_link insert path (it deletes expired siblings before
    # insert) or by a periodic reaper — see whilly/api/sessions.py.
    op.create_index(
        MAGIC_LINKS_ACTIVE_EMAIL_INDEX,
        MAGIC_LINKS_TABLE,
        ["email"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_index(
        MAGIC_LINKS_EMAIL_ISSUED_INDEX,
        MAGIC_LINKS_TABLE,
        ["email", sa.text("issued_at DESC")],
    )
    op.create_index(
        MAGIC_LINKS_EXPIRES_INDEX,
        MAGIC_LINKS_TABLE,
        ["expires_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    op.create_table(
        SESSIONS_TABLE,
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "last_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        SESSIONS_ACTIVE_EMAIL_INDEX,
        SESSIONS_TABLE,
        ["email"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        SESSIONS_EXPIRES_INDEX,
        SESSIONS_TABLE,
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(SESSIONS_EXPIRES_INDEX, table_name=SESSIONS_TABLE)
    op.drop_index(SESSIONS_ACTIVE_EMAIL_INDEX, table_name=SESSIONS_TABLE)
    op.drop_table(SESSIONS_TABLE)
    op.drop_index(MAGIC_LINKS_EXPIRES_INDEX, table_name=MAGIC_LINKS_TABLE)
    op.drop_index(MAGIC_LINKS_EMAIL_ISSUED_INDEX, table_name=MAGIC_LINKS_TABLE)
    op.drop_index(MAGIC_LINKS_ACTIVE_EMAIL_INDEX, table_name=MAGIC_LINKS_TABLE)
    op.drop_table(MAGIC_LINKS_TABLE)
