"""Add scheduler rules and poll cycle tracking.

Revision ID: 017_scheduler_rules_and_cycles
Revises: 016_jira_work_sessions
Create Date: 2026-05-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "017_scheduler_rules_and_cycles"
down_revision: str | None = "016_jira_work_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEDULER_RULES_TABLE: str = "scheduler_rules"
SCHEDULER_POLL_CYCLES_TABLE: str = "scheduler_poll_cycles"
SCHEDULER_POLL_CYCLES_RULE_INDEX: str = "ix_scheduler_poll_cycles_rule_created"
SCHEDULER_POLL_CYCLES_STATUS_INDEX: str = "ix_scheduler_poll_cycles_status"


def upgrade() -> None:
    op.create_table(
        SCHEDULER_RULES_TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("jira_project_key", sa.Text(), nullable=False),
        sa.Column("jql_filter", sa.Text(), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),
        sa.Column("max_results_per_poll", sa.Integer(), nullable=False, server_default=sa.text("50")),
        sa.Column(
            "deduplication_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text('\'["key","summary"]\'::jsonb'),
        ),
        sa.Column(
            "plan_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "custom_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "enabled IN (true, false)",
            name="ck_scheduler_rules_enabled_valid",
        ),
        sa.CheckConstraint(
            "poll_interval_seconds > 0",
            name="ck_scheduler_rules_poll_interval_positive",
        ),
        sa.CheckConstraint(
            "max_results_per_poll > 0",
            name="ck_scheduler_rules_max_results_positive",
        ),
    )

    op.create_table(
        SCHEDULER_POLL_CYCLES_TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "rule_id",
            sa.Text(),
            sa.ForeignKey(f"{SCHEDULER_RULES_TABLE}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("poll_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("total_issues_found", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("new_issues_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duplicate_issues_skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "jql_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "deduplicated_issues",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_plans",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "completed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "poll_status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_scheduler_poll_cycles_status_valid",
        ),
    )
    op.create_index(
        SCHEDULER_POLL_CYCLES_RULE_INDEX,
        SCHEDULER_POLL_CYCLES_TABLE,
        ["rule_id", "created_at"],
    )
    op.create_index(
        SCHEDULER_POLL_CYCLES_STATUS_INDEX,
        SCHEDULER_POLL_CYCLES_TABLE,
        ["poll_status"],
    )


def downgrade() -> None:
    op.drop_index(SCHEDULER_POLL_CYCLES_STATUS_INDEX, table_name=SCHEDULER_POLL_CYCLES_TABLE)
    op.drop_index(SCHEDULER_POLL_CYCLES_RULE_INDEX, table_name=SCHEDULER_POLL_CYCLES_TABLE)
    op.drop_table(SCHEDULER_POLL_CYCLES_TABLE)
    op.drop_table(SCHEDULER_RULES_TABLE)
