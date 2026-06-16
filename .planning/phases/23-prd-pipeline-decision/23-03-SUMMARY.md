---
phase: 23-prd-pipeline-decision
plan: 03
subsystem: openspec-capability-specs
tags: [decision-gate, triz, reverse-spec, documentation]
requires: [task-model-fsm]
provides: [decision-gate]
affects: []
tech-stack:
  added: []
  patterns: [reverse-spec-from-code, openspec-strict-validation]
key-files:
  created:
    - openspec/specs/decision-gate/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Split the spec by surface: per-task evaluate (decision_gate.py) vs deterministic plan-level analyze_plan_triz (core/triz.py) — kept the LLM-backed legacy triz_analyzer.analyze_plan_triz out of the normative pins."
  - "Pinned per-task analyze_contradiction as fail-open (None on all soft-fail modes, hard 25s timeout) and the plan-level analyze_plan_triz as deterministic (no LLM/subprocess/network)."
  - "Referenced task-model-fsm for the SKIPPED terminal outcome instead of re-specifying the FSM."
metrics:
  duration: ~6 min
  completed: 2026-06-16
---

# Phase 23 Plan 03: Decision Gate Capability Spec Summary

One-liner: Authored the normative `decision-gate` OpenSpec capability spec, reverse-spec'd from v4.7.0 code — per-task refuse/accept gate (`evaluate`, `parse_decision`, short-description auto-refuse, fail-open) plus the deterministic plan-level TRIZ preflight (`analyze_plan_triz` → `PlanTrizReport`) — passing `openspec validate decision-gate --strict` with zero errors/warnings.

## What Was Built

`openspec/specs/decision-gate/spec.md` (1 file, 8 requirements, all strict-valid):

1. **Short-description auto-refuse** — `evaluate` returns REFUSE with `cost_usd=0.0` and no runner call when trimmed `description` < `MIN_DESCRIPTION_LEN` (20).
2. **Fail-open on runner exception or non-zero exit** — both paths yield PROCEED; non-zero-exit path carries the runner's `cost_usd`/`raw_text`.
3. **Tolerant `parse_decision`** — bare JSON / embedded JSON blob / bare keyword, defaulting to PROCEED on parse failure.
4. **Decision payload shape** — `decision`/`reason`/`cost_usd`/`raw_text`; `decision` ∈ {PROCEED, REFUSE}.
5. **GitHub label flip** — `label_flip_for_gh_task` flips labels only on a REFUSE for a `GH-` task with a parseable `prd_requirement` issue URL; returns False otherwise.
6. **Deterministic plan-level TRIZ preflight** — `core.triz.analyze_plan_triz` inspects an imported `Plan` with no LLM/subprocess/network/Postgres, returning a `PlanTrizReport` (plan_id, task_count, verdict, ideality_score, findings, mergeable_groups, removable_tasks, summary); cyclic dependencies → critical finding → `reject` verdict; clean plan → `approve`.
7. **Per-task TRIZ is fail-open** — `analyze_contradiction` returns `TrizFinding` on a positive verdict, `None` on no-contradiction and every soft-fail mode (claude absent, timeout, malformed JSON, non-zero exit), never re-raising; hard 25s `TIMEOUT_SECONDS`.
8. **SKIPPED terminal outcome deferred to `task-model-fsm`** — decision-gate does not re-spec the FSM.

## Grounding Notes

- The per-task `evaluate` path is in `whilly/decision_gate.py`; the plan-level deterministic analysis is in `whilly/core/triz.py::analyze_plan_triz`. The legacy LLM-backed `whilly/triz_analyzer.py::analyze_plan_triz` was deliberately NOT pinned — only the deterministic v4 plan-level function is normative, per the plan's critical-grounding instruction.
- `analyze_plan_triz` aggregates findings from `_decision_gate_findings`, `_dependency_findings` (cycles via `detect_cycles` → critical; missing-dep ids → high), `_duplicate_description_groups`, `_shared_file_groups`, and `_over_engineering_findings`. Verdict mapping: any critical → reject; any high/medium → revise; else approve.

## Deviations from Plan

None — plan executed exactly as written. Single `type="auto"` task; no checkpoints, no auth gates.

## Verification

- `openspec validate decision-gate --strict` → "Specification 'decision-gate' is valid", exit 0.
- No `whilly/` Python files modified (documentation-only).

## Self-Check: PASSED

- FOUND: openspec/specs/decision-gate/spec.md
- FOUND commit c212044 (spec)
