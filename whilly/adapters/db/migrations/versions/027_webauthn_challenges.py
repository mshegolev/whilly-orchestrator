"""Add ``webauthn_challenges`` table — server-side single-use WebAuthn challenges.

Security follow-up to E15 (post-merge review, Finding 2). The WebAuthn challenge
used to be carried inside the HMAC-signed pending/registration cookie. The cookie
guarantees integrity, but not *freshness*: a captured ``(cookie, assertion)`` pair
could be replayed within the cookie's 5-minute TTL, and the sign-count regression
check does not help counter-less synced passkeys (iCloud/Google), which report
``sign_count = 0`` forever.

This table makes the challenge **single-use, server-side**: ``begin`` inserts a
row keyed by a random ``challenge_id`` (the cookie now carries only that id), and
``verify``/``finish`` atomically ``DELETE … RETURNING`` it — so a second redemption
finds nothing. The table ships unconditionally (portable schema), but only the
flag-gated WebAuthn routes touch it.

Schema:

* ``id``            — BIGINT IDENTITY PK (project convention, matches 026).
* ``challenge_id``  — UUID NOT NULL UNIQUE. Random handle carried in the cookie;
                      the row is looked up + consumed by this value.
* ``username``      — TEXT NOT NULL, FK → ``users(username)`` ON DELETE CASCADE.
                      A challenge is only ever minted for a known user (an admin
                      session for register, a password-verified pending cookie
                      for authenticate), so the FK is safe and keeps the table
                      tidy when a user is deleted.
* ``purpose``       — TEXT NOT NULL CHECK IN ('register','authenticate'). Binds a
                      challenge to its ceremony so a registration challenge can't
                      be redeemed by the authentication path or vice-versa.
* ``challenge``     — BYTEA NOT NULL. The raw server-generated challenge bytes.
* ``created_at``    — TIMESTAMPTZ NOT NULL DEFAULT NOW().
* ``expires_at``    — TIMESTAMPTZ NOT NULL. Consume rejects expired rows; the repo
                      also opportunistically sweeps expired rows on insert.

Indexes:

* ``ix_webauthn_challenges_expires`` — (expires_at) for the cheap expiry sweep.

Revision ID: 027_webauthn_challenges
Revises: 026_webauthn_credentials
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "027_webauthn_challenges"
down_revision: str | None = "026_webauthn_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WEBAUTHN_CHALLENGES_TABLE: str = "webauthn_challenges"
WEBAUTHN_CHALLENGES_EXPIRES_INDEX: str = "ix_webauthn_challenges_expires"
WEBAUTHN_CHALLENGES_PURPOSE_CHECK: str = "ck_webauthn_challenges_purpose_valid"


def upgrade() -> None:
    op.create_table(
        WEBAUTHN_CHALLENGES_TABLE,
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False, start=1),
            primary_key=True,
        ),
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["username"],
            ["users.username"],
            name="fk_webauthn_challenges_username",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("challenge_id", name="uq_webauthn_challenges_challenge_id"),
        sa.CheckConstraint(
            "purpose IN ('register', 'authenticate')",
            name=WEBAUTHN_CHALLENGES_PURPOSE_CHECK,
        ),
    )
    op.create_index(
        WEBAUTHN_CHALLENGES_EXPIRES_INDEX,
        WEBAUTHN_CHALLENGES_TABLE,
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(WEBAUTHN_CHALLENGES_EXPIRES_INDEX, table_name=WEBAUTHN_CHALLENGES_TABLE)
    op.drop_table(WEBAUTHN_CHALLENGES_TABLE)
