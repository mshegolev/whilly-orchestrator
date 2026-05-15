"""Add failed_attempts and locked_until columns to the users table.

These two columns drive the account-lockout logic in
:mod:`whilly.api.users_repo`: after five consecutive password misses the
account is locked for 15 minutes.  The lock is stored in the database rather
than in-process so it survives restarts and is visible across multiple
control-plane replicas.  On a successful login both columns are reset
atomically in the same UPDATE that bumps ``last_login_at`` to avoid a
window where the credentials are correct but the lock is still active.

Revision ID: 022_users_failed_login_counters
Revises: 021_users_must_change_password
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "022_users_failed_login_counters"
down_revision: str | None = "021_users_must_change_password"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_attempts")
