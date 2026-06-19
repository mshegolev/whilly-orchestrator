---
phase: 22-orchestration-cluster
plan: 02
subsystem: openspec-capability-specs
tags: [openspec, orchestration, plan-contract, result-parsing, documentation]
requires:
  - openspec/AUTHORING.md (format authority)
  - openspec/specs/task-model-fsm/spec.md (worked exemplar + legal status reference)
provides:
  - openspec/specs/plan-json-contract/spec.md (ORCH-03)
  - openspec/specs/result-collection/spec.md (ORCH-07)
affects:
  - .planning/REQUIREMENTS.md (ORCH-03, ORCH-07 marked complete)
  - .planning/STATE.md (Current Position advanced)
tech-stack:
  added: []
  patterns: [reverse-spec'd-from-v4-code, normative-testable-openspec]
key-files:
  created:
    - openspec/specs/plan-json-contract/spec.md
    - openspec/specs/result-collection/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "plan-json-contract enumerates the task_manager.py Task dataclass field set (id, phase, category, priority, description, status + defaulted collections) AND the v4 plan_io.py envelope/extensions, keeping legal status values referenced to task-model-fsm rather than re-listed."
  - "Asserted the NEGATION of the stale v3 'first 3 tasks' claim — validate_schema validates EVERY task id."
metrics:
  duration: ~12m
  completed: 2026-06-15
  tasks: 2
  files: 4
---

# Phase 22 Plan 02: Plan JSON Contract + Result Collection Summary

Two normative OpenSpec capability specs reverse-spec'd from the real v4.7.0 code:
`plan-json-contract` (ORCH-03) capturing the plan envelope, required task fields,
v4 extensions, atomic writes, round-trip tolerance and full-array id validation;
and `result-collection` (ORCH-07) capturing AgentResult parsing, usage/cost
accounting, stream-event normalisation, the `<promise>COMPLETE</promise>`
completion signal and the defensive no-raise fallback.

## What Was Built

### Task 1 — plan-json-contract/spec.md (ORCH-03) — commit 01b9e90

Eight requirements grounded in `whilly/adapters/filesystem/plan_io.py`,
`whilly/task_manager.py`, `whilly/cli/__init__.py`, and `whilly/core/models.py`:

- Plan envelope shape (`project` + `tasks[]` required; `PlanParseError` on bad shape).
- Required task fields per the `task_manager.py` `Task` dataclass (no-default `id`,
  `phase`, `category`, `priority`, `description`, `status`; defaulted `dependencies`,
  `key_files`, `acceptance_criteria`, `test_steps`, `prd_requirement`).
- Parser per-task minimum (non-empty `id` + `status`/`priority`/`description`) and the
  `critical|high|medium|low` priority range; status delegated to task-model-fsm.
- Optional v4 extensions: `plan_id` (project fallback), `origin`/PlanOrigin,
  `repo_targets`/RepoTarget, `verification_commands`/VerificationCommand.
- Atomic writes (temp-then-`os.replace`, temp cleanup on failure).
- Round-trip tolerance: unknown keys tolerated on read, dropped on serialize.
- Every task id validated by `validate_schema` — explicitly asserts NO first-3 limit.

### Task 2 — result-collection/spec.md (ORCH-07) — commit e6ac69b

Six requirements grounded in `whilly/adapters/runner/result_parser.py`:

- Immutable `AgentResult(output, usage, exit_code, is_complete)` value object (frozen).
- `AgentUsage` token/cost accounting with zeroed defaults.
- `exit_code` threaded in by the subprocess wrapper (not parsed from stdout), default 0.
- Stream-event-array normalisation → final `{"type": "result", ...}` event.
- Completion signal: `is_complete` true iff `<promise>COMPLETE</promise>` in result text.
- Defensive no-raise fallback for empty/malformed/plaintext/usage-less stdout, still
  scanning raw stdout for the marker.

## Verification

- `openspec validate plan-json-contract --strict` → "is valid" (0 errors / 0 warnings).
- `openspec validate result-collection --strict` → "is valid" (0 errors / 0 warnings).
- Neither file contains delta headers (`## ADDED/MODIFIED/REMOVED/RENAMED`).
- plan-json-contract asserts NO "first 3 tasks" validation (negation present, claim absent).

## Deviations from Plan

None — plan executed exactly as written. The stub `.gitkeep` files in both spec
directories were removed as part of authoring the real `spec.md` files (the
directories were scaffolded empty in plan 22-02's predecessor scaffold).

## Notes on v3→v4 Grounding

Followed the CONTEXT.md architecture note: the v3 CLAUDE.md "validate_schema checks
only the first 3 tasks" claim is FALSE in v4 — `validate_schema` lives in
`whilly/cli/__init__.py` and iterates the entire task array. The spec encodes the
correct v4 behavior. Note there are two `Task` types in v4: the v3-shaped
`task_manager.py` dataclass (whose field set the plan's must_haves require enumerated)
and the frozen `core/models.py` `Task` used by `plan_io.py`; the spec references the
former's field set and the latter's parser/envelope behavior, both observed from code.

## Self-Check: PASSED

- openspec/specs/plan-json-contract/spec.md — FOUND
- openspec/specs/result-collection/spec.md — FOUND
- commit 01b9e90 — FOUND
- commit e6ac69b — FOUND
