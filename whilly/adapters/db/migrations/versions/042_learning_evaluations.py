"""Durable research reports, experiment evidence and owner decisions."""

from alembic import op

revision = "042_learning_evaluations"
down_revision = "041_product_change_sets"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """CREATE TABLE swarm_learning_stops (
        product_id TEXT NOT NULL REFERENCES swarm_products(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (product_id, run_id)
    )"""
    )
    op.execute(
        """CREATE TABLE swarm_learning_reports (
        product_id TEXT NOT NULL REFERENCES swarm_products(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL,
        report JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (product_id, run_id),
        CONSTRAINT ck_learning_report_object CHECK (jsonb_typeof(report) = 'object')
    )"""
    )
    op.execute(
        """CREATE TABLE swarm_learning_experiments (
        product_id TEXT NOT NULL REFERENCES swarm_products(id) ON DELETE CASCADE,
        id TEXT NOT NULL,
        proposer_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        report JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (product_id, id),
        CONSTRAINT ck_learning_experiment_report_object CHECK (jsonb_typeof(report) = 'object')
    )"""
    )
    op.execute(
        """CREATE TABLE swarm_learning_experiment_decisions (
        event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        product_id TEXT NOT NULL,
        experiment_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('accept','reject','rollback')),
        reason TEXT NOT NULL,
        rollback_reference TEXT,
        execution_authorized BOOLEAN NOT NULL DEFAULT FALSE CHECK (execution_authorized = FALSE),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        FOREIGN KEY (product_id, experiment_id)
          REFERENCES swarm_learning_experiments(product_id, id) ON DELETE CASCADE
    )"""
    )
    op.execute(
        """CREATE FUNCTION swarm_learning_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'learning evidence is immutable'; END $$"""
    )
    op.execute(
        """CREATE TRIGGER swarm_learning_reports_immutable BEFORE UPDATE OR DELETE ON swarm_learning_reports
        FOR EACH ROW EXECUTE FUNCTION swarm_learning_evidence_immutable()"""
    )
    op.execute(
        """CREATE TRIGGER swarm_learning_experiments_immutable BEFORE UPDATE OR DELETE ON swarm_learning_experiments
        FOR EACH ROW EXECUTE FUNCTION swarm_learning_evidence_immutable()"""
    )
    op.execute(
        """CREATE TRIGGER swarm_learning_decisions_append_only BEFORE UPDATE OR DELETE
        ON swarm_learning_experiment_decisions FOR EACH ROW EXECUTE FUNCTION swarm_learning_evidence_immutable()"""
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS swarm_learning_decisions_append_only ON swarm_learning_experiment_decisions")
    op.execute("DROP TRIGGER IF EXISTS swarm_learning_experiments_immutable ON swarm_learning_experiments")
    op.execute("DROP TRIGGER IF EXISTS swarm_learning_reports_immutable ON swarm_learning_reports")
    op.execute("DROP FUNCTION IF EXISTS swarm_learning_evidence_immutable()")
    op.execute("DROP TABLE IF EXISTS swarm_learning_experiment_decisions")
    op.execute("DROP TABLE IF EXISTS swarm_learning_experiments")
    op.execute("DROP TABLE IF EXISTS swarm_learning_reports")
    op.execute("DROP TABLE IF EXISTS swarm_learning_stops")
