"""Preserve complete execution bindings alongside immutable spec revisions."""

from alembic import op

revision = "035_spec_bindings"
down_revision = "034_publication_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE swarm_product_specs ADD COLUMN binding JSONB")


def downgrade():
    op.execute("ALTER TABLE swarm_product_specs DROP COLUMN binding")
