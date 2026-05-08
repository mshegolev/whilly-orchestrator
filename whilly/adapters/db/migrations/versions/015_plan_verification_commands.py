"""Add plan-level verification command metadata.

Revision ID: 015_plan_verification_commands
Revises: 014_control_state
Create Date: 2026-05-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "015_plan_verification_commands"
down_revision: str | None = "014_control_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLANS_TABLE: str = "plans"
VERIFICATION_COMMANDS_COLUMN: str = "verification_commands"


def upgrade() -> None:
    op.add_column(
        PLANS_TABLE,
        sa.Column(
            VERIFICATION_COMMANDS_COLUMN,
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column(PLANS_TABLE, VERIFICATION_COMMANDS_COLUMN)
