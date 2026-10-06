"""Durable publication evidence and compatibility with early admission installs."""

from alembic import op

revision = "034_publication_receipts"
down_revision = "033_product_chief"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE swarm_model_admissions ADD COLUMN IF NOT EXISTS owner_host TEXT NOT NULL DEFAULT 'unknown'")
    op.execute("ALTER TABLE swarm_model_admissions ADD COLUMN IF NOT EXISTS owner_pid INTEGER NOT NULL DEFAULT 0")
    op.execute(
        "ALTER TABLE swarm_model_admissions ADD COLUMN IF NOT EXISTS owner_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    )
    op.execute(
        "CREATE TABLE swarm_publications(feature_id TEXT NOT NULL REFERENCES swarm_product_features(id) ON DELETE CASCADE, project_id TEXT NOT NULL, receipt JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(feature_id,project_id))"
    )


def downgrade():
    op.execute("DROP TABLE swarm_publications")
