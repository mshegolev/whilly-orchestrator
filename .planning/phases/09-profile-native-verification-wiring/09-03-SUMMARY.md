---
phase: 09-profile-native-verification-wiring
plan: 03
subsystem: verification
tags: [profile-verification, cli-verification, worker-runtime, audit-events]

# Dependency graph
requires:
  - phase: 09-profile-native-verification-wiring
    provides: Plan 09-02 persisted and transported ordered plan-level verification metadata.
provides:
  - Source-aware verification command specs, results, and audit event payloads.
  - Shared `resolve_verification_specs()` helper for profile-native and explicit CLI commands.
  - Local `whilly run` composition using profile commands before required and optional CLI flags.
affects: [09-profile-native-verification-wiring, local-worker-verification, verification-audit]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Verification command sources are explicit as `profile` or `cli`.
    - Runtime verification command resolution is centralized in `whilly.pipeline.verification`.

key-files:
  created:
    - .planning/phases/09-profile-native-verification-wiring/09-03-SUMMARY.md
  modified:
    - whilly/pipeline/verification.py
    - whilly/cli/run.py
    - tests/unit/test_verification_runner.py
    - tests/unit/test_cli_run.py

key-decisions:
  - "Profile-native verification commands run before explicit required CLI commands and explicit optional CLI commands."
  - "Verification audit result payloads carry `source` while continuing to redact command and output details."

patterns-established:
  - "Use `resolve_verification_specs(profile_commands=..., required_cli=..., optional_cli=...)` at runtime composition boundaries."
  - "Default source for legacy command-like values is `cli` unless a source attribute is present."

requirements-completed: [VER-01]

# Metrics
duration: 6 min
completed: 2026-05-08
---

# Phase 09 Plan 03: Source-aware Verification Runtime Summary

**Profile-native and explicit CLI verification commands now resolve through one source-aware local runtime path.**

## Performance

- **Duration:** 6 min
- **Started:** 2026-05-08T15:42:28Z
- **Completed:** 2026-05-08T15:47:35Z
- **Tasks:** 1
- **Files modified:** 5

## Accomplishments

- Added `profile` and `cli` source metadata to verification specs, results, and result audit event payloads.
- Added `resolve_verification_specs()` to deterministically union profile-native, required CLI, and optional CLI commands.
- Wired local `whilly run` to use `plan.verification_commands` plus explicit CLI verification flags without replacing CLI-only behavior.
- Extended focused unit tests for source preservation, blocked and timed-out results, event payloads, and local runtime composition.

## Task Commits

This TDD task was committed atomically:

1. **Task 1 RED: source-aware verification tests** - `3dc1c38` (test)
2. **Task 1 GREEN: source-aware verification resolution** - `f58a1d8` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/pipeline/verification.py` - Adds source constants, source-aware specs/results/events, preserved source copying, and shared command resolution.
- `whilly/cli/run.py` - Resolves local worker verification specs from plan metadata and CLI flags through the shared helper.
- `tests/unit/test_verification_runner.py` - Covers source-aware resolution, result preservation, blocked/timeout preservation, and event payloads.
- `tests/unit/test_cli_run.py` - Covers local runtime creation for profile-only commands, profile/CLI ordering, and CLI-only compatibility.
- `.planning/phases/09-profile-native-verification-wiring/09-03-SUMMARY.md` - Captures execution results.

## Decisions Made

- Kept user-facing `NAME=COMMAND` parsing behavior unchanged while moving resolution into `whilly.pipeline.verification`.
- Kept legacy command-like inputs compatible by defaulting missing source metadata to `cli`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed premature verification outcome return**
- **Found during:** Task 1 GREEN implementation
- **Issue:** The first implementation accidentally placed `return VerificationRunOutcome(...)` inside the command loop, which would have stopped multi-command verification after the first command.
- **Fix:** Moved the aggregate return after the loop so all resolved commands execute sequentially.
- **Files modified:** `whilly/pipeline/verification.py`
- **Verification:** `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_verification_runner.py --maxfail=1` passed with 26 tests.
- **Committed in:** `f58a1d8`

---

**Total deviations:** 1 auto-fixed (Rule 1 - Bug)
**Impact on plan:** No scope change; the fix preserves the existing multi-command verification contract.

## Issues Encountered

- The worktree contained unrelated untracked `.serena/`; it was left untouched per the plan ownership boundary.

## Verification

- RED check: `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_verification_runner.py --maxfail=1` failed during collection because `CLI_VERIFICATION_SOURCE` was not implemented yet.
- `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_verification_runner.py --maxfail=1` - 26 passed.
- `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.
- `.venv/bin/python -m ruff check whilly/pipeline/verification.py whilly/cli/run.py tests/unit/test_verification_runner.py tests/unit/test_cli_run.py` - All checks passed.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Ready for Plan 09-04 to wire remote worker composition, source-aware failure detail, and compliance evidence on top of the shared resolver.

## Self-Check: PASSED

- Confirmed `.planning/phases/09-profile-native-verification-wiring/09-03-SUMMARY.md` exists.
- Confirmed task commits `3dc1c38` and `f58a1d8` exist.

---
*Phase: 09-profile-native-verification-wiring*
*Completed: 2026-05-08*
