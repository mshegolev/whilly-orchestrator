---
phase: 11-ci-polling-and-bounded-repair
plan: 03
subsystem: verification
tags: [ci, project-config, verification, repair-budget, transport]

requires:
  - phase: 11-ci-polling-and-bounded-repair
    provides: CI verification dispatch from Plan 11-01 and repair budget primitives from Plan 11-02.
  - phase: 09-profile-native-verification-wiring
    provides: Plan-level verification command metadata across local, DB, and remote payloads.
provides:
  - Durable `source` and `repair_max_attempts` metadata on verification commands.
  - Filesystem, Postgres import/export, and remote transport round-trip preservation for CI verification metadata.
  - Project config validation for `source="ci"` commands and non-negative repair budgets.
  - Configured `ci_status` sink stages that emit concrete `source="ci"` `ci://...` verification commands.
affects: [worker-verification, project-config, plan-transport, phase-11-runtime-repair]

tech-stack:
  added: []
  patterns:
    - "`source=\"ci\"` verification entries are validated as CI targets and bypass shell command scanning."
    - "`repair_max_attempts` defaults to 0 and is serialized explicitly in canonical verification metadata."
    - "`ci_status` sinks generate executable verification commands without PR mutation, auto-merge, or background polling."

key-files:
  created: []
  modified:
    - whilly/core/models.py
    - whilly/adapters/filesystem/plan_io.py
    - whilly/adapters/transport/schemas.py
    - whilly/ci/verification.py
    - whilly/cli/plan.py
    - whilly/pipeline/verification.py
    - whilly/pipeline/sinks.py
    - whilly/project_config/models.py
    - whilly/project_config/loader.py
    - whilly/project_config/plan_builder.py
    - tests/unit/test_plan_io.py
    - tests/integration/test_plan_io.py
    - tests/unit/test_transport_schemas.py
    - tests/unit/test_project_config.py
    - tests/unit/test_configured_sinks.py

key-decisions:
  - "Verification command metadata now carries repair_max_attempts explicitly with a default of 0 for old plan compatibility."
  - "Project-config source=\"ci\" commands require ci:// targets and bypass shell scanning because they are not shell commands."
  - "Configured ci_status sinks emit both a sink-stage task and a plan-level source=\"ci\" verification command so current workers can execute the CI poll path."

patterns-established:
  - "Plan IO and transport payloads use the same verification metadata shape: name, command, required, source, repair_max_attempts."
  - "Configured ci_status sink tasks remain evidence-only and do not trigger PR creation, mutation, auto-merge, or background loops."

requirements-completed: [CI-01, CI-02]

duration: 9 min
completed: 2026-05-08T18:27:20Z
---

# Phase 11 Plan 03: CI And Repair Metadata Contracts Summary

**Executable CI verification metadata with durable repair budgets across plan files, DB import/export, remote transport, and configured ci_status sinks**

## Performance

- **Duration:** 9 min
- **Started:** 2026-05-08T18:18:21Z
- **Completed:** 2026-05-08T18:27:20Z
- **Tasks:** 2
- **Files modified:** 15 plus this summary

## Accomplishments

- Added `repair_max_attempts` to core verification commands, verification specs/results, filesystem plan IO, DB import/export helpers, and remote plan payloads.
- Preserved old plan JSON by defaulting missing `source` to `profile` and missing `repair_max_attempts` to `0`.
- Extended project config verification commands with `source` and `repair_max_attempts` validation.
- Added `ci_status` sink support that produces concrete `source="ci"` commands with `ci://...` targets.
- Added regression coverage for plan IO, Postgres import/export, remote transport, config validation, and configured sink generation.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: metadata round-trip tests** - `2614aa1` (test)
2. **Task 1 GREEN: plan metadata contracts** - `83af28d` (feat)
3. **Task 2 RED: ci_status config tests** - `2d99a6d` (test)
4. **Task 2 GREEN: ci_status metadata generation** - `71417d2` (feat)
5. **Task 2 REFACTOR: Ruff formatting** - `a907bf2` (refactor)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/core/models.py` - Adds `repair_max_attempts` to `VerificationCommand`.
- `whilly/adapters/filesystem/plan_io.py` - Parses and serializes `source` plus `repair_max_attempts`.
- `whilly/adapters/transport/schemas.py` - Preserves repair budget metadata in remote `PlanPayload` conversion.
- `whilly/ci/verification.py` - Carries repair budget metadata through CI verification results.
- `whilly/cli/plan.py` - Preserves repair budgets in Postgres import/export verification command JSON.
- `whilly/pipeline/verification.py` - Copies source and repair budget metadata from dataclass and dict command-like values.
- `whilly/project_config/models.py` - Adds source and repair budget fields to configured verification commands.
- `whilly/project_config/loader.py` - Validates CI sources, CI targets, and repair budgets.
- `whilly/project_config/plan_builder.py` - Emits configured `ci_status` stages and executable CI verification commands.
- `whilly/pipeline/sinks.py` - Defines `ci_status` sink constants.
- `tests/unit/test_plan_io.py` - Covers local plan metadata round-trip and old defaults.
- `tests/integration/test_plan_io.py` - Covers Postgres import/export preservation.
- `tests/unit/test_transport_schemas.py` - Covers remote payload preservation.
- `tests/unit/test_project_config.py` - Covers config validation for CI source and repair budget.
- `tests/unit/test_configured_sinks.py` - Covers configured `ci_status` sink generation.

## Decisions Made

- Missing repair budgets remain compatible by defaulting to `0`; serialized canonical metadata includes the explicit zero.
- `source="ci"` config values must begin with `ci://` and are not shell-scanned or shell-executed.
- `ci_status` sinks emit a task-local verification command for stage metadata and a plan-level verification command for current worker execution.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `make lint` initially failed Ruff format check for `whilly/project_config/loader.py` and `whilly/project_config/plan_builder.py`. Applied Ruff formatting and committed it as `a907bf2`; the rerun passed.
- Pre-existing workspace changes in `.planning/config.json` and untracked `.serena/` were left untouched.

## Verification

- Task 1 RED: `.venv/bin/python -m pytest -q tests/unit/test_plan_io.py tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py --maxfail=1` failed as expected because `VerificationCommand` had no `repair_max_attempts`.
- Task 1 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_plan_io.py tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py --maxfail=1` - 76 passed, 10 skipped.
- Task 1 compatibility check: `.venv/bin/python -m pytest -q tests/unit/test_verification_runner.py --maxfail=1` - 14 passed.
- Task 2 RED: `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_configured_sinks.py --maxfail=1` failed as expected because CI source commands still reached shell scanning.
- Task 2 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_configured_sinks.py --maxfail=1` - 41 passed.
- Post-refactor Task 2 check: `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_configured_sinks.py --maxfail=1` - 41 passed.
- Focused plan gate: `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_configured_sinks.py --maxfail=1` - 117 passed, 10 skipped.
- Lint gate: `make lint` - Ruff check passed and 447 files were already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Plan 11-04 can consume durable CI source and repair budget metadata from plan files, DB import/export, remote transport payloads, and configured project profiles. Runtime repair wiring should keep the same boundaries: no PR mutation, no auto-merge, and no background polling loop.

---
*Phase: 11-ci-polling-and-bounded-repair*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/11-ci-polling-and-bounded-repair/11-03-SUMMARY.md` exists.
- Confirmed task commits exist: `2614aa1`, `83af28d`, `2d99a6d`, `71417d2`, and `a907bf2`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task commits.
