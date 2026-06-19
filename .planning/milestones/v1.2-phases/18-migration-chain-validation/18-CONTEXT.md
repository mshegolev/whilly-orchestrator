# Phase 18: Migration Chain Validation - Context

**Gathered:** 2026-06-11
**Status:** Ready for planning
**Mode:** Infrastructure phase — smart discuss skipped (autonomous mode)

<domain>
## Phase Boundary

Prove the full Alembic migration chain (001 → 028) runs green from an empty Postgres in Docker,
and make that validation repeatable via a scripted/CI entry point instead of a one-off manual
run. Covers MIG-01 and MIG-02. Produces inspectable evidence (exit code, migration count, final
revision). Does not change schema content or add new migrations.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. Use ROADMAP
phase goal, success criteria, and codebase conventions to guide decisions.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `alembic.ini` at repo root; `script_location = whilly/adapters/db/migrations` (inside the
  package).
- 28 migration revisions in `whilly/adapters/db/migrations/versions/` (001_initial_schema →
  028_webauthn_user_handles; note the out-of-pattern `019a_plans_archived_at.py` revision id).
- `docker-compose.yml` already defines `postgres:15-alpine` (`whilly-postgres`, named volume
  `whilly_pgdata`).
- Focused migration tests exist: `tests/test_migration_018_smoke.py`,
  `tests/integration/test_alembic_013_work_intents.py`,
  `tests/integration/test_alembic_015_plan_verification_commands.py` — static/focused, not a
  full-chain Docker run.

### Established Patterns
- Codebase maps available in `.planning/codebase/` (STACK, STRUCTURE, CONVENTIONS, TESTING).
- Makefile exists but has no migrate/chain-validation target yet.
- Project constraint: verification needs focused tests first; broaden for migrations (this phase
  touches migrations, so the broader chain run is in scope by definition).

### Integration Points
- New entry point should live beside existing ops scripts/Make targets and reuse the compose
  Postgres definition (or an ephemeral container) rather than inventing a parallel DB setup.
- Evidence output should align with existing audit/report conventions (`whilly_logs/`,
  reporter patterns) where reasonable.

</code_context>

<specifics>
## Specific Ideas

No specific requirements — infrastructure phase. Success criteria from ROADMAP:
1. One command against empty Docker Postgres applies all migrations without error.
2. Re-run from a reset container reproduces the green result (idempotency proof).
3. CI entry point (script or Makefile target) requires no manual steps.
4. Chain result recorded as inspectable evidence (exit code, migration count, final revision).

</specifics>

<deferred>
## Deferred Ideas

None — discuss skipped.

</deferred>
