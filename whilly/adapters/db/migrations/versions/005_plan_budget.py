"""Add per-plan budget columns + plan-level events (TASK-102).

This migration is the data-layer prerequisite for the *plan budget guard*
introduced by TASK-102. Three coupled changes are made in a single
atomic step so the budget edge can land cleanly:

1. **Add ``plans.budget_usd numeric(10,4) NULL``** — operator-supplied
   spend cap in USD with sub-cent precision (NUMERIC arithmetic so 100
   increments of ``0.0123`` accumulate to exactly ``1.23`` without float
   drift; see VAL-BUDGET-033). ``NULL`` is the documented "unlimited"
   value (VAL-BUDGET-011 / VAL-BUDGET-020 / VAL-BUDGET-042) — operators
   omit ``--budget`` on ``whilly plan create`` to opt out of the guard.
   Numeric ``(10, 4)`` accommodates spend caps up to ``999_999.9999``
   USD which is multiple orders of magnitude above the realistic
   per-plan budget (typical Claude run ≪ $100); the precision is what
   matters for the strict-< gate (VAL-BUDGET-023).

2. **Add ``plans.spent_usd numeric(10,4) NOT NULL DEFAULT 0``** —
   running total of completed-task ``cost_usd`` for the plan. The
   non-null default lets pre-existing plans (created before this
   migration) come up with a clean ``0`` baseline without a backfill
   error (VAL-BUDGET-003). Strict monotonic non-decrease is enforced
   at the *repository* layer, not via a CHECK constraint, because:

   * the only writer that increments ``spent_usd`` is
     :meth:`TaskRepository.complete_task` (single SQL statement,
     guarded by the optimistic-lock filter on ``tasks``); no path
     decrements it;
   * a CHECK constraint on the column itself can only assert "value
     >= 0" — not "no decrement vs prior value", which would require a
     trigger and a self-join. The repository contract is the right
     place for the monotonicity invariant (VAL-BUDGET-072).

3. **Add ``events.plan_id text NULL`` and relax ``events.task_id NOT
   NULL`` to nullable.** The plan-level sentinel event
   ``plan.budget_exceeded`` (VAL-BUDGET-040 / 041 / 042 / 043) is
   emitted with ``task_id IS NULL`` and a populated ``plan_id`` —
   neither column is sufficient on its own. The original
   ``events.task_id NOT NULL`` constraint must be relaxed because
   the sentinel has no single triggering task (it observes the
   *crossing*, not the *task*); the new ``plan_id`` column carries
   the plan reference for the sentinel and stays ``NULL`` for the
   per-task events that already populate ``task_id``.

   A FK from ``events.plan_id`` to ``plans.id`` is intentionally
   omitted: with ``ON DELETE CASCADE`` on ``tasks.plan_id`` the
   sentinel event would be deleted with the parent plan, which is
   the desired clean-up. Adding a FK on ``events.plan_id`` would
   require ``ON DELETE CASCADE`` here too, which is exactly what we
   want — we add it so a hard ``DELETE FROM plans WHERE id = $1``
   wipes the sentinel rows along with the parent plan and its
   tasks. (See ``schema.sql`` / migration 001 for the existing FK
   chain ``plans → tasks → events``.)

Migration numbering
-------------------
This migration is **005** because TASK-101 shipped
``004_per_worker_bearer.py`` immediately before this one
(``down_revision = "004_per_worker_bearer"``). The validation contract
text predates the rebase and refers to the migration as ``004`` — the
numbering shifted at mission-coordination time (see
``AGENTS.md → Migration Coordination``). The schema delta the contract
asserts is unchanged; only the file name moved.

Reversibility
-------------
``downgrade()`` reverses all three changes in the inverse order:

1. Re-imposes ``events.task_id NOT NULL``. Any plan-level sentinel
   events (``task_id IS NULL``) created while at revision 005 are
   *deleted* before re-imposing the constraint — they have no
   surviving wire-format under the pre-005 schema (no ``plan_id``
   column to receive their reference) and a backfill of ``task_id``
   to a sentinel would corrupt the audit log's reading-by-task
   semantics. This is the same trade-off
   ``004_per_worker_bearer.py`` documents for the revoked-token
   placeholder: the downgrade path discards data that has no
   pre-revision shape.
2. Drops ``events.plan_id``.
3. Drops ``plans.spent_usd`` and ``plans.budget_usd``.

After ``downgrade -1`` the ``alembic_version`` row points at
``004_per_worker_bearer`` and the schema is byte-equal to the
pre-005 layout — pinned by VAL-BUDGET-004 / VAL-BUDGET-070.

Revision ID: 005_plan_budget
Revises: 004_per_worker_bearer
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_plan_budget"
down_revision: str | None = "004_per_worker_bearer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``plans.budget_usd`` / ``plans.spent_usd`` and ``events.plan_id``.

    Order matters minimally: the three column adds are independent (no
    column references another), and the ``ALTER COLUMN events.task_id
    DROP NOT NULL`` only depends on the column already existing (it does
    — installed by migration 001). All four DDL statements run inside
    the alembic-managed transaction (Postgres supports transactional
    DDL), so a mid-migration crash leaves the schema untouched at
    revision 004.
    """
    # Step 1 — add ``plans.budget_usd``. NULL = unlimited (per the AC).
    # ``numeric(10, 4)`` matches ``plans.spent_usd`` precision so the
    # strict-< comparison ``spent_usd < budget_usd`` in
    # :data:`whilly.adapters.db.repository._CLAIM_SQL` doesn't suffer
    # implicit-cast surprises.
    op.add_column(
        "plans",
        sa.Column(
            "budget_usd",
            sa.Numeric(precision=10, scale=4),
            nullable=True,
            server_default=None,
        ),
    )

    # Step 2 — add ``plans.spent_usd``. NOT NULL with a server default
    # of ``0`` so existing plans (pre-005) come up with a clean
    # baseline. ``server_default=sa.text("0")`` (text, not Python
    # ``0``) so alembic emits ``DEFAULT 0`` in the DDL — Postgres then
    # owns the default for any future INSERTs that omit the column.
    op.add_column(
        "plans",
        sa.Column(
            "spent_usd",
            sa.Numeric(precision=10, scale=4),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # Step 3 — add ``events.plan_id``. Nullable: per-task events keep
    # ``plan_id IS NULL`` (their plan reference is on the parent
    # ``tasks.plan_id``); only the plan-level sentinel
    # (``plan.budget_exceeded``) populates this column. The FK with
    # ``ON DELETE CASCADE`` mirrors the existing ``tasks.plan_id``
    # FK so a ``DELETE FROM plans`` wipes the sentinel rows
    # alongside the parent plan + tasks (idempotent ``plan reset
    # --hard``).
    op.add_column(
        "events",
        sa.Column(
            "plan_id",
            sa.Text(),
            nullable=True,
            server_default=None,
        ),
    )
    op.create_foreign_key(
        "fk_events_plan_id_plans",
        "events",
        "plans",
        ["plan_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Step 4 — relax ``events.task_id NOT NULL``. The plan-level
    # sentinel writes ``task_id IS NULL``, which the original
    # constraint would refuse. The FK on ``events.task_id`` (with
    # ``ON DELETE CASCADE``) is preserved — only the nullability
    # toggles. Per-task events still populate ``task_id`` and the
    # FK enforces the reference.
    op.alter_column(
        "events",
        "task_id",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    """Reverse the upgrade in inverse order.

    Strict reversibility (VAL-BUDGET-004 / VAL-BUDGET-070): after
    ``downgrade -1``, the schema is byte-equal to revision 004 —
    ``plans.budget_usd`` / ``plans.spent_usd`` / ``events.plan_id``
    are all gone and ``events.task_id`` is back to ``NOT NULL``.

    Sentinel-event handling
    -----------------------
    Any plan-level sentinel events created while at revision 005
    (``events.task_id IS NULL``) are *deleted* before re-imposing the
    constraint — they have no surviving wire-format under the pre-005
    schema (the ``plan_id`` column is gone, and a backfill of
    ``task_id`` to a sentinel would corrupt the audit log's
    reading-by-task semantics).
    """
    # Step 1 — wipe any sentinel events (``task_id IS NULL``) so the
    # subsequent ``SET NOT NULL`` doesn't fail. Also wipes any rows
    # that pre-existing tooling may have left with a ``plan_id`` set
    # (though the migration just added that column, so under normal
    # use this is no-op).
    op.execute("DELETE FROM events WHERE task_id IS NULL")

    # Step 2 — restore ``events.task_id NOT NULL``.
    op.alter_column(
        "events",
        "task_id",
        existing_type=sa.Text(),
        nullable=False,
    )

    # Step 3 — drop the FK then the ``events.plan_id`` column itself.
    # The FK must drop first because Postgres refuses ``DROP COLUMN``
    # while a constraint references it.
    op.drop_constraint("fk_events_plan_id_plans", "events", type_="foreignkey")
    op.drop_column("events", "plan_id")

    # Step 4 — drop the plan-budget columns (in inverse-add order;
    # alembic doesn't care, but a reader following the upgrade can
    # walk the diff in reverse).
    op.drop_column("plans", "spent_usd")
    op.drop_column("plans", "budget_usd")
