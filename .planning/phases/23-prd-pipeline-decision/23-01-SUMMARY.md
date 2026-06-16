---
phase: 23-prd-pipeline-decision
plan: 01
subsystem: docs
tags: [openspec, prd-generation, task-generation, classifier, reverse-spec]

requires:
  - phase: 21-spec-baseline-taxonomy
    provides: task-model-fsm exemplar spec + AUTHORING.md format rules
  - phase: 22-orchestration-cluster
    provides: plan-json-contract spec (task-field schema referenced, not duplicated)
provides:
  - openspec/specs/prd-generation/spec.md (normative PRD-01 capability spec)
  - openspec/specs/task-generation/spec.md (normative PRD-03 capability spec)
affects: [23-prd-pipeline-decision, 28-forward-process-coverage-validation]

tech-stack:
  added: []
  patterns:
    - "Reverse-spec from real v4.7.0 modules, not prose narrative"
    - "Split a co-hosted module (prd_generator.py) into two disjoint capabilities by behavior"
    - "Reference plan-json-contract for task fields; do not re-spec the field schema"

key-files:
  created:
    - openspec/specs/prd-generation/spec.md
    - openspec/specs/task-generation/spec.md
  modified:
    - .planning/STATE.md
    - .planning/REQUIREMENTS.md

key-decisions:
  - "prd-generation = PRD-document synthesis (generate_prd + _call_claude) + classifier subsystem (router/heuristic/llm/matcher/epic_inferrer); tasks.json contract scoped OUT"
  - "task-generation = PRD->tasks.json contract (run_init_command + generate_tasks/generate_tasks_dict over shared _build_tasks_payload); task-field schema referenced from plan-json-contract, not duplicated"
  - "Grounded the Claude CLI invocation contract (CLAUDE_BIN, -p mode, --disallowedTools, WHILLY_CLAUDE_TIMEOUT, empty-string-on-failure) in the real _call_claude implementation"
  - "Captured the classifier fallback discipline (LLMClassifier -> HeuristicClassifier, never raises) and router REJECT/LINK_AS_CHILD/CREATE_ORPHAN actions from the actual code"

patterns-established:
  - "Each requirement body's first line carries SHALL/MUST; every requirement has >=1 #### Scenario with WHEN/THEN"

requirements-completed: [PRD-01, PRD-03]

duration: 11min
completed: 2026-06-16
---

# Phase 23 Plan 01: PRD-Generation & Task-Generation Specs Summary

**Two normative OpenSpec capability specs that split the behavior co-hosted in `whilly/prd_generator.py`: `prd-generation` (PRD-document synthesis via `generate_prd`/`_call_claude` plus the `whilly/classifier/*` task/epic classification subsystem) and `task-generation` (the PRD→tasks.json contract via `run_init_command` + `generate_tasks`/`generate_tasks_dict` over the shared `_build_tasks_payload`), each passing `openspec validate <slug> --strict` with zero errors and warnings.**

## Performance

- **Duration:** 11 min
- **Tasks completed:** 2/2
- **Files created:** 2 spec files
- **Files modified:** 2 planning docs

## What Was Built

### Task 1 — prd-generation spec (PRD-01)

`openspec/specs/prd-generation/spec.md`, six requirements reverse-spec'd from v4 code:

1. **PRD document synthesis from a description** — `generate_prd` builds the PRD-authoring prompt, calls Claude via `_call_claude`, writes `PRD-<slug>.md`; empty model response raises `RuntimeError`.
2. **Slug derivation for the PRD filename** — explicit `slug` wins; otherwise auto-derived from leading ~50 chars (alnum/`-`/`_`).
3. **Output normalisation strips markdown fences** — leading ` ```markdown `/` ``` ` and trailing ` ``` ` removed before persisting.
4. **Claude CLI invocation contract for synthesis** — `CLAUDE_BIN` (default `claude`), `-p` mode, `--disallowedTools` (Write/Edit/MultiEdit/NotebookEdit/Bash), `WHILLY_CLAUDE_TIMEOUT`, empty-string-on-failure (missing binary / non-zero / timeout).
5. **Idea classification feeds PRD structure** — `LLMClassifier` primary, `HeuristicClassifier` fallback on any LLM failure (never raises); router `REJECT` on out-of-scope / below-length flags.
6. **Parent routing and orphan handling** — `LINK_AS_CHILD` when top match ≥ `match_threshold`, else `CREATE_ORPHAN`.

Scope OUT: the tasks.json contract (deferred to task-generation).

### Task 2 — task-generation spec (PRD-03)

`openspec/specs/task-generation/spec.md`, five requirements reverse-spec'd from v4 code:

1. **PRD-to-tasks payload construction** — `_build_tasks_payload` reads the PRD, prompts Claude, returns `{project, tasks[...]}`; missing PRD → `FileNotFoundError`; empty tasks → `RuntimeError`.
2. **Robust JSON parsing with forensics fallback** — strip fences, `json_repair` fallback, dump raw to `raw_dump_path` then raise on unrecoverable failure.
3. **Task field defaults applied at generation** — `status=pending`, empty-list collection fields, sequential `TASK-NNN` ids; field meaning referenced from plan-json-contract.
4. **Dual generation flows over one payload builder** — `generate_tasks` writes `<slug>_tasks.json`; `generate_tasks_dict` stamps `plan_id` and returns in-memory (removes temp forensics file on success).
5. **Init pipeline slugifies, refuses overwrite, then imports** — `run_init_command` slugifies/validates, resolves `PRD-<slug>.md`, refuses overwrite without `--force`, rejects empty idea, imports plan + task count when DB URL set.

Scope OUT: the per-task field schema (referenced from plan-json-contract).

## Boundary Honored

The two specs describe disjoint behaviors of the same source module: prd-generation never re-specifies the tasks.json shape; task-generation never re-specifies the PRD-document synthesis path and references plan-json-contract for task fields rather than duplicating them.

## Verification

- `openspec validate prd-generation --strict` → "Specification 'prd-generation' is valid", exit 0 (0 errors, 0 warnings).
- `openspec validate task-generation --strict` → "Specification 'task-generation' is valid", exit 0 (0 errors, 0 warnings).
- No `whilly/` Python files modified (documentation-only phase).

## Deviations from Plan

None - plan executed exactly as written. Both tasks (Task 1 prd-generation, Task 2 task-generation) completed in order, each committed individually, both specs strict-valid on first validation run.

## Known Stubs

None. Both specs are complete, normative, and grounded in observed v4.7.0 behavior.

## Self-Check: PASSED

- FOUND: openspec/specs/prd-generation/spec.md
- FOUND: openspec/specs/task-generation/spec.md
- FOUND commit 1eb37f5 (prd-generation spec)
- FOUND commit 5ba4b6a (task-generation spec)
