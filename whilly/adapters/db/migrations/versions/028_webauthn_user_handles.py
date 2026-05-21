"""Add ``webauthn_user_handles`` table — opaque per-user WebAuthn handles (E15 Finding 3).

Security follow-up to E15. Registration used the username as the WebAuthn user
handle (``user.id``). The spec recommends an **opaque, random** handle (≤64 bytes)
because it is stored on the authenticator / passkey provider: a username is PII,
leaks identity to that provider, and is not stable across a rename. This table
holds one random 32-byte handle per user, created on first enrolment and stable
thereafter (so multiple passkeys for the same user share one handle).

Schema:

* ``username``    — TEXT PRIMARY KEY, FK → ``users(username)`` ON DELETE CASCADE.
                    One handle per user; removed when the user is deleted.
* ``user_handle`` — BYTEA NOT NULL UNIQUE. Random opaque handle used as
                    ``PublicKeyCredentialUserEntity.id`` at registration.
* ``created_at``  — TIMESTAMPTZ NOT NULL DEFAULT NOW().

Revision ID: 028_webauthn_user_handles
Revises: 027_webauthn_challenges
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "028_webauthn_user_handles"
down_revision: str | None = "027_webauthn_challenges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WEBAUTHN_USER_HANDLES_TABLE: str = "webauthn_user_handles"


def upgrade() -> None:
    op.create_table(
        WEBAUTHN_USER_HANDLES_TABLE,
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("user_handle", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("username", name="pk_webauthn_user_handles"),
        sa.ForeignKeyConstraint(
            ["username"],
            ["users.username"],
            name="fk_webauthn_user_handles_username",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_handle", name="uq_webauthn_user_handles_user_handle"),
    )


def downgrade() -> None:
    op.drop_table(WEBAUTHN_USER_HANDLES_TABLE)
