---
phase: 10-rollback-safety-net
plan: 02
subsystem: rollback-cli
tags: [git, rollback, cli, preflight, confirmation]

requires:
  - phase: 10-rollback-safety-net
    provides: Rollback core models, Git adapter, preflight reports, backup tags, and confirmation-gated restore service.
provides:
  - Operator-facing `whilly rollback create|list|preflight|restore` command surface.
  - JSON and human renderers for rollback points, preflight reports, and restore evidence.
  - Non-TTY restore refusal without exact confirmation and no broad approval flag.
  - Lazy top-level `whilly rollback ...` dispatch with legacy CLI shim regression coverage.
affects: [10-rollback-safety-net, rollback-cli, pr-preflight, compliance]

tech-stack:
  added: []
  patterns:
    - Argparse adapter module delegating rollback operations to `whilly.rollback.service`.
    - Exact restore confirmation phrase exposed through dry-run JSON evidence before mutation.
    - Top-level CLI dispatch imports rollback command handlers only inside the rollback branch.

key-files:
  created:
    - whilly/cli/rollback.py
    - tests/integration/test_rollback_cli.py
  modified:
    - whilly/cli/__init__.py
    - tests/unit/test_cli_legacy_flag_shim.py

key-decisions:
  - "Rollback restore exposes dry-run confirmation evidence but performs reset only after exact phrase confirmation."
  - "Annotated rollback tags are peeled to their target commit in the CLI restore path before confirmation and reset."
  - "Top-level rollback dispatch remains lazy so `whilly --help` advertises rollback without importing `whilly.cli.rollback`."

patterns-established:
  - "Rollback CLI payloads reuse model `to_dict()` contracts and add only CLI-specific restore confirmation evidence."
  - "Destructive rollback restore has no `--yes`; non-interactive mutation requires `--confirm` with the exact phrase."

requirements-completed: [ROLL-01, ROLL-02, ROLL-03]

duration: 6 min 26 sec
completed: 2026-05-08
---

# Phase 10 Plan 02: Rollback CLI and Lazy Dispatch Summary

**Safe operator rollback CLI with backup tags, structured preflight JSON, dry-run confirmation evidence, and lazy top-level dispatch**

## Performance

- **Duration:** 6 min 26 sec
- **Started:** 2026-05-08T16:51:53Z
- **Completed:** 2026-05-08T16:58:19Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Added `whilly rollback create|list|preflight|restore` with exact command flags from the plan.
- Added temp Git repository integration coverage for annotated tag creation/listing, custom tag messages, dirty preflight blockers, dry-run restore evidence, non-TTY confirmation refusal, wrong confirmation refusal, and exact confirmation reset.
- Added lazy top-level dispatcher support and legacy shim coverage for rollback pass-through and help text without module import.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: Rollback CLI integration tests** - `32ca029` (test)
2. **Task 1 GREEN: Rollback CLI command surface** - `1f70dbe` (feat)
3. **Task 2 RED: Rollback dispatcher coverage** - `b96e0f1` (test)
4. **Task 2 GREEN: Lazy top-level rollback dispatch** - `535f644` (feat)
5. **Verification fix: Rollback CLI lint gate** - `befd568` (fix)

## Files Created/Modified

- `whilly/cli/rollback.py` - Argparse command surface, JSON/human renderers, exact restore confirmation flow, and annotated-tag peeling for restore.
- `whilly/cli/__init__.py` - Top-level rollback help text and lazy dispatch branch.
- `tests/integration/test_rollback_cli.py` - Real temp Git repo coverage for create, list, preflight, dry-run restore, and confirmed restore.
- `tests/unit/test_cli_legacy_flag_shim.py` - Dispatcher regression coverage for rollback pass-through and help without importing rollback.

## Decisions Made

- Dry-run restore computes and returns `confirmation_phrase` while passing the exact phrase internally to the service so no reset occurs.
- CLI restore peels tag/ref targets with `^{}` before confirmation because annotated rollback tags otherwise resolve to tag objects instead of commit SHAs.
- The top-level help text includes rollback, but `whilly.cli.rollback` is imported only when `cmd == "rollback"`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Peeled annotated rollback tags before restore**
- **Found during:** Task 1 (rollback CLI command surface)
- **Issue:** `git rev-parse <annotated-tag>` returned the tag object SHA, so dry-run restore evidence targeted the wrong object.
- **Fix:** The CLI restore path resolves and passes `<target>^{}` so annotated tags are peeled to their target commit while preserving the operator-supplied target in output.
- **Files modified:** `whilly/cli/rollback.py`
- **Verification:** `.venv/bin/python -m pytest -q tests/integration/test_rollback_cli.py --maxfail=1`
- **Committed in:** `1f70dbe`

**2. [Rule 3 - Blocking] Fixed final ruff lint failure**
- **Found during:** Final verification
- **Issue:** Ruff reported one `F541` lint error from a literal string accidentally written as an f-string.
- **Fix:** Replaced the f-string with a plain string literal.
- **Files modified:** `whilly/cli/rollback.py`
- **Verification:** `.venv/bin/python -m ruff check whilly/rollback whilly/cli/rollback.py whilly/cli/__init__.py tests/unit/test_rollback.py tests/integration/test_rollback_cli.py tests/unit/test_cli_legacy_flag_shim.py`
- **Committed in:** `befd568`

---

**Total deviations:** 2 auto-fixed (1 bug, 1 blocking issue)
**Impact on plan:** Both fixes were required for the planned behavior and verification gates. No architectural changes or scope expansion.

## Issues Encountered

- Pre-existing untracked `.serena/` was present before and after execution and was left untouched.

## Verification

- `.venv/bin/python -m pytest -q tests/integration/test_rollback_cli.py --maxfail=1` - `6 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_cli_legacy_flag_shim.py --maxfail=1` - `43 passed`
- `.venv/bin/python -m pytest -q tests/integration/test_rollback_cli.py tests/unit/test_cli_legacy_flag_shim.py --maxfail=1` - `49 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_rollback.py tests/integration/test_rollback_cli.py tests/unit/test_cli_legacy_flag_shim.py --maxfail=1` - `62 passed`
- `.venv/bin/lint-imports --config .importlinter` - `2 kept, 0 broken`
- `.venv/bin/python -m ruff check whilly/rollback whilly/cli/rollback.py whilly/cli/__init__.py tests/unit/test_rollback.py tests/integration/test_rollback_cli.py tests/unit/test_cli_legacy_flag_shim.py` - `All checks passed`

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

The rollback CLI is operator-visible and ready for Plan 10-03 to wire preflight into the PR push path and compliance evidence. Remaining Phase 10 completion still depends on push-path and compliance integration.

## Self-Check: PASSED

- Verified created files exist: `whilly/cli/rollback.py`, `tests/integration/test_rollback_cli.py`, `.planning/phases/10-rollback-safety-net/10-02-SUMMARY.md`.
- Verified task commits exist: `32ca029`, `1f70dbe`, `b96e0f1`, `535f644`, `befd568`.

---
*Phase: 10-rollback-safety-net*
*Completed: 2026-05-08*
