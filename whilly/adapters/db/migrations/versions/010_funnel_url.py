"""Add ``funnel_url`` singleton table for the M2 localhost.run sidecar.

Adds a one-row table the funnel sidecar (m2-localhostrun-funnel-
sidecar) upserts on every reconnect with the latest assigned
``https://<random>.lhr.life`` URL. Workers re-discover the URL via
``SELECT url FROM funnel_url ORDER BY updated_at DESC LIMIT 1`` (or
``WHERE id = 1`` since the singleton invariant guarantees a single
row at most). The shared-volume file ``/funnel/url.txt`` is the
fallback for environments without postgres reachability.

Schema shape
------------
* ``id integer PRIMARY KEY DEFAULT 1`` — singleton key. The
  ``funnel_url_singleton`` CHECK pins ``id = 1`` so no second row
  can ever land. Sidecar upsert uses ``INSERT ... ON CONFLICT (id)
  DO UPDATE`` to overwrite the row in place on every URL rotation.
* ``url text NOT NULL`` — the latest published
  ``https://<random>.lhr.life`` URL.
* ``updated_at timestamptz NOT NULL DEFAULT NOW()`` — sidecar
  bumps this on every upsert; consumers (worker URL re-discovery,
  validator probes) order by it.

Why a singleton (vs. an append-only history)?
---------------------------------------------
The contract is "what is the live URL right now?" — historical URLs
are useful for forensics but not for routing. Keeping a single row
keeps the lookup query trivial (one PK lookup) and makes the
``ON CONFLICT (id) DO UPDATE`` upsert race-free across multiple
sidecar restarts. The ``CHECK (id = 1)`` constraint at the schema
level is a belt-and-braces guarantee against future code that
forgets to pass ``id=1`` explicitly.

Why no FK / no per-tenant scoping?
----------------------------------
The funnel URL is a deployment-wide singleton (one control-plane
per deployment, one sidecar per control-plane). M2 does not
support multi-tenant funnel URLs; a future M5+ feature could
generalize this into a ``funnel_url(deployment_id, url, ...)``
table.

Reversibility
-------------
``downgrade()`` drops the table. After ``downgrade -1`` the schema
is byte-equal to revision 009 (table is gone, ``alembic_version``
rolls back to ``009_bootstrap_tokens``). Pinned by the alembic
full-chain test and by
``tests/integration/test_migration_010_funnel_url.py``.

Revision ID: 010_funnel_url
Revises: 009_bootstrap_tokens
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_funnel_url"
down_revision: str | None = "009_bootstrap_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FUNNEL_URL_TABLE: str = "funnel_url"
FUNNEL_URL_SINGLETON_CONSTRAINT: str = "funnel_url_singleton"


def upgrade() -> None:
    """Create the ``funnel_url`` singleton table with the ``id = 1`` check."""
    op.create_table(
        FUNNEL_URL_TABLE,
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=FUNNEL_URL_SINGLETON_CONSTRAINT),
    )


def downgrade() -> None:
    """Reverse the upgrade: drop the table.

    Strict reversibility: after ``downgrade -1`` the schema is
    byte-equal to revision 009 — the table is gone.
    """
    op.drop_table(FUNNEL_URL_TABLE)
