---
phase: 11-ci-polling-and-bounded-repair
plan: 04
subsystem: worker-runtime
tags: [ci, repair, local-worker, verification, audit-events]

requires:
  - phase: 11-ci-polling-and-bounded-repair
    provides: CI evidence contracts from Plan 11-01, repair primitives from Plan 11-02, and durable CI/repair metadata from Plan 11-03.
provides:
  - Local `source="ci"` verification runner injection with GitHub CI polling.
  - Ordered local `ci.poll.started` and `ci.poll.result` audit events before mapped verification events.
  - Transactional local repair task insertion with `repair.attempt.requested` evidence.
  - Local bounded repair request, exhaustion escalation, and terminal repair completion events.
affects: [remote-worker-repair, compliance-reporting, phase-11-ci-runtime]

tech-stack:
  added: []
  patterns:
    - Local verification consumes `VerificationRunOutcome.ci_polls` as concrete evidence.
    - Repair creates deterministic new tasks and never releases the failed task for retry.
    - Terminal repair completion events resolve max attempts from prior requested events when available.

key-files:
  created:
    - .planning/phases/11-ci-polling-and-bounded-repair/11-04-SUMMARY.md
  modified:
    - whilly/adapters/db/repository.py
    - whilly/cli/run.py
    - whilly/worker/local.py
    - tests/unit/test_cli_run.py
    - tests/unit/test_local_worker.py

key-decisions:
  - "Local `whilly run` creates a GitHub CI poll runner only when resolved verification specs include source=\"ci\"."
  - "Local worker records `ci.poll.started` and `ci.poll.result` from `VerificationRunOutcome.ci_polls` before mapped verification result events."
  - "Bounded repair creates a new deterministic repair task or emits `repair.escalated`; it does not call `release_task()` for repair retry."
  - "Repair completion uses prior `repair.attempt.requested` payloads for `max_attempts`, falling back to the parsed attempt number."

patterns-established:
  - "Local worker runtime side effects stay audit-first: CI evidence, repair request/escalation, then normal task terminal transition."
  - "Repository repair insertion stores the repair task and requested event in one transaction."

requirements-completed: [CI-01, CI-02]

duration: 9 min 55 sec
completed: 2026-05-08T18:41:20Z
---

# Phase 11 Plan 04: Local Runtime CI Evidence And Bounded Repair Summary

**Local worker CI evidence and bounded repair runtime with transactional repair task insertion and terminal repair completion events**

## Performance

- **Duration:** 9 min 55 sec
- **Started:** 2026-05-08T18:31:25Z
- **Completed:** 2026-05-08T18:41:20Z
- **Tasks:** 2
- **Files modified:** 5 plus this summary

## Accomplishments

- Wired local `whilly run` to pass `GitHubCIPollAdapter()` into verification execution when any resolved spec uses `source="ci"`.
- Added local worker CI evidence emission from `VerificationRunOutcome.ci_polls`, preserving started/result ordering before verification result events.
- Added repository `insert_repair_task()` for same-transaction repair task insertion plus `repair.attempt.requested` evidence.
- Added local bounded repair request/escalation handling and terminal `repair.attempt.completed` evidence for `DONE` and `FAILED` repair tasks.

## Task Commits

Each TDD task was committed atomically with RED and GREEN commits:

1. **Task 1 RED: local CI runtime tests** - `ab160fe` (test)
2. **Task 1 GREEN: local CI poll evidence** - `c2e36ed` (feat)
3. **Task 2 RED: local repair runtime tests** - `5186a7c` (test)
4. **Task 2 GREEN: bounded local repair runtime** - `dc1bf5f` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/cli/run.py` - Injects a GitHub CI poll runner into local verification for `source="ci"` specs.
- `whilly/worker/local.py` - Records CI poll evidence, requests or escalates bounded repair, and emits terminal repair completion events.
- `whilly/adapters/db/repository.py` - Adds transactional repair task insertion with requested repair evidence.
- `tests/unit/test_cli_run.py` - Covers local CI poll runner injection.
- `tests/unit/test_local_worker.py` - Covers CI event ordering, poll budget payloads, repair request/escalation, no release retry, and repair completion.

## Decisions Made

- Local CI polling stays one-shot and explicit via configured verification specs; no background loop was added.
- Repair remains task-based and bounded: budget remaining creates exactly one new repair task, while exhausted budgets emit escalation evidence.
- Existing disabled-repair behavior is preserved because only failed required results with `repair_max_attempts > 0` enter the repair path.
- `repair.attempt.completed` follows the existing Plan 11-02 payload contract where payload `task_id` is the repair task id.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `make lint` initially found one Ruff formatting wrap in `whilly/worker/local.py`. Applied `python3 -m ruff format whilly/worker/local.py`, reran tests, and amended the Task 2 GREEN commit.
- The Task 2 completion tests were aligned to the existing Plan 11-02 event builder payload, which stores the repair task id in payload `task_id`.
- Pre-existing `.planning/config.json` changes and untracked `.serena/` were left untouched.

## Verification

- RED Task 1: `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py::test_async_run_passes_ci_poll_runner_for_ci_source tests/unit/test_local_worker.py::test_local_worker_records_ci_poll_events_before_verification_failure tests/unit/test_local_worker.py::test_local_worker_ci_started_event_includes_original_poll_budget tests/unit/test_local_worker.py::test_local_worker_configured_ci_status_stage_emits_ci_poll_events --maxfail=1` failed as expected because `ci_poll_runner` was `None`.
- Task 1 GREEN subset: same command - 4 passed.
- Task 1 focused gate: `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_local_worker.py --maxfail=1` - 47 passed.
- RED Task 2: `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py::test_local_worker_requests_repair_task_with_budget_remaining tests/unit/test_local_worker.py::test_local_worker_escalates_when_repair_budget_exhausted tests/unit/test_local_worker.py::test_local_worker_repair_path_does_not_release_failed_task tests/unit/test_local_worker.py::test_local_worker_records_repair_attempt_completed_on_done tests/unit/test_local_worker.py::test_local_worker_records_repair_attempt_completed_on_failed --maxfail=1` failed as expected because verification failure detail had no `repair_max_attempts`.
- Task 2 GREEN subset: same command - 5 passed.
- Task 2 focused gate: `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py --maxfail=1` - 36 passed.
- Final focused local runtime gate: `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_local_worker.py --maxfail=1` - 52 passed.
- Lint gate: `make lint` - Ruff check passed and 447 files were already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Plan 11-05 can wire the same CI evidence and bounded repair behavior through remote worker transport. The local path now proves ordered CI events, deterministic repair task creation/escalation, and terminal repair completion without same-task retry.

---
*Phase: 11-ci-polling-and-bounded-repair*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/11-ci-polling-and-bounded-repair/11-04-SUMMARY.md` exists.
- Confirmed task commits exist: `ab160fe`, `c2e36ed`, `5186a7c`, and `dc1bf5f`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task commits.
