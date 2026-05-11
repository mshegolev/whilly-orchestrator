---
phase: 10-rollback-safety-net
plan: 01
subsystem: rollback-safety
tags: [git, rollback, preflight, safety]

requires:
  - phase: 09-profile-native-verification-wiring
    provides: Profile-native verification and current hardening context for refusal-first safety gates.
provides:
  - Typed rollback point, worktree, protection, preflight, and restore-result contracts.
  - Git subprocess adapter using list argv, explicit cwd, timeout, and captured output.
  - Backup tag creation/listing, preflight report construction, exact restore confirmation, and clean-worktree restore gating.
affects: [10-rollback-safety-net, rollback-cli, pr-preflight, compliance]

tech-stack:
  added: []
  patterns:
    - Frozen slot dataclasses for JSON-ready rollback evidence.
    - GitClient boundary for all rollback Git subprocess calls.
    - Refusal-first restore flow before any destructive reset.

key-files:
  created:
    - whilly/rollback/__init__.py
    - whilly/rollback/models.py
    - whilly/rollback/git_ops.py
    - whilly/rollback/service.py
    - tests/unit/test_rollback.py
  modified: []

key-decisions:
  - "Rollback restore refuses dirty worktrees before confirmation or reset."
  - "Missing branch-protection evidence is reported as unknown with a warning, never as unprotected."
  - "Rollback points are annotated Git tags under whilly/rollback/ and are created without force replacement."

patterns-established:
  - "Rollback service boundary: whilly.rollback.service orchestrates Git through GitClient only."
  - "Machine-readable evidence: every public rollback model exposes to_dict() for CLI, PR sink, and compliance consumers."

requirements-completed: [ROLL-01, ROLL-02, ROLL-03]

duration: 7 min 6 sec
completed: 2026-05-08
---

# Phase 10 Plan 01: Rollback Core Models and Service Summary

**Typed rollback safety-net core with annotated backup tags, JSON-ready preflight evidence, and clean-worktree restore gating**

## Performance

- **Duration:** 7 min 6 sec
- **Started:** 2026-05-08T16:40:15Z
- **Completed:** 2026-05-08T16:47:21Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments

- Added `whilly.rollback` contracts for rollback points, worktree state, protection signals, preflight reports, and restore results.
- Added `GitClient` and `GitCommandResult` so rollback Git calls use list argv, explicit cwd, timeouts, captured output, and no shell.
- Implemented annotated rollback tag creation/listing, structured preflight reports, exact restore confirmation, dry-run restore, and dirty-worktree refusal before `git reset --hard`.
- Added unit coverage for model dictionaries, Git adapter behavior, rollback tags, protection handling, dirty restore refusal, exact confirmation, dry-run behavior, and hidden-cleanup avoidance.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: Rollback contracts and Git adapter tests** - `7576199` (test)
2. **Task 1 GREEN: Rollback contracts and Git adapter** - `89a4024` (feat)
3. **Task 2 RED: Rollback service tests** - `22649fe` (test)
4. **Task 2 GREEN: Rollback service safety net** - `87f471e` (feat)

## Files Created/Modified

- `whilly/rollback/__init__.py` - Public exports for rollback models, Git adapter, service functions, and `RollbackError`.
- `whilly/rollback/models.py` - Frozen slot dataclasses and `to_dict()` contracts for rollback evidence.
- `whilly/rollback/git_ops.py` - `GitClient`, `GitCommandResult`, and `RollbackError`.
- `whilly/rollback/service.py` - Backup tag, preflight, confirmation phrase, and restore service logic.
- `tests/unit/test_rollback.py` - Unit tests for contracts, adapter behavior, and refusal-first service invariants.

## Decisions Made

- Dirty tracked or untracked porcelain entries block push, merge, and restore preflight; restore raises before reset.
- Missing protection probes and probe failures produce `ProtectionSignal(status="unknown")` with warnings rather than blockers.
- Backup points are local annotated tags and existing names are not overwritten with `-f`.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- Pre-existing uncommitted `.planning/config.json` and untracked `.serena/` were present before implementation and were left untouched.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_rollback.py --maxfail=1` - `13 passed`
- `.venv/bin/lint-imports --config .importlinter` - `2 kept, 0 broken`
- `.venv/bin/python -m ruff check whilly/rollback tests/unit/test_rollback.py` - `All checks passed`

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

The reusable rollback core is ready for Plan 10-02 to add the `whilly rollback` CLI and Plan 10-03 to wire PR push preflight and compliance evidence. Full Phase 10 operator-visible completion still depends on those later plans.

## Self-Check: PASSED

- Verified created files exist.
- Verified task commits exist: `7576199`, `89a4024`, `22649fe`, `87f471e`.

---
*Phase: 10-rollback-safety-net*
*Completed: 2026-05-08*
