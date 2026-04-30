"""Add ``plans.github_issue_ref`` for Forge intake (TASK-108a).

This migration is the data-layer prerequisite for the *Forge intake*
stage introduced by TASK-108a. A single coupled change is made:

1. **Add ``plans.github_issue_ref text NULL``** — the canonical
   ``"owner/repo/<number>"`` triple captured by ``whilly forge
   intake`` so the plan row carries a verifiable back-reference to
   the GitHub Issue it was generated from. ``NULL`` is the documented
   "no GitHub origin" value (every plan created before this migration
   carries ``NULL`` and remains readable through the same code paths;
   plans created via ``whilly init`` keep ``NULL`` because they have
   no GitHub anchor).

   The column is **nullable** so the migration applies cleanly to
   pre-006 databases without backfilling stale plans (VAL-FORGE-001 /
   VAL-FORGE-002 — pre-existing rows show ``github_issue_ref IS
   NULL``). Forge intake's idempotency contract relies on the column
   *also* being unique on its non-NULL slice — we install a partial
   UNIQUE index with the same shape as
   ``ix_workers_token_hash_unique`` (migration 004): re-running
   ``whilly forge intake owner/repo/123`` MUST not create a second
   row for the same canonical triple (VAL-FORGE-007 / VAL-FORGE-019).
   The partial form (``WHERE github_issue_ref IS NOT NULL``) is
   essential — Postgres treats NULLs as distinct in a regular UNIQUE
   index, so a full UNIQUE on a nullable column would also satisfy
   "many rows with NULL", but the partial form documents the intent
   loudly and keeps the index footprint to plans that *have* an
   origin issue.

   ``text`` (over a fixed-width type) accepts arbitrary
   ``owner/repo/<number>`` strings — the longest realistic
   ``owner/repo`` is bounded by GitHub's own 39+100 char limits, but
   pinning that at the column level would create a footgun if we
   ever extend Forge to other forges (GitLab, Gitea) sharing the
   same column.

Migration numbering
-------------------
This migration is **006** because TASK-102 shipped
``005_plan_budget.py`` immediately before this one
(``down_revision = "005_plan_budget"``). The validation contract
text predates the rebase and refers to the migration as ``005`` — the
numbering shifted at mission-coordination time (see
``AGENTS.md → Migration Coordination``). The schema delta the contract
asserts is unchanged; only the file name moved.

Reversibility
-------------
``downgrade()`` reverses both changes in inverse order:

1. Drop the partial UNIQUE index ``ix_plans_github_issue_ref_unique``.
2. Drop the column ``plans.github_issue_ref``.

After ``downgrade -1`` the ``alembic_version`` row points at
``005_plan_budget`` and the schema is byte-equal to the pre-006
layout — pinned by VAL-FORGE-002 / VAL-FORGE-020.

Revision ID: 006_plan_github_ref
Revises: 005_plan_budget
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_plan_github_ref"
down_revision: str | None = "005_plan_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Index name — exported as a module-level constant so tests can assert on
# it via ``information_schema.indexes`` / ``pg_indexes`` introspection
# without duplicating the literal at the call site (a typo would make
# the test pass against the wrong index).
GITHUB_ISSUE_REF_UNIQUE_INDEX: str = "ix_plans_github_issue_ref_unique"


def upgrade() -> None:
    """Add ``plans.github_issue_ref text NULL`` and the partial UNIQUE index.

    Order matters: the column add happens first so the index creation
    observes the post-DDL column. Both DDL statements run in the same
    alembic-managed transaction (Postgres supports transactional DDL),
    so a mid-migration crash leaves the schema untouched at revision
    005.
    """
    # Step 1 — add the column. NULL is the documented "no GitHub
    # origin" value; ``server_default=None`` keeps the column free of
    # an INSERT-time default so callers must opt in by passing the
    # canonical ``owner/repo/<number>`` triple explicitly (VAL-FORGE-004).
    op.add_column(
        "plans",
        sa.Column(
            "github_issue_ref",
            sa.Text(),
            nullable=True,
            server_default=None,
        ),
    )

    # Step 2 — partial UNIQUE on the non-NULL slice. The index pins
    # the idempotency contract at the schema level (VAL-FORGE-007 /
    # VAL-FORGE-019): two concurrent ``whilly forge intake
    # owner/repo/123`` invocations either both insert the same row
    # (one wins, the loser hits ON CONFLICT DO NOTHING and reads back
    # the existing plan) or one wins and the other catches a
    # ``UniqueViolationError`` that the application code translates
    # into "return existing plan id". Either way: exactly one
    # ``plans`` row per canonical issue ref.
    op.create_index(
        GITHUB_ISSUE_REF_UNIQUE_INDEX,
        "plans",
        ["github_issue_ref"],
        unique=True,
        postgresql_where="github_issue_ref IS NOT NULL",
    )


def downgrade() -> None:
    """Reverse the upgrade: drop the index, then the column.

    Strict reversibility (VAL-FORGE-002 / VAL-FORGE-020): after
    ``downgrade -1``, the schema is byte-equal to revision 005 — the
    column and the partial UNIQUE index are both gone. Index drops
    *before* the column so Postgres doesn't refuse on a "constraint
    references this column" error (the partial UNIQUE is implemented
    as an index, not a TABLE constraint, but ordering it this way
    keeps the diff readable in either direction).
    """
    op.drop_index(GITHUB_ISSUE_REF_UNIQUE_INDEX, table_name="plans")
    op.drop_column("plans", "github_issue_ref")
