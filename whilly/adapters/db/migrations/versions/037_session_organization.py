"""Reversible presentation metadata for swarm sessions."""

from alembic import op

revision = "037_session_organization"
down_revision = "036_learning_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE swarm_sessions ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE swarm_sessions ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT FALSE")


def downgrade():
    op.execute("ALTER TABLE swarm_sessions DROP COLUMN is_test, DROP COLUMN archived")
