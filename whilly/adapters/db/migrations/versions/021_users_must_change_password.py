"""Add must_change_password + updated_at columns; backfill bootstrap admin.

Two columns are added to ``users`` (introduced in migration 020):

* ``must_change_password BOOLEAN NOT NULL DEFAULT FALSE`` — when True the
  operator's next authenticated request is redirected to
  ``GET /auth/change-password`` before they can reach any other page.
  The bootstrap ``admin`` row is backfilled to True so the first login
  always prompts for a new password.

* ``updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`` — records the last
  time the row was touched (password change, email update, etc.).  Kept
  separate from ``last_login_at`` so the two signals can be queried
  independently.

The backfill condition targets ONLY the unmodified bootstrap row (matching
the seeded password hash from migration 020) so any operator who has
already changed the admin password does not get locked into the
change-password flow on the next deploy.

Revision ID: 021_users_must_change_password
Revises: 020_users
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "021_users_must_change_password"
down_revision: str | None = "020_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bootstrap hash from migration 020 — used as the backfill sentinel.
# Only rows still carrying this exact hash are flipped to must_change_password=TRUE.
_BOOTSTRAP_HASH: str = "4d4c0992c6a5c80417f50fe2f787961bd49b222be4c4664dbdc7544434e40df2"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # Backfill: flip only the unmodified bootstrap admin row.  Any operator who
    # has already changed the admin password has a different password_hash, so
    # this UPDATE is a strict no-op for them — no disruption on upgrade.
    op.execute(
        sa.text(
            "UPDATE users SET must_change_password = TRUE WHERE username = 'admin' AND password_hash = :bootstrap_hash"
        ).bindparams(bootstrap_hash=_BOOTSTRAP_HASH)
    )


def downgrade() -> None:
    op.drop_column("users", "updated_at")
    op.drop_column("users", "must_change_password")
