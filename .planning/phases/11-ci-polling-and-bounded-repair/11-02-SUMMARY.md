---
phase: 11-ci-polling-and-bounded-repair
plan: 02
subsystem: repair
tags: [repair, ci, verification, audit-events, bounded-retry]

requires:
  - phase: 11-ci-polling-and-bounded-repair
    provides: CI poll evidence and `source="ci"` verification dispatch from Plan 11-01.
  - phase: 09-profile-native-verification-wiring
    provides: Worker verification outcome and audit-event patterns.
provides:
  - Typed repair budget, trigger, and decision contracts.
  - Pure bounded repair policy with deterministic repair task ids.
  - `repair.attempt.requested`, `repair.attempt.completed`, and `repair.escalated` event builders.
  - Deterministic repair task construction with no failed-task dependency.
affects: [worker-repair-runtime, remote-repair-runtime, compliance-reporting]

tech-stack:
  added: []
  patterns:
    - Repair defaults to disabled when no positive attempt budget is configured.
    - Repair policy requests one deterministic next task while budget remains, otherwise escalates.
    - Repair tasks are new pending tasks with empty dependencies; runtime repair must not release the failed task.

key-files:
  created:
    - whilly/repair/__init__.py
    - whilly/repair/models.py
    - whilly/repair/policy.py
    - whilly/repair/events.py
    - whilly/repair/tasks.py
    - tests/unit/test_repair_loop.py
  modified: []

key-decisions:
  - "RepairBudget(max_attempts=0) and negative budgets are disabled; there is no implicit repair budget."
  - "Budgeted repair creates deterministic `<orig-task-id>-repair-N` task ids and never releases the failed task."
  - "Repair tasks use empty dependencies so a failed original task cannot block the repair task under future scheduler rules."
  - "Repair task prompts describe trigger metadata and budget, while detailed failure evidence stays in audit events."

patterns-established:
  - "Repair events are pure PipelineTaskEvent builders with bounded JSON payloads."
  - "Repair task construction inherits key files, priority, and repo target but not original dependencies, version, or raw descriptions."

requirements-completed: [CI-02]

duration: 5 min
completed: 2026-05-08T18:14:03Z
---

# Phase 11 Plan 02: Bounded Repair Primitives And Escalation Evidence Summary

**Bounded repair contracts with deterministic repair-task creation and audit-ready request, completion, and escalation events**

## Performance

- **Duration:** 5 min
- **Started:** 2026-05-08T18:08:56Z
- **Completed:** 2026-05-08T18:14:03Z
- **Tasks:** 2
- **Files modified:** 6 plus this summary

## Accomplishments

- Added frozen `RepairBudget`, `RepairTrigger`, and `RepairDecision` contracts.
- Added pure policy helpers for disabled, exhausted, and next-attempt repair decisions.
- Added repair attempt request, repair attempt completion, and repair escalation event builders.
- Added deterministic repair task construction that inherits safe metadata and uses `dependencies=()`.
- Added unit coverage for budget boundaries, nested repair suffix parsing, event payloads, and no failed-task dependency.

## Task Commits

Each TDD task was committed atomically with RED and GREEN commits:

1. **Task 1 RED: repair policy tests** - `74e6e1e` (test)
2. **Task 1 GREEN: repair budget policy** - `1ba19fe` (feat)
3. **Task 2 RED: repair event/task tests** - `407b3db` (test)
4. **Task 2 GREEN: repair events and tasks** - `227eb1c` (feat)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/repair/models.py` - Defines repair budget, trigger, and decision value objects.
- `whilly/repair/policy.py` - Implements budget decision logic and final `-repair-N` suffix parsing.
- `whilly/repair/events.py` - Builds repair request, completion, and escalation audit events.
- `whilly/repair/tasks.py` - Builds deterministic pending repair tasks with empty dependencies.
- `whilly/repair/__init__.py` - Exports the public repair primitive surface.
- `tests/unit/test_repair_loop.py` - Covers repair budgets, task ids, event payloads, and task construction.

## Decisions Made

- Disabled repair is represented by `max_attempts <= 0`, so existing verification failure behavior remains unchanged until a positive budget is configured.
- Repair attempts create new tasks named `<orig-task-id>-repair-N`; no repair primitive retries by releasing or reclaiming the same failed task.
- Repair task descriptions omit raw original descriptions and provider output; downstream runtimes should use audit events for failure evidence.
- `repair.attempt.completed` exists now as a primitive for Plans 11-04 and 11-05 to emit when repair tasks reach `DONE` or `FAILED`.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. Pre-existing workspace changes in `.planning/config.json` and untracked `.serena/` were left untouched.

## Verification

- RED Task 1: `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py::test_repair_disabled_escalates_without_task_request tests/unit/test_repair_loop.py::test_repair_budget_requests_next_attempt tests/unit/test_repair_loop.py::test_repair_budget_escalates_when_exhausted tests/unit/test_repair_loop.py::test_parse_nested_repair_task_id_uses_final_suffix --maxfail=1` failed as expected because `whilly.repair` did not exist.
- Task 1 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py --maxfail=1` - 4 passed.
- Task 1 acceptance subset: `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py::test_repair_disabled_escalates_without_task_request tests/unit/test_repair_loop.py::test_repair_budget_requests_next_attempt tests/unit/test_repair_loop.py::test_repair_budget_escalates_when_exhausted tests/unit/test_repair_loop.py::test_parse_nested_repair_task_id_uses_final_suffix --maxfail=1` - 4 passed.
- Task 1 static acceptance: `rg -n "RepairBudget|RepairTrigger|RepairDecision|decide_repair|repair_budget_exhausted|repair_disabled" whilly/repair tests/unit/test_repair_loop.py` found the policy contract.
- RED Task 2: `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py --maxfail=1` failed as expected because repair event/task exports were missing.
- Task 2 GREEN: `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py --maxfail=1` - 9 passed.
- Task 2 static acceptance: `rg -n "repair.attempt.requested|repair.attempt.completed|repair.escalated|build_repair_task|dependencies=\\(\\)" whilly/repair tests/unit/test_repair_loop.py` found the event and task contracts.
- No same-task retry primitive: `rg -n "release_task\\(|client\\.release\\(" whilly/repair tests/unit/test_repair_loop.py` returned no matches.
- Focused repair primitive gate: `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py --maxfail=1` - 9 passed.
- Focused Phase 11 gate: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py --maxfail=1` - 16 passed.
- Lint gate: `make lint` - Ruff check passed and 447 files were already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Plan 11-03 can preserve CI/repair metadata and generate executable CI status verification commands. Plans 11-04 and 11-05 can wire these primitives into local and remote worker runtime paths without inventing task statuses or release-based retry behavior.

---
*Phase: 11-ci-polling-and-bounded-repair*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/11-ci-polling-and-bounded-repair/11-02-SUMMARY.md` exists.
- Confirmed repair primitive files exist under `whilly/repair/` and `tests/unit/test_repair_loop.py` exists.
- Confirmed task commits exist: `74e6e1e`, `1ba19fe`, `407b3db`, and `227eb1c`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task commits.
