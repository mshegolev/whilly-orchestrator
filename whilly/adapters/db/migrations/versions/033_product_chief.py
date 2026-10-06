"""Durable product chief conversation identity."""

from alembic import op

revision = "033_product_chief"
down_revision = "032_model_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE swarm_products ADD COLUMN chief_session_id TEXT REFERENCES swarm_sessions(id) ON DELETE SET NULL"
    )
    op.execute("CREATE UNIQUE INDEX swarm_feature_session_unique ON swarm_product_features(session_id)")


def downgrade():
    op.execute("DROP INDEX swarm_feature_session_unique")
    op.execute("ALTER TABLE swarm_products DROP COLUMN chief_session_id")
