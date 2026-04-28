"""Initial Whilly v4.0 schema: plans, tasks, events, workers.

Mirrors the domain dataclasses in :mod:`whilly.core.models` (TASK-004) and
the contract in PRD FR-2.1 / FR-2.4. The DDL is duplicated for human readers
in ``whilly/adapters/db/schema.sql`` — that file is documentation, this is
the source of truth that ``alembic upgrade head`` actually applies.

Design notes:

* **Status / priority** columns are plain ``TEXT`` with ``CHECK`` constraints
  rather than Postgres ``ENUM`` types. Enum types require a dedicated
  migration just to add a value, and we want headroom (FR-2.2 already lists
  six statuses; future work might add ``CANCELLED`` etc.). The ``CHECK``
  constraint preserves the closed set for now and is cheap to alter.
* **Collection-typed columns** (``dependencies``, ``key_files``, etc.) are
  ``JSONB``: round-trips a Python list cleanly through asyncpg, indexable
  if we ever need it, and avoids the array-vs-list coercion edge cases of
  ``TEXT[]``.
* **Indexes** match the access patterns specified in the task AC:
  ``tasks(plan_id, status)`` for ``next_ready`` (TASK-013c) and the
  scheduler's per-plan filtering, ``events(task_id, created_at)`` for the
  per-task audit log (TASK-009b/c), ``workers(last_heartbeat)`` for the
  visibility-timeout sweep (TASK-009d / TASK-025).
* **``tasks.claimed_by``** is a nullable foreign key with
  ``ON DELETE SET NULL``: deleting a worker (e.g. test cleanup) must not
  cascade-delete the work it had claimed; the visibility-timeout sweep will
  re-claim the rows once the FK is cleared.

Revision ID: 001_initial_schema
Revises: <none — first migration>
Create Date: 2026-04-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TASK_STATUSES = ("PENDING", "CLAIMED", "IN_PROGRESS", "DONE", "FAILED", "SKIPPED")
_TASK_PRIORITIES = ("critical", "high", "medium", "low")


def upgrade() -> None:
    # ─── workers ─────────────────────────────────────────────────────────
    # Created first so tasks.claimed_by can FK against worker_id without a
    # forward reference. Token plaintext is NEVER persisted (PRD FR-1.2);
    # only the hash lives here.
    op.create_table(
        "workers",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column(
            "last_heartbeat",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # Visibility-timeout sweep (TASK-025) scans for workers with stale
    # heartbeats; this is the supporting index.
    op.create_index("ix_workers_last_heartbeat", "workers", ["last_heartbeat"])

    # ─── plans ───────────────────────────────────────────────────────────
    op.create_table(
        "plans",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ─── tasks ───────────────────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "plan_id",
            sa.Text(),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column(
            "dependencies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "key_files",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("priority", sa.Text(), nullable=False, server_default=sa.text("'medium'")),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "acceptance_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "test_steps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("prd_requirement", sa.Text(), nullable=False, server_default=sa.text("''")),
        # Optimistic-locking counter (PRD FR-2.4). The Postgres adapter
        # (TASK-009b/c) UPDATEs ``WHERE id = $1 AND version = $2`` and
        # increments on success.
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Claim ownership / visibility-timeout fields (PRD FR-1.3, FR-1.4).
        sa.Column(
            "claimed_by",
            sa.Text(),
            sa.ForeignKey("workers.worker_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claimed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
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
        # Closed-set guards. Cheap to ALTER if we add a value later, but
        # makes invalid statuses surface immediately rather than
        # corrupting downstream state-machine logic (TASK-005).
        sa.CheckConstraint(
            f"status IN {_TASK_STATUSES}",
            name="ck_tasks_status_valid",
        ),
        sa.CheckConstraint(
            f"priority IN {_TASK_PRIORITIES}",
            name="ck_tasks_priority_valid",
        ),
        # claimed_by and claimed_at must be set together — either the row
        # is unclaimed (both NULL) or owned (both set).
        sa.CheckConstraint(
            "(claimed_by IS NULL) = (claimed_at IS NULL)",
            name="ck_tasks_claim_pair_consistent",
        ),
    )
    # next_ready filters by plan + status; this is the primary hot path.
    op.create_index("ix_tasks_plan_id_status", "tasks", ["plan_id", "status"])
    # release_stale_tasks (TASK-009d) scans by claimed_at globally; partial
    # index on the claimed-state set keeps it tiny.
    op.create_index(
        "ix_tasks_claimed_at_active",
        "tasks",
        ["claimed_at"],
        postgresql_where=sa.text("status IN ('CLAIMED', 'IN_PROGRESS')"),
    )

    # ─── events ──────────────────────────────────────────────────────────
    op.create_table(
        "events",
        # BIGSERIAL: one row per state transition is cheap, but the orchestrator
        # is meant to run for years across many plans — INTEGER would wrap.
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False, start=1),
            primary_key=True,
        ),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
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
    # Per-task audit log: dashboard (TASK-027) and tests (TASK-026) read
    # events ordered by time within a task.
    op.create_index("ix_events_task_id_created_at", "events", ["task_id", "created_at"])


def downgrade() -> None:
    # Reverse order of upgrade(); FK-bearing tables drop first.
    op.drop_index("ix_events_task_id_created_at", table_name="events")
    op.drop_table("events")

    op.drop_index("ix_tasks_claimed_at_active", table_name="tasks")
    op.drop_index("ix_tasks_plan_id_status", table_name="tasks")
    op.drop_table("tasks")

    op.drop_table("plans")

    op.drop_index("ix_workers_last_heartbeat", table_name="workers")
    op.drop_table("workers")
