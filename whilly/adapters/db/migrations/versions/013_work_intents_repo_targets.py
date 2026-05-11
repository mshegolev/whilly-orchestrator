"""Add generic work-intent and repository-target metadata.

This migration generalizes the GitHub-only Forge provenance model into
provider-neutral tables while keeping existing columns such as
``plans.github_issue_ref`` readable for compatibility.

Revision ID: 013_work_intents_repo_targets
Revises: 012_pull_requests_and_pr_events
Create Date: 2026-05-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013_work_intents_repo_targets"
down_revision: str | None = "012_pull_requests_and_pr_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WORK_INTENTS_TABLE: str = "work_intents"
PLAN_ORIGINS_TABLE: str = "plan_origins"
REPO_TARGETS_TABLE: str = "repo_targets"
PLAN_REPO_TARGETS_TABLE: str = "plan_repo_targets"
TASK_REPO_TARGETS_TABLE: str = "task_repo_targets"

WORK_INTENTS_ORIGIN_UNIQUE: str = "ix_work_intents_origin_unique"
REPO_TARGETS_PROVIDER_REPO_UNIQUE: str = "ix_repo_targets_provider_repo_unique"
PLAN_REPO_TARGETS_DEFAULT_UNIQUE: str = "ix_plan_repo_targets_default_unique"
PULL_REQUESTS_LEGACY_PLAN_PR_UNIQUE: str = "ix_pull_requests_plan_id_pr_number_unique"
PULL_REQUESTS_NULL_REPO_PLAN_PR_UNIQUE: str = "ix_pull_requests_plan_pr_null_repo_unique"
PULL_REQUESTS_PLAN_REPO_PR_UNIQUE: str = "ix_pull_requests_plan_repo_pr_unique"
PULL_REQUESTS_REPO_TARGET_FK: str = "fk_pull_requests_repo_target_id_repo_targets"


def upgrade() -> None:
    op.create_table(
        WORK_INTENTS_TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("origin_system", sa.Text(), nullable=False),
        sa.Column("origin_ref", sa.Text(), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("title", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("content_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ready'")),
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
    )
    op.create_index(
        WORK_INTENTS_ORIGIN_UNIQUE,
        WORK_INTENTS_TABLE,
        ["origin_system", "origin_ref"],
        unique=True,
    )

    op.create_table(
        PLAN_ORIGINS_TABLE,
        sa.Column(
            "plan_id",
            sa.Text(),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "work_intent_id",
            sa.Text(),
            sa.ForeignKey("work_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("prd_file", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("decomposition_mode", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("plan_id", "work_intent_id", name="pk_plan_origins"),
    )

    op.create_table(
        REPO_TARGETS_TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("clone_url", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("default_branch", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("credential_policy", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        REPO_TARGETS_PROVIDER_REPO_UNIQUE,
        REPO_TARGETS_TABLE,
        ["provider", "repo_full_name"],
        unique=True,
    )

    op.create_table(
        PLAN_REPO_TARGETS_TABLE,
        sa.Column(
            "plan_id",
            sa.Text(),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "repo_target_id",
            sa.Text(),
            sa.ForeignKey("repo_targets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("plan_id", "repo_target_id", name="pk_plan_repo_targets"),
    )
    op.create_index(
        PLAN_REPO_TARGETS_DEFAULT_UNIQUE,
        PLAN_REPO_TARGETS_TABLE,
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        TASK_REPO_TARGETS_TABLE,
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "repo_target_id",
            sa.Text(),
            sa.ForeignKey("repo_targets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("base_ref", sa.Text(), nullable=False, server_default=sa.text("''")),
    )

    op.add_column(
        "pull_requests",
        sa.Column(
            "repo_target_id",
            sa.Text(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        PULL_REQUESTS_REPO_TARGET_FK,
        "pull_requests",
        "repo_targets",
        ["repo_target_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index(PULL_REQUESTS_LEGACY_PLAN_PR_UNIQUE, table_name="pull_requests")
    op.create_index(
        PULL_REQUESTS_NULL_REPO_PLAN_PR_UNIQUE,
        "pull_requests",
        ["plan_id", "pr_number"],
        unique=True,
        postgresql_where=sa.text("repo_target_id IS NULL"),
    )
    op.create_index(
        PULL_REQUESTS_PLAN_REPO_PR_UNIQUE,
        "pull_requests",
        ["plan_id", "repo_target_id", "pr_number"],
        unique=True,
        postgresql_where=sa.text("repo_target_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(PULL_REQUESTS_PLAN_REPO_PR_UNIQUE, table_name="pull_requests")
    op.drop_index(PULL_REQUESTS_NULL_REPO_PLAN_PR_UNIQUE, table_name="pull_requests")
    op.create_index(
        PULL_REQUESTS_LEGACY_PLAN_PR_UNIQUE,
        "pull_requests",
        ["plan_id", "pr_number"],
        unique=True,
    )
    op.drop_constraint(PULL_REQUESTS_REPO_TARGET_FK, "pull_requests", type_="foreignkey")
    op.drop_column("pull_requests", "repo_target_id")
    op.drop_table(TASK_REPO_TARGETS_TABLE)
    op.drop_index(PLAN_REPO_TARGETS_DEFAULT_UNIQUE, table_name=PLAN_REPO_TARGETS_TABLE)
    op.drop_table(PLAN_REPO_TARGETS_TABLE)
    op.drop_index(REPO_TARGETS_PROVIDER_REPO_UNIQUE, table_name=REPO_TARGETS_TABLE)
    op.drop_table(REPO_TARGETS_TABLE)
    op.drop_table(PLAN_ORIGINS_TABLE)
    op.drop_index(WORK_INTENTS_ORIGIN_UNIQUE, table_name=WORK_INTENTS_TABLE)
    op.drop_table(WORK_INTENTS_TABLE)
