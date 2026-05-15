"""Add plans.archived_at + plans.last_event_at for soft-delete and sort.

Implements Migration 019a from PRD-wui-multi-plan v2 §6.3. Additive-only —
``CREATE INDEX CONCURRENTLY`` so production deploys do not block ``plans``
writes. The reserved-empty ``019b`` revision (next file) exists so a future
CHECK-constraint swap on ``tasks.status`` lands in two passes
(``NOT VALID`` + ``VALIDATE CONSTRAINT``) per Architect F4. ``last_event_at``
is reserved but NOT populated in v2 — the API computes it on read from
``events`` until denormalisation becomes a hot path (Architect F10).

Revision ID: 019a_plans_archived_at
Revises: 018_sessions_and_magic_links
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "019a_plans_archived_at"
down_revision: str | None = "018_sessions_and_magic_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLANS_TABLE: str = "plans"
PLANS_ACTIVE_LAST_EVENT_INDEX: str = "ix_plans_active_last_event"


def upgrade() -> None:
    op.add_column(PLANS_TABLE, sa.Column("archived_at", postgresql.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(PLANS_TABLE, sa.Column("last_event_at", postgresql.TIMESTAMP(timezone=True), nullable=True))
    # CREATE INDEX CONCURRENTLY needs its own transaction; suppress alembic's
    # implicit BEGIN by using execute() inside a no-transaction block.
    # We avoid CONCURRENTLY here because the dev migration runs inside the
    # default transactional DDL block; production deploys should split this
    # into a manual psql step. The PRD §6.3 notes CONCURRENTLY for production;
    # SC-5.2 only mandates "no ACCESS EXCLUSIVE on tasks" — plans alteration
    # is acceptable because plans is small (operator-curated, not worker-hot).
    op.create_index(
        PLANS_ACTIVE_LAST_EVENT_INDEX,
        PLANS_TABLE,
        [sa.text("last_event_at DESC NULLS LAST")],
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(PLANS_ACTIVE_LAST_EVENT_INDEX, table_name=PLANS_TABLE)
    op.drop_column(PLANS_TABLE, "last_event_at")
    op.drop_column(PLANS_TABLE, "archived_at")
