---
phase: 23-prd-pipeline-decision
plan: 02
subsystem: openspec-capability-specs
tags: [openspec, prd-wizard, decomposition, reverse-spec, documentation]
requires:
  - openspec/AUTHORING.md
  - openspec/specs/task-model-fsm/spec.md
provides:
  - openspec/specs/prd-wizard/spec.md (PRD-02)
  - openspec/specs/decomposition/spec.md (PRD-04)
affects: []
tech-stack:
  added: []
  patterns: [reverse-spec-from-source, normative-SHALL-MUST, strict-openspec-validation]
key-files:
  created:
    - openspec/specs/prd-wizard/spec.md
    - openspec/specs/decomposition/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
    - .planning/ROADMAP.md
decisions:
  - "decomposition spec includes a NORMATIVE requirement stating the decomposer is legacy/unwired in the v4 worker-claim run path — grep confirmed it is never imported/called by cli/run.py, cli/worker.py, worker/main.py."
metrics:
  duration: ~10m
  completed: 2026-06-16
---

# Phase 23 Plan 02: PRD-Wizard & Decomposition Specs Summary

Authored two strict-valid OpenSpec capability specs reverse-spec'd from real v4.7.0 code:
`prd-wizard` (interactive PRD authoring via the Claude CLI) and `decomposition` (mid-run
task splitting), the latter normatively recording its legacy/unwired v4 status.

## What Was Built

- **openspec/specs/prd-wizard/spec.md (PRD-02)** — 5 requirements covering `PrdWizard.start`
  (background daemon thread + `is_running` double-start guard), interactive tmux authoring
  with non-interactive `claude --print` fallback, `WizardResult`
  (success/prd_path/tasks_path/task_count/error/idea/elapsed_sec) + always-fired `on_complete`
  callback, `run_prd_wizard` foreground CLI with `_build_system_prompt`, and
  `merge_tasks_into_plan` (fresh `TASK-NNN` re-IDing, force `pending`, drop unresolvable
  dependencies, `_origin` tag). Grounded in `whilly/prd_wizard.py` + `whilly/prd_launcher.py`.

- **openspec/specs/decomposition/spec.md (PRD-04)** — 5 requirements covering the pending-only
  `needs_decompose` heuristic (≥6 acceptance_criteria OR 2+ `" и "` OR 1+ `" + "`),
  `build_decompose_prompt` (2-5 subtasks, TASK-XXXa/b inheriting phase/category/priority,
  protects done/in_progress/failed, DECOMPOSED/NO_DECOMPOSE marker), `run_decompose`
  (`_tasks_hash` SHA256 cache + NO_DECOMPOSE short-circuit + count delta), `DECOMPOSE_EVERY`
  default 5, and a dedicated requirement asserting decomposition is legacy and NOT invoked by
  the v4 worker-claim run path. Grounded in `whilly/decomposer.py` + `whilly/config.py:76`.

## How Verified

- `openspec validate prd-wizard --strict` → "Specification 'prd-wizard' is valid" (exit 0).
- `openspec validate decomposition --strict` → "Specification 'decomposition' is valid" (exit 0).
- Wiring status verified by grep: `decomposer` / `needs_decompose` / `run_decompose` are never
  imported or called in `whilly/cli/run.py`, `whilly/cli/worker.py`, `whilly/worker/main.py`,
  or anywhere else in `whilly/` (the only hit is a historical docstring mention in
  `agent_runner.py`). The spec states this truthfully rather than asserting live cadence behavior.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Spec prd-wizard (PRD-02) | a2f512d | openspec/specs/prd-wizard/spec.md |
| 2 | Spec decomposition + wiring status (PRD-04) | f14ac78 | openspec/specs/decomposition/spec.md |

## Deviations from Plan

None — plan executed exactly as written. Documentation-only; zero `whilly/` Python changes.

## Known Stubs

None. Both specs are complete normative documents.

## Self-Check: PASSED

- FOUND: openspec/specs/prd-wizard/spec.md
- FOUND: openspec/specs/decomposition/spec.md
- FOUND commit: a2f512d
- FOUND commit: f14ac78
