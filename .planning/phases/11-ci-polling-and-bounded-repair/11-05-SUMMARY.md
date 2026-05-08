---
phase: 11-ci-polling-and-bounded-repair
plan: 05
subsystem: remote-worker-runtime
tags: [ci, repair, remote-worker, transport, audit-events]

requires:
  - phase: 11-ci-polling-and-bounded-repair
    provides: CI evidence contracts, durable repair metadata, repair primitives, and local runtime wiring from Plans 11-01 through 11-04.
provides:
  - Remote transport allowlist for `ci.*` and `repair.*` diagnostics while preserving worker human-review approval restrictions.
  - Worker-authenticated remote repair request transport that inserts one dependency-free repair task.
  - Remote worker CI poll evidence ordering from `VerificationRunOutcome.ci_polls`.
  - Remote bounded repair request/escalation and terminal repair completion evidence for `DONE` and `FAILED`.
affects: [remote-worker-repair, compliance-reporting, ci-runtime]

tech-stack:
  added: []
  patterns:
    - Remote repair requests create deterministic new tasks through transport; they do not release/reclaim the failed task.
    - Remote CI evidence is emitted before mapped verification result events.
    - Repair completion evidence is recorded before remote terminal complete/fail mutations.

key-files:
  created:
    - .planning/phases/11-ci-polling-and-bounded-repair/11-05-SUMMARY.md
  modified:
    - whilly/adapters/transport/server.py
    - whilly/adapters/transport/client.py
    - whilly/adapters/transport/schemas.py
    - whilly/cli/worker.py
    - whilly/worker/remote.py
    - tests/unit/test_remote_client.py
    - tests/unit/test_cli_worker.py
    - tests/unit/test_remote_worker.py
    - tests/integration/test_transport_tasks.py

key-decisions:
  - "Remote diagnostic transport accepts ci.* and repair.* evidence but keeps workers limited to human_review.required within the human_review.* family."
  - "Remote repair requests use a dedicated request_repair transport call and dependency-free task payload instead of client.release() retry behavior."
  - "Remote worker records CI poll started/result evidence from VerificationRunOutcome.ci_polls before mapped verification events."
  - "Remote terminal repair tasks emit repair.attempt.completed before complete_task or fail_task transport mutations."

patterns-established:
  - "Remote transport repair payloads contain only repair-task fields needed to insert a new pending task."
  - "Remote repair max-attempt completion evidence is recovered from prior repair.attempt.requested events when available."

requirements-completed: [CI-01, CI-02]

duration: 9 min 56 sec
completed: 2026-05-08T18:55:02Z
---

# Phase 11 Plan 05: Remote Transport And Worker CI Repair Runtime Summary

**Remote worker CI evidence and bounded repair transport with deterministic repair task requests and terminal completion audit events**

## Performance

- **Duration:** 9 min 56 sec
- **Started:** 2026-05-08T18:45:06Z
- **Completed:** 2026-05-08T18:55:02Z
- **Tasks:** 2
- **Files modified:** 9 plus this summary

## Accomplishments

- Added `ci.*` and `repair.*` to the remote diagnostic event allowlist while keeping worker-forged `human_review.approved`, `human_review.rejected`, and `human_review.changes_requested` rejected.
- Added remote repair request schemas, client method, and `POST /tasks/{task_id}/repair` endpoint backed by `TaskRepository.insert_repair_task()`.
- Wired remote `whilly-worker` to pass `GitHubCIPollAdapter()` when resolved verification specs include `source="ci"`.
- Wired remote worker CI poll evidence emission before mapped verification result events.
- Added remote bounded repair request/escalation handling and terminal repair completion events for repair tasks that finish `DONE` or `FAILED`.

## Task Commits

Each TDD task was committed atomically with RED and GREEN commits:

1. **Task 1 RED: remote repair transport tests** - `d36e4be` (test)
2. **Task 1 GREEN: remote repair transport** - `604d34c` (feat)
3. **Task 2 RED: remote CI repair runtime tests** - `6cd43f1` (test)
4. **Task 2 GREEN: remote CI repair runtime** - `6279d3c` (feat)
5. **Task 2 REFACTOR: Ruff formatting** - `d25464a` (refactor)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/adapters/transport/server.py` - Accepts CI/repair diagnostics and exposes worker-authenticated repair insertion.
- `whilly/adapters/transport/client.py` - Adds `repair_path()` and `RemoteWorkerClient.request_repair()`.
- `whilly/adapters/transport/schemas.py` - Adds repair request/response wire models.
- `whilly/cli/worker.py` - Injects GitHub CI polling for remote verification specs with `source="ci"`.
- `whilly/worker/remote.py` - Emits ordered CI evidence, requests or escalates bounded repair, and records repair completion events.
- `tests/unit/test_remote_client.py` - Covers CI/repair diagnostic client payloads and repair request transport.
- `tests/unit/test_cli_worker.py` - Covers CI poll runner injection in the remote worker CLI.
- `tests/unit/test_remote_worker.py` - Covers CI ordering, configured ci_status, repair request/escalation, no release retry, and terminal repair completion.
- `tests/integration/test_transport_tasks.py` - Covers server diagnostic allowlist, human-review guard preservation, and repair task insertion/rejection.

## Decisions Made

- Remote repair task creation remains explicit transport behavior through `request_repair()`; retrying by calling `client.release()` is not part of repair handling.
- Remote CI started events use the original `CIPollSpec` budget, while result events use provider `CIPollResult` evidence.
- Repair task payloads sent over transport are dependency-free and omit complete/fail/release/approval mutation semantics.
- Remote repair completion falls back to the parsed repair attempt number when prior request evidence is unavailable.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `make lint` initially failed Ruff format-check for `whilly/worker/remote.py`, `tests/unit/test_remote_worker.py`, and `tests/integration/test_transport_tasks.py`. Applied Ruff formatting, reran focused tests, and committed the cleanup as `d25464a`.
- Pre-existing `.planning/config.json` changes and untracked `.serena/` were left untouched.

## Verification

- RED Task 1: `.venv/bin/python -m pytest -q tests/unit/test_remote_client.py tests/integration/test_transport_tasks.py --maxfail=1` failed as expected because `repair_path` did not exist.
- Task 1 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_remote_client.py tests/integration/test_transport_tasks.py --maxfail=1` - 51 passed, 33 skipped.
- RED Task 2: `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py tests/unit/test_remote_worker.py --maxfail=1` failed as expected because no `ci_poll_runner` was passed for `source="ci"`.
- Task 2 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py tests/unit/test_remote_worker.py --maxfail=1` - 65 passed.
- Post-format focused gate: `.venv/bin/python -m pytest -q tests/unit/test_remote_client.py tests/unit/test_cli_worker.py tests/unit/test_remote_worker.py tests/integration/test_transport_tasks.py --maxfail=1` - 116 passed, 33 skipped.
- Lint gate: `make lint` - Ruff check passed and 447 files were already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Plan 11-06 can update compliance/reporting around explicit CI polling and bounded repair using remote and local runtime evidence. Remote repair now creates deterministic new repair tasks, records escalation when exhausted, and never retries a failed task through release.

---
*Phase: 11-ci-polling-and-bounded-repair*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/11-ci-polling-and-bounded-repair/11-05-SUMMARY.md` exists.
- Confirmed task commits exist: `d36e4be`, `604d34c`, `6cd43f1`, `6279d3c`, and `d25464a`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task commits.
