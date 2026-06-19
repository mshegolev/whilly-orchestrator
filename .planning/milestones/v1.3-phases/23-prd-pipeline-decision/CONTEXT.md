---
phase: 23-prd-pipeline-decision
type: context
requirements: [PRD-01, PRD-02, PRD-03, PRD-04, PRD-05]
source: orchestrator-authored (autonomous run; CLAUDE.md is now v4-accurate)
---

# Phase 23 Context — PRD Pipeline & Decision

## Goal

Capture the 5 PRD-pipeline + Decision-Gate contracts as **normative, machine-checkable**
OpenSpec specs under `openspec/specs/<slug>/spec.md`, each **reverse-spec'd from the real
v4.7.0 code** (no descriptive-only/aspirational specs) and passing
`openspec validate <slug> --strict` (zero errors/warnings).

## Grounding discipline (same as Phase 22)

CLAUDE.md is now v4-accurate, but the rule stands: **READ the modules, spec the observed
behavior.** If a module is legacy/unwired in the v4 worker-claim run path (e.g. it consumes
the legacy in-memory `TaskManager` rather than the Postgres `TaskRepository`), say so
truthfully — describe the function contract AND its wiring status. Do not assert a behavior
the code does not have. The plan-checker and verifier will adversarially check every
requirement against the source.

## 5 specs to write (one per slug)

| Req | Slug | Reverse-spec from (verified v4 symbols) |
|-----|------|------------------------------------------|
| PRD-01 | `prd-generation` | `whilly/prd_generator.py` (`generate_prd`, `_build_tasks_payload`, `_call_claude`); `whilly/classifier/*` (task/epic classification: `router`, `heuristic`, `llm`, `matcher`, `epic_inferrer`, `rebuilder`); `whilly/core/prompts.py` |
| PRD-02 | `prd-wizard` | `whilly/prd_wizard.py` (`PrdWizard`, `WizardResult`, `merge_tasks_into_plan`); `whilly/prd_launcher.py` (`run_prd_wizard`, `_build_system_prompt`) — interactive Claude CLI authoring |
| PRD-03 | `task-generation` | `whilly/cli/init.py` (`run_init_command`, the `--init` PRD→tasks pipeline); the actual generator `whilly/prd_generator.py` (`generate_tasks`, `generate_tasks_dict`, `_build_tasks_payload`) which init imports |
| PRD-04 | `decomposition` | `whilly/decomposer.py` (`needs_decompose`, `build_decompose_prompt`, `run_decompose`, `_tasks_hash`); `WHILLY_DECOMPOSE_EVERY` default 5 (config.py:76). NOTE: decomposer consumes the legacy in-memory `TaskManager` + `use_tmux` — verify and state its wiring status in the v4 path. |
| PRD-05 | `decision-gate` | `whilly/decision_gate.py` (`Decision`, `build_prompt`, `parse_decision`, `evaluate`, `label_flip_for_gh_task`); `whilly/triz_analyzer.py`; `whilly/core/triz.py` (`analyze_plan_triz`, `PlanTrizReport`, `TrizFinding`, `_decision_gate_findings`, etc.) |

(Authoritative module→capability assignments: `openspec/COVERAGE-MATRIX.md`.)

## Boundaries (don't duplicate)

- `prd-generation` vs `task-generation`: `prd_generator.py` co-hosts `generate_prd` (→ prd-generation)
  and `generate_tasks`/`generate_tasks_dict` (→ task-generation). Split by behavior: prd-generation
  specs PRD-document synthesis; task-generation specs the PRD→tasks.json contract (task shape,
  count, import). Reference, don't re-spec, `plan-json-contract` (Phase 22) for task fields.
- `decision-gate` is the refuse/accept gate (per-task `evaluate` + plan-level TRIZ findings).
  Reference `task-model-fsm` for the SKIPPED outcome; don't re-spec the FSM.
- `decomposition` references `task-model-fsm`/`plan-json-contract`, doesn't duplicate them.

## Spec format

Mirror `openspec/specs/task-model-fsm/spec.md` and follow `openspec/AUTHORING.md`:
`## Purpose` (≥50 chars prose) → `## Requirements` with `### Requirement:` blocks (each body
line contains SHALL/MUST, ≤500 chars) → each requirement ≥1 `#### Scenario:` (`- **WHEN**` /
`- **THEN**`, `- **AND**` optional).

## Out of scope

Phases 24–27 capabilities; any `whilly/` Python changes. **Documentation only.**

## Success criteria (ROADMAP)

1. Each of the 5 capabilities has a normative `spec.md`.
2. `decision-gate` specifies refuse/accept criteria + TRIZ contradiction analysis.
3. Each spec ≥1 testable `#### Scenario:`; all 5 pass `openspec validate --strict`.
4. Every module these capabilities cover is accounted for in the coverage matrix.
