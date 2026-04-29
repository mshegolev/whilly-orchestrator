"""Add ``events.detail`` JSONB column for structured per-event context (TASK-104b).

The events table already carries a per-event ``payload`` JSONB column that
mixes state-machine bookkeeping (``version``, ``reason``) with
caller-supplied diagnostics. TASK-104b introduces a *separate* per-event
``detail`` JSONB column dedicated to caller diagnostics — keeping
``payload`` reserved for the canonical fields and giving downstream
consumers a stable place to pluck structured context from
(``payload->>'reason'`` for the canonical reason, ``detail->>'k'`` for
caller-supplied extras).

This migration is the prerequisite for:

* :meth:`TaskRepository.fail_task`'s ``detail`` keyword-only argument
  (VAL-TRIZ-008) — the FAIL event row stores the caller's dict in
  ``events.detail`` rather than mutating ``payload``.
* The TRIZ hook (VAL-TRIZ-001 / VAL-TRIZ-004) — ``triz.contradiction``
  and ``triz.error`` event rows put their finding shape (or error
  reason) into ``detail`` so audit-time queries don't have to special-
  case the per-event-type ``payload`` shape.

Why a *new* column rather than re-using ``payload``?
    Two observable schemas — ``payload`` carries the state-machine
    bookkeeping invariants (``version`` always present, ``reason`` for
    SKIP / FAIL / RELEASE), while ``detail`` carries free-form caller
    context. Mixing them on one column forced ``skip_task`` to merge
    payload with detail and reserve key names; with a dedicated
    column the merge is gone, the contract surface is simpler, and
    audit queries can ``WHERE detail IS NULL`` to filter "events with
    no diagnostics" without inventing a sentinel key.

Default NULL — never the literal JSON ``null`` or the empty object ``{}``
---------------------------------------------------------------------
The column is nullable with no default. A FAIL event written without a
``detail`` argument writes SQL NULL into the column (VAL-TRIZ-009);
it never writes the literal JSON ``"null"`` text or the empty object
``{}``. This is enforced at the repository layer — the SQL accepts a
``$N::jsonb`` cast and Python passes ``None`` straight through asyncpg.

Revision ID: 003_events_detail
Revises: 002_workers_status
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_events_detail"
down_revision: str | None = "002_workers_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=None,
        ),
    )


def downgrade() -> None:
    op.drop_column("events", "detail")
