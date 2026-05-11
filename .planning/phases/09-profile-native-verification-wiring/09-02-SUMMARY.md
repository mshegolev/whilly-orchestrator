---
phase: 09-profile-native-verification-wiring
plan: 02
subsystem: verification
tags: [postgres, alembic, transport, verification-metadata, remote-worker]

# Dependency graph
requires:
  - phase: 09-profile-native-verification-wiring
    provides: Plan 09-01 added typed `VerificationCommand` metadata and filesystem plan_io round-trip support.
provides:
  - `plans.verification_commands` JSONB persistence for ordered profile verification metadata.
  - `whilly plan import/export` support for durable typed verification commands.
  - Task-free transport plan metadata carrying verification commands to remote clients.
affects: [09-profile-native-verification-wiring, plan-persistence, remote-worker-verification]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Ordered plan-level verification metadata is stored as JSONB with default `[]`.
    - Transport clients fetch task-free plan metadata and reconstruct pure core `Plan` values.

key-files:
  created:
    - whilly/adapters/db/migrations/versions/015_plan_verification_commands.py
    - tests/integration/test_alembic_015_plan_verification_commands.py
    - .planning/phases/09-profile-native-verification-wiring/09-02-SUMMARY.md
  modified:
    - whilly/adapters/db/schema.sql
    - whilly/cli/plan.py
    - whilly/adapters/transport/schemas.py
    - whilly/adapters/transport/server.py
    - whilly/adapters/transport/client.py
    - tests/integration/test_plan_io.py
    - tests/integration/test_alembic_full_chain.py
    - tests/integration/test_alembic_013_work_intents.py
    - tests/unit/test_transport_schemas.py
    - tests/unit/test_remote_client.py

key-decisions:
  - "Plan import preserves existing rows with `ON CONFLICT DO NOTHING`; verification metadata is only written on first import."
  - "Remote plan metadata uses task-free `PlanPayload` and ignores server-only endpoint fields when reconstructing core `Plan` values."

patterns-established:
  - "Plan-level profile verification commands persist as ordered JSONB arrays using name, command, required, and source."
  - "Transport plan metadata can include verification commands without shipping sibling tasks."

requirements-completed: [VER-01]

# Metrics
duration: 6 min
completed: 2026-05-08
---

# Phase 09 Plan 02: Plan Verification Persistence and Transport Summary

**Profile verification commands now persist through Postgres import/export and travel over the task-free plan transport surface.**

## Performance

- **Duration:** 6 min
- **Started:** 2026-05-08T15:31:37Z
- **Completed:** 2026-05-08T15:37:51Z
- **Tasks:** 2
- **Files modified:** 12

## Accomplishments

- Added Alembic revision `015_plan_verification_commands` and schema.sql parity for `plans.verification_commands JSONB NOT NULL DEFAULT '[]'::jsonb`.
- Wired `whilly plan import/export` to store ordered verification command metadata and reconstruct typed `Plan.verification_commands`.
- Extended transport schemas, server endpoint, and remote client so workers can fetch task-free plan metadata with verification commands.

## Task Commits

Each TDD step was committed atomically:

1. **Task 1 RED: plan verification persistence tests** - `1d26149` (test)
2. **Task 1 GREEN: Postgres persistence and import/export support** - `80bc074` (feat)
3. **Task 2 RED: plan transport metadata tests** - `fdc715b` (test)
4. **Task 2 GREEN: transport schema, endpoint, and client support** - `04cf50c` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/adapters/db/migrations/versions/015_plan_verification_commands.py` - Adds/drops the `plans.verification_commands` JSONB column.
- `whilly/adapters/db/schema.sql` - Documents the reference DDL for ordered plan-level verification metadata.
- `whilly/cli/plan.py` - Persists verification commands on import and rebuilds typed metadata on export.
- `whilly/adapters/transport/schemas.py` - Adds `VerificationCommandPayload` and `PlanPayload.to_plan()`.
- `whilly/adapters/transport/server.py` - Includes `verification_commands` in `GET /api/v1/plans/{plan_id}`.
- `whilly/adapters/transport/client.py` - Adds `RemoteWorkerClient.get_plan()`.
- Focused integration/unit tests cover persistence, migration head, schema shape, client path, and JSON round-trip behavior.

## Decisions Made

- Kept re-import semantics unchanged: existing plan rows are not updated, so imported verification metadata is durable only for newly inserted plans.
- Kept the remote worker path transport-only: the client parses the plan endpoint into `PlanPayload` and never imports server, DB, or project-config modules.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. Docker-gated integration tests were skipped by the repository fixtures in this environment, but the requested command exited successfully.

## Verification

- `.venv/bin/python -m pytest -q tests/integration/test_plan_io.py tests/integration/test_alembic_015_plan_verification_commands.py tests/integration/test_alembic_full_chain.py tests/integration/test_alembic_013_work_intents.py --maxfail=1` - 3 passed, 15 skipped
- `.venv/bin/python -m pytest -q tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py --maxfail=1` - 82 passed
- `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Ready for Plan 09-03 to resolve profile and CLI verification commands into the runtime worker verification path.

## Self-Check: PASSED

- Verified created summary, migration, and Alembic 015 test files exist.
- Verified task commits `1d26149`, `80bc074`, `fdc715b`, and `04cf50c` exist.

---
*Phase: 09-profile-native-verification-wiring*
*Completed: 2026-05-08*
