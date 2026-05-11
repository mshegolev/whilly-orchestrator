"""Add ``workers.owner_email`` for per-user worker attribution (M2 mission).

Adds a single nullable text column to ``workers`` that records the
email of the operator who registered the worker. Pre-existing rows
(workers registered before this migration ships) retain ``NULL`` —
the column has no server default and the migration does not
backfill. A partial index ``ix_workers_owner_email`` over the
non-NULL slice keeps per-owner lookups (planned ``GET /workers``
filtered-by-owner queries in the M2 admin dashboard) cheap without
inflating the index footprint with one entry per anonymous worker.

Why a column (vs. a join through a separate ``worker_owners`` table)?
---------------------------------------------------------------------
1. Cardinality is 1-to-1: every worker has at most one owner-email
   over its lifetime; an owner-change is in-place
   (``UPDATE workers SET owner_email = ...``), not a relation
   addition.
2. Operator-friendly: a one-line
   ``SELECT owner_email FROM workers WHERE worker_id = $1`` returns
   the value; a join would add no semantic gain.
3. Minimal: one nullable column, one partial index, no FKs, no
   defaults. Pre-existing rows remain readable through every
   existing query path.

Why a *partial* index (``WHERE owner_email IS NOT NULL``)?
-----------------------------------------------------------
The index is queried only when the admin filter is set
(``... WHERE owner_email = $1``); rows with NULL owner are never
returned by such a query, so including them in the index would
just waste pages without ever being scanned. The partial form
also makes the intent explicit at the schema level: the index
serves the per-owner lookup path, not a generic NULL-IS sweep.

Reversibility
-------------
``downgrade()`` drops the partial index first, then the column.
After ``downgrade -1`` the schema is byte-equal to revision 007:
both objects are gone, the ``alembic_version`` row points at
``007_plan_prd_file``. Pinned by the alembic full-chain test and
by ``tests/integration/test_alembic_008.py``.

Revision ID: 008_workers_owner_email
Revises: 007_plan_prd_file
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008_workers_owner_email"
down_revision: str | None = "007_plan_prd_file"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OWNER_EMAIL_PARTIAL_INDEX: str = "ix_workers_owner_email"


def upgrade() -> None:
    """Add ``workers.owner_email text NULL`` and the partial index."""
    op.add_column(
        "workers",
        sa.Column(
            "owner_email",
            sa.Text(),
            nullable=True,
            server_default=None,
        ),
    )
    op.create_index(
        OWNER_EMAIL_PARTIAL_INDEX,
        "workers",
        ["owner_email"],
        unique=False,
        postgresql_where="owner_email IS NOT NULL",
    )


def downgrade() -> None:
    """Reverse the upgrade: drop the partial index, then the column.

    Strict reversibility: after ``downgrade -1`` the schema is
    byte-equal to revision 007 — both the column and the index are
    gone.
    """
    op.drop_index(OWNER_EMAIL_PARTIAL_INDEX, table_name="workers")
    op.drop_column("workers", "owner_email")
