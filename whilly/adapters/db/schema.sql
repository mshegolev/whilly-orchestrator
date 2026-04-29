-- Whilly v4.0 Postgres schema — REFERENCE / DOCUMENTATION ONLY.
--
-- The actual schema is created by Alembic from
-- `whilly/adapters/db/migrations/versions/001_initial_schema.py`. This file
-- exists so reviewers can read the contract in plain SQL without parsing
-- migration scripts (PRD acceptance: "schema.sql — reference DDL dublicates
-- Alembic для удобства чтения"). Keep both in sync when changing the schema:
--
--   1. Author / edit the migration in `migrations/versions/`.
--   2. Run `alembic upgrade head --sql > /tmp/rendered.sql` and use it as a
--      reference; manually update this file to match.
--   3. CI step (TASK-029) parses both for divergence — until then, drift is
--      a maintainer responsibility.
--
-- DO NOT execute this file against a real database. Use `alembic upgrade
-- head` (or `make db-up` once added) instead — that path also tracks the
-- `alembic_version` row needed for future migrations.

-- ─── workers ─────────────────────────────────────────────────────────────
CREATE TABLE workers (
    worker_id      TEXT PRIMARY KEY,
    hostname       TEXT NOT NULL,
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- ``token_hash`` is nullable since migration 004 (TASK-101): operators
    -- revoke a worker by setting it to NULL, and the per-worker bearer
    -- dep treats NULL as "revoked → 401". A partial UNIQUE index
    -- (below) keeps issued hashes unambiguous on the lookup path.
    token_hash     TEXT,
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Offline-worker recovery (TASK-025b, PRD FR-1.4 / NFR-1 / SC-2). The
    -- offline-worker sweep flips this to 'offline' once last_heartbeat
    -- ages past its threshold (2 min default) and releases the worker's
    -- in-flight tasks back to PENDING.
    status         TEXT NOT NULL DEFAULT 'online',
    CONSTRAINT ck_workers_status_valid CHECK (status IN ('online', 'offline'))
);

CREATE INDEX ix_workers_last_heartbeat ON workers (last_heartbeat);
-- Partial index keeps the offline-worker sweep cheap: only online rows
-- are candidates, so the planner skips already-flipped workers without
-- scanning them.
CREATE INDEX ix_workers_status_online_heartbeat ON workers (last_heartbeat)
    WHERE status = 'online';
-- Partial UNIQUE on issued hashes (TASK-101, migration 004). Per-worker
-- bearer validation issues SELECT ... WHERE token_hash = $1 on every
-- RPC; uniqueness over non-NULL hashes makes the lookup deterministic
-- at the schema level. Revoked rows (token_hash = NULL) are excluded
-- from the index so revocation does not collide on a single sentinel.
CREATE UNIQUE INDEX ix_workers_token_hash_unique ON workers (token_hash)
    WHERE token_hash IS NOT NULL;

-- ─── plans ───────────────────────────────────────────────────────────────
CREATE TABLE plans (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    -- Per-plan budget guard (TASK-102, migration 005). ``budget_usd``
    -- is the operator-supplied spend cap (NULL = unlimited);
    -- ``spent_usd`` is the running total of completed-task
    -- ``cost_usd`` updated atomically by ``complete_task``.
    -- numeric(10,4) gives sub-cent precision without float drift
    -- (VAL-BUDGET-033). Strict monotonic non-decrease of
    -- ``spent_usd`` is enforced at the repository layer, not via
    -- a CHECK constraint (see migration 005's docstring).
    budget_usd NUMERIC(10, 4),
    spent_usd  NUMERIC(10, 4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── tasks ───────────────────────────────────────────────────────────────
CREATE TABLE tasks (
    id                  TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    dependencies        JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_files           JSONB NOT NULL DEFAULT '[]'::jsonb,
    priority            TEXT NOT NULL DEFAULT 'medium',
    description         TEXT NOT NULL DEFAULT '',
    acceptance_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    test_steps          JSONB NOT NULL DEFAULT '[]'::jsonb,
    prd_requirement     TEXT NOT NULL DEFAULT '',
    -- Optimistic-locking counter (PRD FR-2.4).
    version             INTEGER NOT NULL DEFAULT 0,
    -- Claim ownership / visibility-timeout (PRD FR-1.3, FR-1.4).
    claimed_by          TEXT REFERENCES workers (worker_id) ON DELETE SET NULL,
    claimed_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_tasks_status_valid CHECK (
        status IN ('PENDING', 'CLAIMED', 'IN_PROGRESS', 'DONE', 'FAILED', 'SKIPPED')
    ),
    CONSTRAINT ck_tasks_priority_valid CHECK (
        priority IN ('critical', 'high', 'medium', 'low')
    ),
    -- Either both claim fields are NULL (unclaimed) or both set (owned).
    CONSTRAINT ck_tasks_claim_pair_consistent CHECK (
        (claimed_by IS NULL) = (claimed_at IS NULL)
    )
);

CREATE INDEX ix_tasks_plan_id_status ON tasks (plan_id, status);
CREATE INDEX ix_tasks_claimed_at_active ON tasks (claimed_at)
    WHERE status IN ('CLAIMED', 'IN_PROGRESS');

-- ─── events ──────────────────────────────────────────────────────────────
CREATE TABLE events (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    -- ``task_id`` was relaxed from NOT NULL by migration 005
    -- (TASK-102): the plan-level sentinel ``plan.budget_exceeded``
    -- writes ``task_id IS NULL`` with ``plan_id`` populated. Per-task
    -- events still populate ``task_id`` and the FK enforces the
    -- reference; ON DELETE CASCADE wipes per-task events when the
    -- parent task is deleted.
    task_id    TEXT REFERENCES tasks (id) ON DELETE CASCADE,
    -- Plan-level reference (TASK-102, migration 005). Populated only
    -- for plan-scoped sentinel events (``plan.budget_exceeded``);
    -- per-task events leave this column NULL. ON DELETE CASCADE on
    -- the FK wipes sentinel rows alongside the parent plan when
    -- ``plan reset --hard`` is invoked.
    plan_id    TEXT REFERENCES plans (id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Per-event caller-supplied diagnostics (TASK-104b, migration 003).
    -- Distinct from ``payload`` (which carries state-machine
    -- bookkeeping like ``version`` / ``reason``); ``detail`` is
    -- nullable, free-form, and never written as the JSON literal
    -- ``null`` or as ``{}`` — the repo passes Python ``None`` straight
    -- through asyncpg so SQL ``IS NULL`` predicates round-trip cleanly.
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_events_task_id_created_at ON events (task_id, created_at);
