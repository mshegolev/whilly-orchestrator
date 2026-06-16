---
phase: 27-safety-quality-cluster
type: context
requirements: [SAFE-01, SAFE-02, SAFE-03, SAFE-04]
source: orchestrator-authored (autonomous run)
---

# Phase 27 Context — Safety & Quality Cluster

## Goal

Capture the 4 safety/quality contracts as **normative, machine-checkable** OpenSpec specs,
each **reverse-spec'd from the real v4.7.0 code**, passing `openspec validate <slug> --strict`.
This is the last spec-writing cluster (Phase 28 = coverage audit + validate + forward process).

## Grounding discipline

READ the modules; spec observed behavior; state wiring/legacy status truthfully. Several
REQUIREMENTS.md wordings carry v3 lore — supersede with real v4 behavior. The plan-checker and
verifier adversarially confirm every requirement against source.

## 4 specs to write (one per slug)

| Req | Slug | Reverse-spec from | Cautions |
|-----|------|-------------------|----------|
| SAFE-01 | `budget-resource-guards` | `whilly/resource_monitor.py`, `whilly/cli/smoke.py` | **GROUNDING CAUTION:** REQUIREMENTS says "budget thresholds 80% warn / 100% kill→exit 2". That's v3 lore (kill-all-tmux-sessions → exit 2). VERIFY how budget/resources are actually monitored in v4 (`resource_monitor.py` — CPU/memory thresholds? what action?) and whether budget enforcement is wired into the worker-claim path. Spec the REAL behavior + state wiring; reference `cli-surface` exit codes rather than asserting the v3 budget→exit-2 path unless the code proves it. |
| SAFE-02 | `recovery-self-healing` | `whilly/recovery.py` (recover_task_statuses, validate_task_consistency — note these take legacy `task_manager` + `workspace_dir`/progress files), `whilly/self_healing.py` (SelfHealingHandler, global_exception_handler, enable_self_healing) | **GROUNDING CAUTION:** recovery.py reconstructs status from progress/log files using the legacy in-memory TaskManager — likely legacy/unwired in the v4 Postgres worker-claim path (v4 uses release_stale_tasks visibility-timeout sweep + optimistic locking instead). VERIFY wiring of both modules; spec what they do + state legacy/live truthfully. |
| SAFE-03 | `quality-compliance-audit` | `whilly/quality/*` (base, _runner, multi, python/go/node/rust language runners), `whilly/compliance/*`, `whilly/audit/*` (jsonl_sink), `whilly/qa_release/*` (autotest_writer, collector, models, test_plan), `whilly/cli/{compliance,qa_release}.py` | Quality runners (lint/test per language), compliance checks, audit-event JSONL sink, QA-release. Subsystem altitude. |
| SAFE-04 | `verification-gates` | `whilly/verifier.py` (VerifyResult, _get_changed_files), `whilly/pipeline/*` (verification, human_review, human_review_decisions, sinks, events), `whilly/ci/verification.py` + ci/{events,models} | Verifier gate + human-review gate behavior. Reference result-collection/orchestration-loop where they feed. |

(Authoritative module→capability assignments: `openspec/COVERAGE-MATRIX.md`.)

## Boundaries

- `budget-resource-guards` vs `cli-surface`: reference the real exit-code contract from cli-surface; don't re-assert v3 budget→exit-2 lore unless grounded.
- `recovery-self-healing` references `task-model-fsm` (terminal states) and `state-persistence` (visibility-timeout sweep) — spec what recovery/self_healing modules actually do, marking legacy paths.
- `verification-gates` references `result-collection` (AgentResult) and `orchestration-loop` (where gates run); don't duplicate.
- Reference earlier capabilities rather than re-speccing.

## Spec format

Mirror `openspec/specs/task-model-fsm/spec.md`; follow `openspec/AUTHORING.md`. `## Purpose`
(≥50 chars) → `## Requirements` with `### Requirement:` (FIRST body line SHALL/MUST, ≤500
chars) each ≥1 `#### Scenario:` (WHEN/THEN).

## Out of scope

Phase 28 (coverage audit/validate/forward-process); any `whilly/` Python changes. **Documentation only.**

## Success criteria (ROADMAP)

1. 4 capabilities specced.
2. Real budget/resource-guard behavior + recovery/self-healing + quality/compliance/audit + verifier/human-review gates captured.
3. Each spec ≥1 scenario; all 4 pass `openspec validate --strict`.
4. Covered modules accounted for in the coverage matrix.
