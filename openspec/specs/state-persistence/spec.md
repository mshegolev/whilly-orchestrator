## Purpose

The state-persistence capability defines how Whilly v4 durably stores and mutates
orchestration state. In v4 the authoritative state lives in **Postgres**, not in any
local JSON file: the `whilly/adapters/db` layer owns an asyncpg connection pool
(`pool.py`), an optimistic-locked `TaskRepository` (`repository.py`) that performs
all task lifecycle transitions, an `events` audit log written in the same transaction
as every state change, and an Alembic migration chain (`migrations/versions/001`
through `028`) that defines and evolves the schema. This capability governs the pool
lifecycle, the concurrency-safe claim/start/complete/fail/release flow, the audit-log
atomicity guarantee, worker registration and liveness, and the schema source of truth.
It also records truthfully that the v3 `StateStore` / `.whilly_state.json` JSON-resume
path is legacy and unwired in v4 — it MUST NOT be treated as the live persistence
contract.

## Requirements

### Requirement: Connection pool lifecycle and DSN coercion
The system SHALL provide all Postgres connectivity through `whilly.adapters.db.pool.create_pool` and `close_pool`, which MUST accept both `postgresql://` and `postgresql+asyncpg://` DSNs (stripping the SQLAlchemy `+asyncpg` driver suffix), size the pool from `WHILLY_DB_POOL_MIN` / `WHILLY_DB_POOL_MAX` (defaults 2 / 10), run a `SELECT 1` fail-fast health check on startup, and close the pool gracefully on shutdown.

#### Scenario: SQLAlchemy-style DSN is coerced for asyncpg
- **WHEN** `create_pool` is called with a DSN beginning `postgresql+asyncpg://`
- **THEN** the system SHALL strip the `+asyncpg` driver suffix and open the pool against the plain `postgresql://` DSN

#### Scenario: Fail-fast health check on a bad database
- **WHEN** `create_pool` cannot reach the database or the `SELECT 1` health check fails
- **THEN** the system SHALL close the pool before propagating the exception so a misconfigured boot crashes immediately instead of leaking sockets

#### Scenario: Pool sizing from environment with safe fallback
- **WHEN** `WHILLY_DB_POOL_MIN` or `WHILLY_DB_POOL_MAX` is unset, empty, non-integer, or non-positive
- **THEN** the system SHALL fall back to the module defaults (`min_size=2`, `max_size=10`)
- **AND** a configured `min_size` greater than `max_size` SHALL raise a `ValueError`

#### Scenario: Graceful drain on shutdown
- **WHEN** `close_pool` is called at process shutdown
- **THEN** the system SHALL wait for in-flight queries to finish before tearing connections down

### Requirement: Atomic task claim with skip-locked concurrency
The system SHALL claim a task via `TaskRepository.claim_task` using a single `SELECT ... FOR UPDATE SKIP LOCKED` CTE that flips exactly one `PENDING` row to `CLAIMED` (setting `claimed_by`, `claimed_at`, incrementing `version`) so that concurrent workers never claim the same row.

#### Scenario: Concurrent workers claim distinct rows
- **WHEN** multiple workers call `claim_task` for the same plan at the same time
- **THEN** each invocation SHALL lock and return a different `PENDING` row, and no row SHALL be claimed by more than one worker
- **AND** the claimed row SHALL transition `PENDING` → `CLAIMED` with `version` incremented

#### Scenario: No claimable work returns nothing
- **WHEN** `claim_task` runs and no `PENDING` row is eligible for the worker
- **THEN** the system SHALL return no task rather than blocking on a locked row

### Requirement: Optimistic-locked completion and failure
The system SHALL complete or fail a task via `TaskRepository.complete_task` / `fail_task` using an UPDATE filtered by `WHERE id = $1 AND version = $2 AND status IN (...)` that increments `version`, so a stale-version writer's UPDATE affects zero rows and a lost update is detected and surfaced as a `VersionConflictError`.

#### Scenario: Stale version is rejected
- **WHEN** a caller invokes `complete_task` (or `fail_task`) with a `version` that no longer matches the row's current `version`
- **THEN** the UPDATE SHALL affect zero rows and the system SHALL raise a `VersionConflictError` after probing the row

#### Scenario: Matching version completes the task
- **WHEN** a caller invokes `complete_task` with the current `version` and the task status is `CLAIMED` or `IN_PROGRESS`
- **THEN** the system SHALL transition the row to `DONE` and increment `version`

#### Scenario: Terminal task cannot be dragged back
- **WHEN** a caller attempts to complete or fail a task already in a terminal status (`DONE`, `FAILED`, `SKIPPED`)
- **THEN** the status filter SHALL exclude the row and the transition SHALL NOT occur

### Requirement: Events audit log written atomically with state changes
The system SHALL insert one row into the `events` table for every task state transition inside the same database transaction as the corresponding `tasks` UPDATE, so the audit log can never disagree with the task state — including the visibility-timeout `RELEASE` path which writes a `RELEASE` event per released row in a single CTE round-trip.

#### Scenario: Transition and audit row commit together
- **WHEN** any task transition (claim, start, complete, fail, release, skip) is executed
- **THEN** the matching `events` row SHALL be inserted in the same transaction as the `tasks` UPDATE
- **AND** if the transaction rolls back, neither the state change nor the audit row SHALL persist

#### Scenario: Stale-claim sweep audits every released row
- **WHEN** `TaskRepository.release_stale_tasks` flips aged-out `CLAIMED`/`IN_PROGRESS` rows back to `PENDING`
- **THEN** the system SHALL insert one `RELEASE` event per released row carrying `reason = "visibility_timeout"` in the same statement as the release UPDATE

### Requirement: Worker registration and liveness persistence
The system SHALL persist cluster membership in the `workers` table via `TaskRepository.register_worker` (storing only the SHA-256 hash of the bearer token, never the plaintext) and SHALL record worker liveness via `TaskRepository.update_heartbeat`, which stamps `last_heartbeat` and returns the worker to `online`.

#### Scenario: Registration stores only the token hash
- **WHEN** `register_worker` inserts a new `workers` row
- **THEN** the system SHALL store the token hash and SHALL NOT store the plaintext bearer token

#### Scenario: Heartbeat advances liveness
- **WHEN** `update_heartbeat` is called for a known `worker_id`
- **THEN** the system SHALL set `last_heartbeat = NOW()` and flip the worker `status` to `online`
- **AND** an unknown `worker_id` SHALL surface as zero rows updated rather than raising

### Requirement: Alembic migration chain is the schema source of truth
The system SHALL define and evolve the Postgres schema exclusively through the Alembic migration chain under `whilly/adapters/db/migrations/versions` (revisions `001` through `028`), which establishes the `tasks`, `workers`, `events`, plan, scheduler, session, user, TOTP, and WebAuthn tables and their constraints.

#### Scenario: Schema originates from migrations
- **WHEN** a Whilly database is provisioned or upgraded
- **THEN** the schema SHALL be produced by applying the Alembic migration chain in order, not by ad-hoc DDL in application code

#### Scenario: Schema changes ship as new revisions
- **WHEN** a new column, table, or constraint is required
- **THEN** the change SHALL be added as a new Alembic revision appended to the chain rather than mutating an existing applied revision

### Requirement: Legacy JSON StateStore is not the v4 persistence contract
The system SHALL NOT rely on the v3 `whilly/state_store.py` `StateStore`, `.whilly_state.json`, or `WHILLY_STATE_FILE` as the live state-persistence mechanism; `StateStore` has zero instantiations in the v4 runtime and the JSON-resume path is a no-op superseded by the Postgres layer.

#### Scenario: StateStore is unwired in v4
- **WHEN** the v4 worker-claim flow persists or restores orchestration state
- **THEN** it SHALL use the Postgres `TaskRepository` / `events` layer and SHALL NOT read or write `.whilly_state.json`

#### Scenario: WHILLY_STATE_FILE has no effect on persistence
- **WHEN** `WHILLY_STATE_FILE` is set in the environment
- **THEN** the v4 persistence contract SHALL be unaffected, because the JSON-resume path is legacy and not wired into the worker-claim flow

### Requirement: File-based pause gate remains a local control signal
The system SHALL keep the pause/resume gate (`whilly/pause_control.py` `PauseControl`, backed by `.whilly_pause`) as a local file-based control signal consumed by the jira-watch dispatch loop, distinct from and not part of the Postgres state-persistence layer.

#### Scenario: Pause gate read by the jira-watch loop
- **WHEN** the `cli/jira_watch_loop` dispatch loop evaluates whether to dispatch work
- **THEN** it SHALL consult `PauseControl` reading `.whilly_pause` and suppress dispatch while paused
- **AND** the pause state SHALL NOT be stored in Postgres as part of the task-persistence contract
