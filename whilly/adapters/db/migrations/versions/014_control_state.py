"""Add global operator control state.

Revision ID: 014_control_state
Revises: 013_work_intents_repo_targets
Create Date: 2026-05-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "014_control_state"
down_revision: str | None = "013_work_intents_repo_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONTROL_STATE_TABLE: str = "control_state"
CONTROL_STATE_SINGLETON_CHECK: str = "ck_control_state_singleton"


def upgrade() -> None:
    op.create_table(
        CONTROL_STATE_TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("paused_by", sa.Text(), nullable=True),
        sa.Column("paused_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint("id = 'global'", name=CONTROL_STATE_SINGLETON_CHECK),
    )


def downgrade() -> None:
    op.drop_table(CONTROL_STATE_TABLE)
