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
    -- Per-user worker attribution (M2 mission, migration 008). NULL for
    -- workers registered without an owner-email (legacy bootstrap path,
    -- back-compat); set to the operator's email when registration
    -- carries it. The partial index ix_workers_owner_email below pins
    -- the per-owner lookup path in the M2 admin dashboard.
    owner_email    TEXT,
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
-- Partial index on owner_email (M2, migration 008). Speeds up the
-- per-owner worker listing query in the M2 admin dashboard
-- (``SELECT ... FROM workers WHERE owner_email = $1``) without
-- bloating the index footprint with one entry per anonymous /
-- legacy-bootstrap row.
CREATE INDEX ix_workers_owner_email ON workers (owner_email)
    WHERE owner_email IS NOT NULL;

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
    -- Forge intake back-reference (TASK-108a, migration 006). NULL
    -- for plans not generated from a GitHub issue (e.g. plans created
    -- via ``whilly init``); for Forge-originated plans, the canonical
    -- ``owner/repo/<number>`` triple captured by ``whilly forge
    -- intake``. The partial UNIQUE index below pins the idempotency
    -- contract — exactly one plan row per canonical issue ref
    -- (VAL-FORGE-007 / VAL-FORGE-019).
    github_issue_ref TEXT,
    -- Absolute filesystem path of the PRD markdown file that
    -- generated this plan (M3 fix-feature, migration 007). NULL for
    -- plans without a generated PRD (created via ``whilly init`` /
    -- ``whilly plan create``); set to the absolute path of
    -- ``docs/PRD-<slug>.md`` for plans created via ``whilly forge
    -- intake``. Pins VAL-FORGE-005 ("Path(plan.prd_file).is_file()
    -- is True") at the plan-row level — a stable point-in-time
    -- fact that does not depend on the events log retention policy.
    prd_file TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial UNIQUE on Forge-originated plans (TASK-108a, migration
-- 006). Re-running ``whilly forge intake owner/repo/<N>`` MUST NOT
-- create a duplicate plan row for the same issue. The partial form
-- (``WHERE github_issue_ref IS NOT NULL``) excludes plans without a
-- GitHub origin so they don't all collide on a single NULL pseudo-
-- value (Postgres treats NULLs as distinct in a regular UNIQUE
-- index, but the partial predicate makes the intent obvious).
CREATE UNIQUE INDEX ix_plans_github_issue_ref_unique ON plans (github_issue_ref)
    WHERE github_issue_ref IS NOT NULL;

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

-- ─── bootstrap_tokens ────────────────────────────────────────────────────
-- Per-user worker-bootstrap auth (M2 mission, migration 009). Replaces
-- the single shared ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` env-var sentinel
-- with a per-operator table: ``POST /workers/register`` carrying
-- ``Bearer <plaintext>`` looks up the SHA-256 hex digest of the
-- plaintext in this table; an active row (``revoked_at IS NULL`` AND
-- (``expires_at IS NULL OR expires_at > NOW()``)) authorises the
-- registration and pins the new ``workers.owner_email`` to the row's
-- ``owner_email``. The ``is_admin`` bit (false by default) is the M2
-- admin-CLI gate (``make_admin_auth`` consults this column to authorise
-- ``whilly admin …`` calls). Plaintext NEVER reaches Postgres
-- (PRD NFR-3 — mirrors the ``workers.token_hash`` discipline).
CREATE TABLE bootstrap_tokens (
    token_hash  TEXT PRIMARY KEY,
    owner_email TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    is_admin    BOOLEAN NOT NULL DEFAULT false
);

-- Partial index over active rows only. The per-owner listing
-- (``list_bootstrap_tokens``) and the per-owner active-token lookup
-- paths only ever touch rows with ``revoked_at IS NULL``; revoked
-- rows are forensic-only. Keeping the index footprint bounded to
-- currently-issued tokens mirrors the pattern of
-- ``ix_workers_owner_email`` (migration 008).
CREATE INDEX ix_bootstrap_tokens_owner_email_active ON bootstrap_tokens (owner_email)
    WHERE revoked_at IS NULL;

-- ─── funnel_url ──────────────────────────────────────────────────────────
-- Singleton table the M2 localhost.run sidecar (m2-localhostrun-funnel-
-- sidecar, migration 010) upserts on every reconnect with the latest
-- assigned ``https://<random>.lhr.life`` URL. Workers re-discover the URL
-- via ``SELECT url FROM funnel_url ORDER BY updated_at DESC LIMIT 1`` (or
-- the equivalent ``WHERE id = 1`` lookup since the singleton invariant
-- guarantees a single row at most). The shared-volume file
-- ``/funnel/url.txt`` is the fallback for environments without postgres
-- reachability.
--
-- The ``CHECK (id = 1)`` constraint pins the singleton invariant at the
-- schema level — no second row can ever land. The sidecar upsert uses
-- ``INSERT INTO funnel_url (id, url) VALUES (1, $URL)
-- ON CONFLICT (id) DO UPDATE SET url=EXCLUDED.url, updated_at=NOW();``
-- to overwrite the row in place on every URL rotation.
CREATE TABLE funnel_url (
    id         INTEGER PRIMARY KEY DEFAULT 1,
    url        TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT funnel_url_singleton CHECK (id = 1)
);
