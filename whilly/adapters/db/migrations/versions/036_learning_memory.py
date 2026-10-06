"""Immutable metadata and redactable payload tables for shared learning memory."""

from alembic import op

revision = "036_learning_memory"
down_revision = "035_spec_bindings"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE swarm_learning_revisions (
        id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES swarm_products(id), project_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('fact','hypothesis','decision','observation')),
        evidence_hash TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL, verified_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ, classification TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('candidate','verified','stale','superseded','retracted')),
        author_id TEXT NOT NULL, verifier_id TEXT, policy_version TEXT NOT NULL, supersedes TEXT,
        conflicts TEXT[] NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        FOREIGN KEY (supersedes) REFERENCES swarm_learning_revisions(id),
        CONSTRAINT learning_scope_check CHECK (project_id IS NOT NULL OR product_id IS NOT NULL)
    )""")
    op.execute("""CREATE TABLE swarm_learning_payloads (
        revision_id TEXT PRIMARY KEY REFERENCES swarm_learning_revisions(id) ON DELETE CASCADE,
        body TEXT NOT NULL, source_uri TEXT NOT NULL, source_sha TEXT, redacted_at TIMESTAMPTZ
    )""")
    op.execute("CREATE INDEX swarm_learning_visible_idx ON swarm_learning_revisions(product_id, project_id, classification, status)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS swarm_learning_payloads")
    op.execute("DROP TABLE IF EXISTS swarm_learning_revisions")
