---
phase: 22-orchestration-cluster
plan: 01
subsystem: docs
tags: [openspec, orchestration, worker-claim, asyncpg, reverse-spec]

requires:
  - phase: 21-spec-baseline-taxonomy
    provides: task-model-fsm exemplar spec + AUTHORING.md format rules
provides:
  - openspec/specs/orchestration-loop/spec.md (normative ORCH-01 capability spec)
affects: [22-orchestration-cluster, 28-forward-process-coverage-validation]

tech-stack:
  added: []
  patterns:
    - "Reverse-spec from real v4 modules, not prose narrative"
    - "Reference task-model-fsm for status FSM; do not re-specify transitions"

key-files:
  created:
    - openspec/specs/orchestration-loop/spec.md
  modified: []

key-decisions:
  - "Spec'd the v4 async worker-claim model (cli/run.py::_async_run + worker/local.py::run_local_worker), NOT the removed v3 run_plan loop"
  - "Six requirements: composition root, claim iteration ordering, idle-wait poll, termination precedence, VersionConflictError tolerance, workspace-aware runner seam"
  - "Deferred FSM transitions to task-model-fsm and budget/verification gates to their own capabilities — referenced, not duplicated"

patterns-established:
  - "Each requirement body's first line carries SHALL/MUST; every requirement has >=1 #### Scenario with WHEN/THEN"

requirements-completed: [ORCH-01]

duration: 9min
completed: 2026-06-15
---

# Phase 22 Plan 01: Orchestration Loop Spec Summary

**Normative OpenSpec capability spec for the v4 worker-claim run path — composition root (`_async_run`), per-iteration claim/start/route ordering, idle-wait poll, termination precedence, optimistic-locking race tolerance, and the workspace-aware runner seam — passing strict validation.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-06-15
- **Completed:** 2026-06-15
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments
- Wrote `openspec/specs/orchestration-loop/spec.md` reverse-spec'd from the real v4 code (`whilly/cli/run.py` and `whilly/worker/local.py`), not from the stale v3 CLAUDE.md narrative.
- Six normative requirements capturing every guaranteed v4 behavior named in the plan must_haves: composition root (pool open → idempotent worker INSERT → plan load → workspace-aware runner → run loop → close pool in finally), claim iteration ordering (claim_task PENDING→CLAIMED → start_task CLAIMED→IN_PROGRESS → runner → complete_task vs fail_task on is_complete+exit_code), idle-wait poll on empty queue, termination precedence (stop event → max_iterations → outer cancellation), VersionConflictError log-and-continue, and the workspace-prepare-before-dispatch seam with failure-as-task-failure.
- `openspec validate orchestration-loop --strict` reports "is valid" and exits 0 (zero errors, zero warnings).
- Confirmed no delta headers and no `run_plan` / `_original_cwd` references in the spec body.

## Task Commits

Each task was committed atomically:

1. **Task 1: Write orchestration-loop spec.md (ORCH-01)** - see commit below (docs)

## Files Created/Modified
- `openspec/specs/orchestration-loop/spec.md` - Normative ORCH-01 capability spec for the v4 worker-claim orchestration loop and run composition root.

## Decisions Made
- Mapped the run-command exit-code split (missing `WHILLY_DATABASE_URL` and unknown plan id → `EXIT_ENVIRONMENT_ERROR` = 2) into composition-root scenarios, grounded in `cli/run.py` constants.
- Captured the workspace-aware runner seam (`RepoTargetWorkspaceResolver.prepare` + `WORKSPACE_FAILED_EXIT_CODE` returned as a non-complete `AgentResult`) without specifying the workspace directory layout, which belongs to the `worktree-isolation` capability.
- Followed the must_haves boundary discipline: referenced `task-model-fsm` for status semantics; did not duplicate budget thresholds (Phase 27) or verification-gate internals.

## Deviations from Plan

None - plan executed exactly as written. No `whilly/` Python changes (documentation-only phase).

## Issues Encountered
None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- ORCH-01 satisfied. ORCH-03..07 (plan-json-contract, batch-planning, agent-dispatch, worktree-isolation, result-collection) remain in this phase.
- The reverse-spec-from-code + reference-don't-duplicate pattern is established for the remaining 22-cluster specs.

## Self-Check: PASSED

---
*Phase: 22-orchestration-cluster*
*Completed: 2026-06-15*
