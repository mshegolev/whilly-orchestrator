"""Durable learning message envelopes with transactional outbox and inbox."""

from alembic import op

revision = "038_learning_messages"
down_revision = "037_session_organization"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE swarm_learning_messages (
        id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL REFERENCES swarm_products(id) ON DELETE CASCADE,
        feature_id TEXT,
        task_id TEXT,
        sender_id TEXT NOT NULL,
        recipient_project TEXT NOT NULL,
        recipient_role TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('question','answer','finding','contract_change','task_proposal','receipt')),
        correlation_id TEXT,
        causation_id TEXT,
        idempotency_key TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        hop_count INTEGER NOT NULL CHECK (hop_count >= 0),
        payload JSONB NOT NULL,
        evidence_refs JSONB NOT NULL,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('persisted','delivered','acknowledged','rejected','expired')),
        rejected_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        delivered_at TIMESTAMPTZ,
        acknowledged_at TIMESTAMPTZ,
        UNIQUE (product_id, sender_id, idempotency_key)
    )""")
    op.execute("""CREATE TABLE swarm_learning_message_outbox (
        message_id TEXT PRIMARY KEY REFERENCES swarm_learning_messages(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        delivered_at TIMESTAMPTZ
    )""")
    op.execute("""CREATE TABLE swarm_learning_message_inbox (
        message_id TEXT PRIMARY KEY REFERENCES swarm_learning_messages(id) ON DELETE CASCADE,
        recipient_project TEXT NOT NULL,
        recipient_role TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('persisted','delivered','acknowledged','rejected','expired')),
        delivered_at TIMESTAMPTZ,
        acknowledged_at TIMESTAMPTZ,
        UNIQUE (message_id, recipient_project, recipient_role)
    )""")
    op.execute(
        "CREATE INDEX swarm_learning_message_inbox_ready_idx ON swarm_learning_message_inbox (recipient_project, recipient_role, state)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS swarm_learning_message_inbox")
    op.execute("DROP TABLE IF EXISTS swarm_learning_message_outbox")
    op.execute("DROP TABLE IF EXISTS swarm_learning_messages")
