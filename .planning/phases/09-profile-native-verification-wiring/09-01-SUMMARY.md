---
phase: 09-profile-native-verification-wiring
plan: 01
subsystem: verification
tags: [project-config, plan-io, verification-metadata, core-model]

# Dependency graph
requires:
  - phase: 08-sandbox-and-secrets-hardening
    provides: Command scanning, redaction, and runner environment boundaries remain the verification safety baseline.
provides:
  - Pure core `VerificationCommand` metadata on `Plan`.
  - Ordered profile-native `verification_commands` in generated project-config plan JSON.
  - Filesystem plan parser and serializer round-trip support for verification command metadata.
affects: [09-profile-native-verification-wiring, plan-persistence, worker-verification]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Frozen dataclass metadata in `whilly.core` for plan-level verification commands.
    - Optional top-level canonical plan JSON metadata omitted when empty.

key-files:
  created:
    - .planning/phases/09-profile-native-verification-wiring/09-01-SUMMARY.md
  modified:
    - whilly/core/models.py
    - whilly/project_config/plan_builder.py
    - whilly/adapters/filesystem/plan_io.py
    - tests/unit/test_project_config.py
    - tests/unit/test_plan_io.py

key-decisions:
  - "Profile-native verification commands are represented as typed plan-level metadata with source `profile`."
  - "Filesystem plan JSON keeps `verification_commands` optional but validates every command item when present."

patterns-established:
  - "Generated project-config verification metadata uses exact keys: name, command, required, source."
  - "Plan serialization preserves command order and omits `verification_commands` when empty."

requirements-completed: [VER-01]

# Metrics
duration: 3 min
completed: 2026-05-08
---

# Phase 09 Plan 01: Profile-Native Verification Metadata Summary

**Typed profile verification commands now survive project-config generation and canonical filesystem plan round-trips.**

## Performance

- **Duration:** 3 min
- **Started:** 2026-05-08T15:20:57Z
- **Completed:** 2026-05-08T15:24:01Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments

- Added a pure frozen `VerificationCommand` value object and `Plan.verification_commands`.
- Generated ordered top-level `verification_commands` from `ProjectConfig.verification_commands` with source `profile`.
- Parsed, validated, serialized, and round-tripped top-level verification metadata through filesystem `plan_io`.

## Task Commits

Each TDD step was committed atomically:

1. **Task 1 RED: profile verification metadata tests** - `18c042c` (test)
2. **Task 1 GREEN: typed metadata and project-config payload support** - `115d49b` (feat)
3. **Task 2 RED: filesystem round-trip tests** - `f142901` (test)
4. **Task 2 GREEN: parser and serializer support** - `11879e1` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/core/models.py` - Added pure plan-level verification command metadata.
- `whilly/project_config/plan_builder.py` - Emits profile verification commands into generated plan payloads.
- `whilly/adapters/filesystem/plan_io.py` - Parses, validates, and serializes top-level verification metadata.
- `tests/unit/test_project_config.py` - Covers generated payload source, order preservation, empty omission, and core model shape.
- `tests/unit/test_plan_io.py` - Covers parse/serialize round-trip, defaults, omission, order preservation, and invalid metadata errors.
- `.planning/phases/09-profile-native-verification-wiring/09-01-SUMMARY.md` - Captures plan execution outcome.

## Decisions Made

- Profile-generated commands use `source="profile"` at generation time and remain source-tagged through filesystem parsing.
- Invalid command metadata fails at the canonical filesystem boundary with field-specific `PlanParseError` messages.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_project_config.py --maxfail=1` - 28 passed
- `.venv/bin/python -m pytest -q tests/unit/test_plan_io.py --maxfail=1` - 41 passed
- `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py --maxfail=1` - 69 passed
- `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Ready for Plan 09-02 to persist and expose `Plan.verification_commands` through import/export and transport metadata.

## Self-Check: PASSED

- Verified all created/modified files listed in this summary exist.
- Verified task commits `18c042c`, `115d49b`, `f142901`, and `11879e1` exist.

---
*Phase: 09-profile-native-verification-wiring*
*Completed: 2026-05-08*
