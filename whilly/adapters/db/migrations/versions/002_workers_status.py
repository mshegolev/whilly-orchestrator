"""Add ``workers.status`` for offline-worker detection (TASK-025b, PRD FR-1.4 / NFR-1 / SC-2).

The visibility-timeout sweep (TASK-025a / migration 001) reclaims orphaned
*tasks* once their claim ages past ``visibility_timeout`` (15 min default).
That's a coarse safety net: a hard-killed worker pins its claim for the
full timeout before the sweep notices. TASK-025b adds a faster, heartbeat-
driven recovery path — workers whose ``last_heartbeat`` predates a 2-minute
threshold are flipped to ``offline`` and *all* their CLAIMED / IN_PROGRESS
work is released back to PENDING in a single SQL round-trip
(:meth:`whilly.adapters.db.TaskRepository.release_offline_workers`).

Why a column instead of deriving "offline" from ``last_heartbeat`` at read
time?
    The dashboard (TASK-027) and the offline-worker sweep both want a stable
    flag they can filter on: "did we already release this worker's tasks,
    or is it still pending recovery?". Deriving the bool from
    ``last_heartbeat < NOW() - 2min`` on every read couples every consumer
    to the same threshold and forces them to redo the same datetime math —
    and worse, the *act* of marking offline is what triggers the audit
    event for the released tasks. The column captures the moment the
    sweep observed the worker as dead so the audit log can never disagree
    with the workers row.

Why not an ``ENUM`` type?
    Same rationale as the ``tasks.status`` / ``tasks.priority`` columns
    in migration 001: enum types require dedicated migrations to add a
    value, while a plain ``TEXT`` column with a ``CHECK`` constraint is
    trivially extensible (e.g. a future ``maintenance`` state for cluster
    operations). The constraint preserves the closed set today.

Default ``'online'`` for both new and existing rows
---------------------------------------------------
``server_default = 'online'`` means:

* Rows already in ``workers`` (registered before the migration) come up
  as ``online`` without a backfill — correct, because they're either
  still heartbeating (truly online) or stale (the next sweep tick will
  flip them to ``offline`` and release their work).
* Newly-registered workers (``register_worker`` doesn't pass
  ``status``) inherit the default — ``POST /workers/register`` does not
  need to be changed.

Revision ID: 002_workers_status
Revises: 001_initial_schema
Create Date: 2026-04-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_workers_status"
down_revision: str | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_WORKER_STATUSES = ("online", "offline")


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'online'"),
        ),
    )
    op.create_check_constraint(
        "ck_workers_status_valid",
        "workers",
        f"status IN {_WORKER_STATUSES}",
    )
    # Offline-worker sweep filters by ``last_heartbeat`` AND ``status !=
    # 'offline'`` so only the not-yet-flipped rows are scanned. The
    # existing ``ix_workers_last_heartbeat`` (migration 001) already covers
    # the heartbeat side; the partial index here trims it further to
    # online rows so each sweep tick reads only the live working set.
    op.create_index(
        "ix_workers_status_online_heartbeat",
        "workers",
        ["last_heartbeat"],
        postgresql_where=sa.text("status = 'online'"),
    )


def downgrade() -> None:
    op.drop_index("ix_workers_status_online_heartbeat", table_name="workers")
    op.drop_constraint("ck_workers_status_valid", "workers", type_="check")
    op.drop_column("workers", "status")
