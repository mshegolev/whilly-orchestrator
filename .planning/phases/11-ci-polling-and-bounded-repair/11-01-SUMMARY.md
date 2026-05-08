---
phase: 11-ci-polling-and-bounded-repair
plan: 01
subsystem: verification
tags: [ci, verification, github, audit-events]

requires:
  - phase: 09-profile-native-verification-wiring
    provides: Source-aware verification command runner and profile/CLI ordering.
  - phase: 08-sandbox-and-secrets-hardening
    provides: Verification result redaction and runner guard boundaries.
provides:
  - Provider-neutral CI poll spec, result, check summary, and evidence contracts.
  - Bounded `ci.poll.started` and `ci.poll.result` audit event builders.
  - One-shot GitHub-compatible CI adapter with explicit unavailable and unauthenticated results.
  - `source="ci"` verification dispatch that bypasses shell execution and records CI evidence.
affects: [worker-verification, profile-native-verification, phase-11-repair]

tech-stack:
  added: []
  patterns:
    - CI verification commands are represented as structured evidence, not shell commands.
    - Provider failures become explicit non-success CI results.
    - Verification outcomes carry `CIPollEvidence(spec, result)` for downstream event ordering.

key-files:
  created:
    - whilly/ci/__init__.py
    - whilly/ci/models.py
    - whilly/ci/events.py
    - whilly/ci/github.py
    - whilly/ci/verification.py
    - tests/unit/test_ci_polling.py
  modified:
    - whilly/pipeline/verification.py
    - tests/unit/test_verification_runner.py

key-decisions:
  - "A verification command with source=\"ci\" dispatches to a CI poll runner before shell scanning or subprocess execution."
  - "A missing CI poll runner produces explicit unavailable CI evidence with reason ci_poll_runner_not_configured."
  - "The GitHub CI adapter is one-shot and returns non-success evidence for provider auth, availability, and timeout failures."

patterns-established:
  - "CI poll event payloads include bounded result fields while check details omit raw provider payloads."
  - "Optional CI failures map to verification.warning; required CI failures map to verification.failed."

requirements-completed: [CI-01]

duration: 5 min
completed: 2026-05-08T18:05:28Z
---

# Phase 11 Plan 01: CI Polling Primitives And Verification Source Summary

**Provider-neutral CI polling evidence with one-shot GitHub probing and `source="ci"` verification dispatch that never reaches shell execution**

## Performance

- **Duration:** 5 min
- **Started:** 2026-05-08T18:00:11Z
- **Completed:** 2026-05-08T18:05:28Z
- **Tasks:** 2
- **Files modified:** 8 plus this summary

## Accomplishments

- Added typed CI contracts for poll specs, check summaries, poll results, and evidence pairs.
- Added `ci.poll.started` and `ci.poll.result` event builders with bounded check detail.
- Added a one-shot GitHub-compatible CI adapter that classifies unauthenticated, unavailable, timeout, and unknown data as non-success evidence.
- Extended the verification runner with `CI_VERIFICATION_SOURCE`, `ci_poll_runner`, and `VerificationRunOutcome.ci_polls`.
- Added regression coverage proving `source="ci"` bypasses `asyncio.create_subprocess_shell`.

## Task Commits

Each TDD task was committed atomically with RED and GREEN commits:

1. **Task 1 RED: CI polling primitive tests** - `9757baa` (test)
2. **Task 1 GREEN: CI polling primitives** - `cfb3e02` (feat)
3. **Task 2 RED: CI verification dispatch tests** - `9d40ae0` (test)
4. **Task 2 GREEN: CI verification dispatch** - `def66f2` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/ci/models.py` - Defines `CI_VERIFICATION_SOURCE`, `CI_PROVIDER_GITHUB`, `CIPollSpec`, `CICheckSummary`, `CIPollResult`, and `CIPollEvidence`.
- `whilly/ci/events.py` - Builds `ci.poll.started` and `ci.poll.result` pipeline task events with bounded check detail.
- `whilly/ci/github.py` - Adds a monkeypatchable one-shot `GitHubCIPollAdapter` backed by `gh pr view`.
- `whilly/ci/verification.py` - Maps verification specs to CI specs and CI results to verification results.
- `whilly/ci/__init__.py` - Exports the public CI primitive surface.
- `whilly/pipeline/verification.py` - Dispatches `source="ci"` before `_run_one()` and preserves CI poll evidence.
- `tests/unit/test_ci_polling.py` - Covers CI models, events, GitHub adapter auth failures, and result mapping.
- `tests/unit/test_verification_runner.py` - Covers shell bypass, no-runner failure, and CI evidence handoff.

## Decisions Made

- `source="ci"` is a structured verification source and is not shell-scanned or shell-executed.
- Missing CI runner configuration is explicit evidence, not a fallback to executing the target string.
- GitHub support remains one-shot and adapter-local; no polling loop, sleep, or daemon behavior was introduced.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. Pre-existing workspace changes in `.planning/config.json` and untracked `.serena/` were left untouched.

## Verification

- RED Task 1: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py --maxfail=1` failed as expected because `whilly.ci` did not exist.
- Task 1 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py --maxfail=1` - 5 passed.
- Task 1 acceptance subset: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py::test_ci_poll_started_event_payload_has_target_provider_and_budget tests/unit/test_ci_polling.py::test_required_unavailable_ci_is_not_success tests/unit/test_ci_polling.py::test_optional_failed_ci_is_nonblocking tests/unit/test_ci_polling.py::test_github_adapter_reports_unauthenticated_without_success --maxfail=1` - 4 passed.
- RED Task 2: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_verification_runner.py --maxfail=1` failed as expected because `whilly.ci.verification` did not exist.
- Task 2 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_verification_runner.py --maxfail=1` - 21 passed.
- Static acceptance: `rg -n "CI_VERIFICATION_SOURCE|ci_poll_runner|ci_polls|CIPollEvidence|ci_poll_runner_not_configured" whilly/pipeline/verification.py whilly/ci/verification.py tests/unit/test_verification_runner.py` found the dispatch and evidence handoff.
- Shell-bypass regression: `rg -n "create_subprocess_shell" tests/unit/test_verification_runner.py` found the regression test.
- Hidden loop check: `rg -n "background|daemon|while True|sleep\\(" whilly/ci` returned no matches.
- Focused plan gate: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_verification_runner.py --maxfail=1` - 21 passed.
- Lint gate: `make lint` - Ruff check passed and 441 files were already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Plan 11-01 is ready for bounded repair work in later Phase 11 plans. Downstream local and remote worker wiring can consume `VerificationRunOutcome.ci_polls` without treating CI targets as shell commands.

---
*Phase: 11-ci-polling-and-bounded-repair*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/11-ci-polling-and-bounded-repair/11-01-SUMMARY.md` exists.
- Confirmed task commits exist: `9757baa`, `cfb3e02`, `9d40ae0`, and `def66f2`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task and metadata commits.
