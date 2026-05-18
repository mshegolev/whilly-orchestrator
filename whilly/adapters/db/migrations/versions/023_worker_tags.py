"""Add ``tags`` to ``workers`` and ``required_tags`` to ``tasks``.

These two text[] columns enable worker-tag routing (PRD-post-auth-hardening
§Epic F, Item 18): workers advertise capabilities (e.g. ``{"docker", "gpu"}``)
and tasks declare requirements (e.g. ``{"gpu"}``). The control plane matches
``required_tags`` against ``tags`` when claiming, so a task is only handed to
a worker that has at least the declared capabilities. An empty
``required_tags`` array means "any worker" — the default for legacy tasks —
so this migration is backwards-compatible by construction.

Both columns are ``NOT NULL DEFAULT '{}'`` so existing rows backfill safely
and application code can rely on the column being a non-NULL array.

Revision ID: 023_worker_tags
Revises: 022_users_failed_login_counters
Create Date: 2026-05-18

Note on numbering: this migration is sequentially numbered 023 — the next
free slot after 022. The originating PRD pre-assigned 026 on the assumption
that E14a (024 TOTP) and E15 (025 WebAuthn) would land first. As of the F18a
merge those reservations were still unallocated, so we take the next
sequential number and let the later E-epic migrations renumber against the
current head when they land. The Alembic revision graph is a DAG over
revision IDs, not numbers, so re-numbering downstream branches is the
canonical way to resolve this.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "023_worker_tags"
down_revision: str | None = "022_users_failed_login_counters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "required_tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "required_tags")
    op.drop_column("workers", "tags")
