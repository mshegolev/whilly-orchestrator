---
phase: 22-orchestration-cluster
verified: 2026-06-16T00:40:00Z
status: passed
score: 7/7 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: none
  previous_score: none
warnings:
  - "REQUIREMENTS.md ORCH-02 checkbox is still [ ] (task-model-fsm was authored in Phase 21, spec exists + validates). Stale Phase-21 bookkeeping, not a Phase-22 deliverable."
  - "REQUIREMENTS.md Traceability table line 131 (ORCH-01..07 | Phase 22 | Pending) was not flipped to Done despite the 6 in-scope items being checked off in the body. Documentation inconsistency in .planning/ only; all spec deliverables exist and validate."
---

# Phase 22: Orchestration Cluster Verification Report

**Phase Goal:** The seven load-bearing orchestration contracts (orchestration-loop, task-model-fsm, plan-json-contract, batch-planning, agent-dispatch, worktree-isolation, result-collection) are captured as normative, machine-checkable OpenSpec capability specs reverse-spec'd from the REAL v4.7.0 code. ORCH-02 (task-model-fsm) was authored in Phase 21; this phase authored the remaining 6 (ORCH-01,03,04,05,06,07).
**Verified:** 2026-06-16T00:40:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #   | Truth                                                                                 | Status     | Evidence |
| --- | ------------------------------------------------------------------------------------- | ---------- | -------- |
| 1   | All 6 in-scope spec.md files exist and pass `openspec validate <slug> --strict`        | ✓ VERIFIED | All 6 validated: each printed "Specification '<slug>' is valid", exit 0. task-model-fsm (Phase 21) also validates. |
| 2   | orchestration-loop describes the v4 worker-claim loop, NOT v3 run_plan/_original_cwd   | ✓ VERIFIED | Spec grounds in `_async_run` (pool/INSERT ON CONFLICT/_select_plan_with_tasks/workspace_runner/close_pool in finally) + `run_local_worker` (claim_task→start_task→route, idle_wait, VersionConflictError, max_iterations). grep confirms NO `run_plan`/`_original_cwd` in any spec. Code: whilly/cli/run.py, whilly/worker/local.py. |
| 3   | plan-json-contract: validate_schema validates ALL tasks (no first-3 limit); Task fields match | ✓ VERIFIED | `validate_schema` (whilly/cli/__init__.py) loops `for index, task in enumerate(raw_tasks)` over every task. Spec asserts "SHALL NOT limit validation to only the first three tasks" (negation). Task no-default fields `id, phase, category, priority, description, status` match task_manager.py exactly. |
| 4   | agent-dispatch + worktree-isolation do NOT gate on WHILLY_WORKTREE; resolver model used | ✓ VERIFIED | config.py:93-94 confirms WORKTREE/USE_WORKSPACE are removed no-ops. Both specs assert their NEGATION (no dispatch/activation effect). worktree-isolation uses RepoTargetWorkspaceResolver (`.whilly_workspaces/repos/<repo>/<plan>/<task>`), not `.whilly_workspaces/{slug}/`. WorktreeManager NOT referenced in cli/run.py (confirms "not wired into live run path"). |
| 5   | batch-planning truthfully scopes plan_batches/plan_batches_llm; no v3 dispatch coupling | ✓ VERIFIED | Spec Purpose: "pure functions over the ready set; this capability specifies only the batches they return, not how a run loop consumes them." No "first-batch then re-evaluate" v3 coupling present (grep empty). Grouping/cap/fallback all match whilly/orchestrator.py. |
| 6   | result-collection: AgentResult fields + COMPLETION_MARKER exact                        | ✓ VERIFIED | result_parser.py: `@dataclass(frozen=True) AgentResult(output, usage, exit_code, is_complete)`, `COMPLETION_MARKER = "<promise>COMPLETE</promise>"`, exit_code threaded as param default 0, BACKOFF and defensive no-raise fallback all match spec. |
| 7   | Documentation-only: no whilly/ Python changes; no delta headers in any spec            | ✓ VERIFIED | `git diff 502e972^..f27894d` touches only openspec/specs/ and .planning/ — NO whilly/ files. grep for `## ADDED/MODIFIED/REMOVED/RENAMED` across all 6 specs returns NONE. |

**Score:** 7/7 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `openspec/specs/orchestration-loop/spec.md`  | ORCH-01 normative spec | ✓ VERIFIED | 6 requirements, 13 scenarios, strict-valid, grounded in v4 worker-claim path. |
| `openspec/specs/plan-json-contract/spec.md`  | ORCH-03 normative spec | ✓ VERIFIED | 7 requirements, 17 scenarios, strict-valid, fields match task_manager.py + validate_schema all-tasks. |
| `openspec/specs/batch-planning/spec.md`      | ORCH-04 normative spec | ✓ VERIFIED | 6 requirements, 9 scenarios, strict-valid, grounded in plan_batches/plan_batches_llm. |
| `openspec/specs/agent-dispatch/spec.md`      | ORCH-05 normative spec | ✓ VERIFIED | 7 requirements, 15 scenarios, strict-valid, tmux/subprocess selection + no-op-flag negation. |
| `openspec/specs/worktree-isolation/spec.md`  | ORCH-06 normative spec | ✓ VERIFIED | 7 requirements, 10 scenarios, strict-valid, RepoTargetWorkspaceResolver + WorktreeManager lifecycle. |
| `openspec/specs/result-collection/spec.md`   | ORCH-07 normative spec | ✓ VERIFIED | 6 requirements, 13 scenarios, strict-valid, AgentResult + completion marker. |
| `openspec/specs/task-model-fsm/spec.md`      | ORCH-02 (Phase 21)     | ✓ VERIFIED | 9 scenarios, strict-valid. Authored 72c7e7d (Phase 21) — out of Phase 22 scope, confirmed present. |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | -- | --- | ------ | ------- |
| orchestration-loop/spec.md | whilly/worker/local.py::run_local_worker + whilly/cli/run.py::_async_run | reverse-spec'd v4 worker-claim code | ✓ WIRED | All named symbols (claim_task/start_task/complete_task/fail_task/release_task, idle_wait, VersionConflictError, _select_plan_with_tasks, close_pool) present in code. |
| plan-json-contract/spec.md | plan_io.py + task_manager.py + cli/__init__.py::validate_schema | reverse-spec'd field+envelope+validation | ✓ WIRED | Task dataclass fields + from_dict unknown-key filter + validate_schema all-task loop verified. |
| batch-planning/spec.md | whilly/orchestrator.py (plan_batches, plan_batches_llm) | reverse-spec'd grouping | ✓ WIRED | max_parallel<=1 path (L14), overlap grouping, cap (L30), 3× fallback to plan_batches verified. |
| agent-dispatch/spec.md | tmux_runner.py + claude_cli.py + cli/run.py | reverse-spec'd selection+dispatch | ✓ WIRED | whilly-{safe_id} session, USE_TMUX, disallowedTools, BACKOFF (5,10,20,40,60), auth substrings, workspace_runner cwd injection verified. |
| worktree-isolation/spec.md | workspaces.py::RepoTargetWorkspaceResolver + worktree_runner.py::WorktreeManager | reverse-spec'd workspace+worktree lifecycle | ✓ WIRED | DEFAULT_WORKSPACE_BASE=.whilly_workspaces/repos, reused_current_cwd, RuntimeError on unregistered, .whilly_worktrees create→merge_back(cherry-pick HEAD..branch)→cleanup verified; WorktreeManager absent from cli/run.py. |
| result-collection/spec.md | whilly/adapters/runner/result_parser.py::AgentResult | reverse-spec'd parsing+completion | ✓ WIRED | frozen AgentResult/AgentUsage, COMPLETION_MARKER exact string, parse_output exit_code param verified. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| All 7 specs strict-valid | `openspec validate <slug> --strict` ×7 | "is valid", exit 0 for each | ✓ PASS |
| No delta headers | `grep -E '^## (ADDED\|MODIFIED\|REMOVED\|RENAMED)'` | none | ✓ PASS |
| No v3 run_plan/_original_cwd | `grep 'run_plan\|_original_cwd'` across specs | none | ✓ PASS |
| Documentation-only | `git diff --name-only 502e972^..f27894d \| grep ^whilly/` | none | ✓ PASS |
| Each spec ≥1 scenario | `grep -c '^#### Scenario:'` | 13/9/17/9/15/10/13 | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ----------- | ----------- | ------ | -------- |
| ORCH-01 | 22-01-PLAN | orchestration-loop v4 worker-claim model | ✓ SATISFIED | spec.md exists, strict-valid, grounded in _async_run + run_local_worker. |
| ORCH-02 | (Phase 21)  | task-model-fsm status machine | ✓ SATISFIED | Authored Phase 21 (72c7e7d), strict-valid. Out of Phase 22 scope; confirmed present. |
| ORCH-03 | 22-02-PLAN | plan-json-contract task fields + envelope | ✓ SATISFIED | spec.md exists, strict-valid, fields match task_manager.py, validate-all-tasks. |
| ORCH-04 | 22-04-PLAN | batch-planning key_files grouping | ✓ SATISFIED | spec.md exists, strict-valid, grounded in orchestrator.py helpers. |
| ORCH-05 | 22-03-PLAN | agent-dispatch runner selection | ✓ SATISFIED | spec.md exists, strict-valid, tmux/subprocess + no-op-flag negation. |
| ORCH-06 | 22-04-PLAN | worktree-isolation workspace+worktree | ✓ SATISFIED | spec.md exists, strict-valid, RepoTargetWorkspaceResolver + WorktreeManager. |
| ORCH-07 | 22-02-PLAN | result-collection AgentResult + marker | ✓ SATISFIED | spec.md exists, strict-valid, exact COMPLETION_MARKER. |

All 7 declared requirement IDs accounted for. No orphaned requirements (REQUIREMENTS.md maps ORCH-01..07 to Phase 22, all addressed).

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| ---- | ---- | ------- | -------- | ------ |
| (6 spec files) | — | TODO/FIXME/TBD/placeholder/stub | — | NONE — clean scan |
| .planning/REQUIREMENTS.md | 37 | ORCH-02 checkbox `[ ]` (task-model-fsm done in Phase 21) | ℹ️ Info | Stale Phase-21 bookkeeping; spec exists + validates. Not a Phase-22 deliverable. |
| .planning/REQUIREMENTS.md | 131 | Traceability table "ORCH-01..07 \| Phase 22 \| Pending" not flipped to Done | ℹ️ Info | Documentation inconsistency in .planning/ only; all spec deliverables exist + validate. |

### Human Verification Required

None. All claims are verifiable programmatically (file existence, strict validation, code-grounding grep, git diff). No visual/runtime/external-service behavior involved (documentation-only phase).

### Gaps Summary

No gaps. The phase goal is fully achieved:

- All 6 in-scope specs (ORCH-01,03,04,05,06,07) exist, are non-empty, normative (SHALL/MUST + ≥1 Scenario each), and pass `openspec validate <slug> --strict` with exit 0.
- ORCH-02 (task-model-fsm) confirmed present from Phase 21 and strict-valid.
- Every CRITICAL groundedness dimension verified against the REAL v4.7.0 code: worker-claim loop (not v3 run_plan), validate-all-tasks (not first-3), no WHILLY_WORKTREE gating, RepoTargetWorkspaceResolver model, batch helpers scoped as pure (not v3 first-batch dispatch), and the exact `<promise>COMPLETE</promise>` marker.
- No stale v3 behavior is pinned as current. No delta headers. Documentation-only constraint upheld (zero whilly/ Python changes).
- Coverage matrix (openspec/COVERAGE-MATRIX.md) maps every named module to its ORCH capability (SC#5 satisfied).

Two non-blocking .planning/ bookkeeping nits noted as WARNINGS for human awareness (ORCH-02 checkbox and the Traceability table row), neither of which affects the actual phase deliverables.

---

_Verified: 2026-06-16T00:40:00Z_
_Verifier: Claude (gsd-verifier)_
