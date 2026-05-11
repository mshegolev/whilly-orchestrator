"""Add ``pull_requests`` table for the M2 PR-review feedback loop.

Adds the data-layer prerequisite for the M2 ``pr-review-feedback``
milestone: a new ``pull_requests`` table that tracks the GitHub PRs
opened by the orchestrator on behalf of completed tasks, plus the
poller cursor (``last_seen_review_id`` / ``last_seen_check_run_id``)
and the latest observed review decision so the re-iterate path can
spawn follow-up tasks deterministically.

Schema shape
------------
* ``id BIGSERIAL PRIMARY KEY`` — surrogate id; matches the existing
  ``events.id`` convention. BIGINT because PRs accumulate across
  long-lived deployments and INTEGER would wrap.
* ``plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE`` —
  parent plan. The column type matches ``plans.id`` (TEXT, set in
  migration 001) — the feature spec's "UUID" reference is a
  documentation-side typo: every existing FK to ``plans.id``
  (``tasks.plan_id``, ``events.plan_id``) is also TEXT, and a UUID
  declaration would refuse the FK at constraint-creation time.
  ``ON DELETE CASCADE`` means a hard ``DELETE FROM plans`` (e.g.
  ``whilly plan reset --hard``) wipes the PR rows alongside the
  parent plan.
* ``task_id TEXT NOT NULL`` — originating task id. Intentionally
  *not* a foreign key to ``tasks.id`` (yet) — task rows can be
  deleted via plan reset while their PR row should still survive
  for forensic queries, and the M2 mission scope does not include
  the PR-side cascade story. The string is validated at the
  application layer via the same ``^[A-Za-z0-9._:/-]+$`` regex M1
  installed on ``Task.from_dict``.
* ``pr_number INT NOT NULL`` — the GitHub PR number (from
  ``gh pr create --json number``).
* ``pr_url TEXT NOT NULL`` — the canonical
  ``https://github.com/<owner>/<repo>/pull/<n>`` URL. Stored
  verbatim so audit queries don't need to reconstruct it from the
  ``github_issue_ref`` triple plus ``pr_number``.
* ``branch TEXT NOT NULL`` — the head branch the agent pushed.
* ``head_sha TEXT`` — the head commit SHA at PR-open time. Nullable
  because the GitHub API may return a transiently-empty value while
  the PR is still being processed; the poller refreshes it on every
  cycle (VAL-PR-005's ``head_sha`` payload-key contract).
* ``state TEXT NOT NULL DEFAULT 'open'`` with a CHECK over the
  closed set ``('open','merged','closed','failed')``. ``'failed'``
  is the new state-class introduced by M2's PR opener for
  ``gh pr create`` failures (VAL-PR-022) — the row is still
  inserted (so the audit trail is preserved) but flagged so the
  poller skips it.
* ``review_decision TEXT`` (nullable) with a CHECK over
  ``('APPROVED','CHANGES_REQUESTED','REVIEW_REQUIRED', NULL)``.
  ``NULL`` is the documented "not yet reviewed" value; the GitHub
  API uses these three literals verbatim (no rewrite).
* ``last_synced_at TIMESTAMPTZ`` (nullable) — set by every
  successful poll cycle. ``NULL`` until the first sync.
* ``last_seen_review_id BIGINT`` (nullable) — cursor for the
  per-PR review feed (``gh api .../reviews``). The poller fetches
  reviews with ``id > last_seen_review_id`` so an unchanged feed
  emits zero events on re-poll (VAL-PR-012).
* ``last_seen_check_run_id BIGINT`` (nullable) — cursor for the
  status-check rollup. Same role as ``last_seen_review_id`` for
  CI signals.
* ``created_at`` / ``updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()``
  — standard audit timestamps.

Unique invariants
-----------------
A composite UNIQUE on ``(plan_id, pr_number)`` (VAL-PR-003): two
rows with the same PR number against different plans are allowed
(different repos / different orchestrator deployments may collide
on PR numbers), but two rows with the same PR number against the
same plan are rejected at the schema level so the post-COMPLETE
hook cannot insert a duplicate row on retry.

Reversibility
-------------
``downgrade()`` drops the table (the unique index drops with it
under Postgres semantics; no separate ``op.drop_index`` needed).
After ``downgrade -1`` the ``alembic_version`` row points at
``011_events_notify_trigger`` and the schema is byte-equal to the
pre-012 layout — pinned by the round-trip test.

Revision ID: 012_pull_requests_and_pr_events
Revises: 011_events_notify_trigger
Create Date: 2026-05-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_pull_requests_and_pr_events"
down_revision: str | None = "011_events_notify_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PULL_REQUESTS_TABLE: str = "pull_requests"
PULL_REQUESTS_PLAN_PR_UNIQUE_INDEX: str = "ix_pull_requests_plan_id_pr_number_unique"
PULL_REQUESTS_STATE_CHECK: str = "ck_pull_requests_state_valid"
PULL_REQUESTS_REVIEW_DECISION_CHECK: str = "ck_pull_requests_review_decision_valid"

_PR_STATES: tuple[str, ...] = ("open", "merged", "closed", "failed")
_PR_REVIEW_DECISIONS: tuple[str, ...] = ("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED")


def upgrade() -> None:
    """Create ``pull_requests`` plus the composite UNIQUE index."""
    op.create_table(
        PULL_REQUESTS_TABLE,
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False, start=1),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.Text(),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("review_decision", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_seen_review_id", sa.BigInteger(), nullable=True),
        sa.Column("last_seen_check_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            f"state IN {_PR_STATES}",
            name=PULL_REQUESTS_STATE_CHECK,
        ),
        sa.CheckConstraint(
            f"review_decision IS NULL OR review_decision IN {_PR_REVIEW_DECISIONS}",
            name=PULL_REQUESTS_REVIEW_DECISION_CHECK,
        ),
    )
    op.create_index(
        PULL_REQUESTS_PLAN_PR_UNIQUE_INDEX,
        PULL_REQUESTS_TABLE,
        ["plan_id", "pr_number"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the table (and its dependent unique index).

    Strict reversibility: after ``downgrade -1`` the schema is
    byte-equal to revision 011 — the table and its unique index
    are gone. ``op.drop_table`` cascades the index drop in
    Postgres; we name the index here only for documentation.
    """
    op.drop_index(PULL_REQUESTS_PLAN_PR_UNIQUE_INDEX, table_name=PULL_REQUESTS_TABLE)
    op.drop_table(PULL_REQUESTS_TABLE)
