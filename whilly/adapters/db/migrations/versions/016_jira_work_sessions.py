"""Add Jira work session and event state.

Revision ID: 016_jira_work_sessions
Revises: 015_plan_verification_commands
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "016_jira_work_sessions"
down_revision: str | None = "015_plan_verification_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JIRA_WORK_SESSIONS_TABLE: str = "jira_work_sessions"
JIRA_WORK_EVENTS_TABLE: str = "jira_work_events"
JIRA_WORK_EVENTS_ISSUE_CREATED_INDEX: str = "ix_jira_work_events_issue_created"


def upgrade() -> None:
    op.create_table(
        JIRA_WORK_SESSIONS_TABLE,
        sa.Column("issue_key", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'classified'")),
        sa.Column("work_kind", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("urgency", sa.Text(), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("readiness_verdict", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("summary_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("description_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("link_set_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("last_seen_comment_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "raw_snapshot",
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
            "work_kind IN ('', 'feature', 'bug', 'task', 'devops')",
            name="ck_jira_work_sessions_work_kind_valid",
        ),
        sa.CheckConstraint(
            "urgency IN ('normal', 'hotfix')",
            name="ck_jira_work_sessions_urgency_valid",
        ),
        sa.CheckConstraint(
            "readiness_verdict IN ('', 'ready_for_testing', 'needs_test_plan', "
            "'needs_repo_choice', 'needs_human_context', 'blocked')",
            name="ck_jira_work_sessions_readiness_valid",
        ),
    )

    op.create_table(
        JIRA_WORK_EVENTS_TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "issue_key",
            sa.Text(),
            sa.ForeignKey(f"{JIRA_WORK_SESSIONS_TABLE}.issue_key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "payload",
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
    )
    op.create_index(
        JIRA_WORK_EVENTS_ISSUE_CREATED_INDEX,
        JIRA_WORK_EVENTS_TABLE,
        ["issue_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(JIRA_WORK_EVENTS_ISSUE_CREATED_INDEX, table_name=JIRA_WORK_EVENTS_TABLE)
    op.drop_table(JIRA_WORK_EVENTS_TABLE)
    op.drop_table(JIRA_WORK_SESSIONS_TABLE)
