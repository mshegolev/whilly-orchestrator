---
phase: 09-profile-native-verification-wiring
plan: 04
subsystem: verification
tags: [remote-worker, profile-verification, compliance, redaction]

# Dependency graph
requires:
  - phase: 09-profile-native-verification-wiring
    provides: Plan 09-03 shared source-aware verification command resolution.
provides:
  - Remote worker metadata fetch and profile-native verification runner composition.
  - Source-aware, redacted verification failure detail for local and remote workers.
  - Separate compliance evidence for profile-native verification commands.
affects: [profile-native-verification, remote-worker-runtime, compliance-reporting]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Remote worker sessions fetch task-free plan metadata before worker execution.
    - Verification failure detail records command source and redacts command strings.

key-files:
  created:
    - .planning/phases/09-profile-native-verification-wiring/09-04-SUMMARY.md
  modified:
    - whilly/cli/worker.py
    - whilly/worker/local.py
    - whilly/worker/remote.py
    - whilly/compliance/__init__.py
    - tests/unit/test_cli_worker.py
    - tests/unit/test_local_worker.py
    - tests/unit/test_remote_worker.py
    - tests/unit/test_compliance_report.py

key-decisions:
  - "Remote worker URL-rotation sessions use a session context factory so plan metadata and verification runner state refresh per client session."
  - "Compliance reports profile-native verification commands separately from explicit CLI verification support without claiming complete profile coverage."

patterns-established:
  - "Use `RemoteWorkerClient.get_plan(plan_id)` at remote composition boundaries before building verification runners."
  - "Use `redact_secrets()` before storing verification command strings in failure detail."

requirements-completed: [VER-01]

# Metrics
duration: 9 min
completed: 2026-05-08T16:01:47Z
---

# Phase 09 Plan 04: Remote Verification Wiring Summary

**Remote workers now fetch profile plan metadata, run source-aware verification, and report profile-native compliance evidence honestly.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-05-08T15:52:27Z
- **Completed:** 2026-05-08T16:01:47Z
- **Tasks:** 2
- **Files modified:** 8 plus this summary

## Accomplishments

- Added `whilly-worker` required and optional verification flags and wired them through remote worker composition.
- Fetched remote plan metadata via `RemoteWorkerClient.get_plan(plan_id)` in static mode and per URL-rotation client session.
- Built remote verification runners with `resolve_verification_specs()` so profile commands run before explicit CLI required and optional commands.
- Added `source` plus redacted `command` to local and remote required verification failure detail.
- Split compliance evidence into explicit CLI verification enforcement and `"Profile-native verification commands"` without exhaustive-coverage wording.

## Task Commits

Each TDD task was committed atomically with RED and GREEN commits:

1. **Task 1 RED: remote verification wiring tests** - `cb312e7` (test)
2. **Task 1 GREEN: remote profile verification runner** - `6bed427` (feat)
3. **Task 2 RED: source-aware failure/compliance tests** - `c3f6d7b` (test)
4. **Task 2 GREEN: source-aware failure evidence** - `e779279` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/cli/worker.py` - Adds remote verification flags, plan metadata fetch, and shared verification runner composition.
- `whilly/worker/local.py` - Adds redacted command and source to required verification failure detail.
- `whilly/worker/remote.py` - Adds redacted command/source failure detail and URL-rotation session context refresh support.
- `whilly/compliance/__init__.py` - Adds separate profile-native verification capability and updates explicit CLI verification wording.
- `tests/unit/test_cli_worker.py` - Covers parsing, forwarding, static metadata fetch, CLI-only remote verification, and URL-rotation session metadata.
- `tests/unit/test_local_worker.py` - Covers source-aware redacted local verification failure detail.
- `tests/unit/test_remote_worker.py` - Covers source-aware redacted remote verification failure detail.
- `tests/unit/test_compliance_report.py` - Covers separate honest profile-native verification capability.

## Decisions Made

- Remote URL-rotation uses a session context factory rather than reusing stale plan metadata across rotated client sessions.
- Required verification failures continue to use `verification_failed`; no task status or transition semantics changed.
- Compliance wording states that configured profile commands feed runtime verification, not that every profile has complete tests.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The worktree contained unrelated untracked `.serena/`; it was left untouched.
- `make lint` is not clean due to out-of-scope Ruff format drift in `tests/integration/test_plan_io.py`, `whilly/adapters/transport/client.py`, and `whilly/adapters/transport/schemas.py`.
- `make test` is not clean due to out-of-scope `README.md` quickstart documentation failure in `tests/unit/test_readme_quickstart_extractable.py::test_long_running_block_is_segregated_in_readme`.

## Verification

- RED Task 1: `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py --maxfail=1` failed as expected because `whilly-worker` did not accept `--verify-command` / `--optional-verify-command`.
- Task 1 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py --maxfail=1` - 26 passed.
- Task 1 import purity: `.venv/bin/python -m pytest -q tests/unit/test_worker_entrypoint_import_purity.py tests/unit/test_cli_worker.py --maxfail=1` - 29 passed.
- Task 1 Ruff: `.venv/bin/python -m ruff check whilly/cli/worker.py whilly/worker/remote.py tests/unit/test_cli_worker.py` - all checks passed.
- RED Task 2: `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` failed as expected because fail detail still carried the raw command.
- Task 2 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` - 68 passed.
- Task 2 Ruff: `.venv/bin/python -m ruff check whilly/worker/local.py whilly/worker/remote.py whilly/compliance/__init__.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py` - all checks passed.
- Phase 9 wave verification: `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` - 94 passed.
- Broader Phase 9 verification: `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py tests/unit/test_cli_run.py tests/unit/test_verification_runner.py tests/unit/test_cli_worker.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` - 271 passed.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.
- `make lint` - failed only on out-of-scope Ruff format drift listed above; `ruff check` portion passed.
- `make test` - 2772 passed, 648 skipped, 1 out-of-scope README quickstart failure listed above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 9 is complete for profile-native verification wiring. Phase 10 can build rollback safety net behavior on top of workers that now distinguish profile-native and explicit CLI verification evidence.

---
*Phase: 09-profile-native-verification-wiring*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/09-profile-native-verification-wiring/09-04-SUMMARY.md` exists.
- Confirmed task commits `cb312e7`, `6bed427`, `c3f6d7b`, and `e779279` exist.
- Confirmed only unrelated untracked `.serena/` remains outside plan metadata updates.
