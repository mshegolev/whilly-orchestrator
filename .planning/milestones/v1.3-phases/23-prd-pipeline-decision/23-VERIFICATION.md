---
phase: 23-prd-pipeline-decision
verified: 2026-06-16T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: null
  note: initial verification (no prior VERIFICATION.md)
---

# Phase 23: PRD Pipeline & Decision Verification Report

**Phase Goal:** The PRD generation/wizard pipeline, task generation, decomposition, and the
Decision Gate are captured as normative OpenSpec capability specs reverse-spec'd from the REAL
v4.7.0 code (PRD-01..05).
**Verified:** 2026-06-16
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | All 5 spec.md exist and pass `openspec validate <slug> --strict` (exit 0, "is valid") | ✓ VERIFIED | Ran all 5: each printed "Specification '<slug>' is valid", exit=0. No errors/warnings. |
| 2 | prd-generation grounded in real generate_prd / classifier / _call_claude | ✓ VERIFIED | `prd_generator.py:150 generate_prd`, `:371 _call_claude` (CLAUDE_BIN, WHILLY_CLAUDE_TIMEOUT, --disallowedTools, returns "" on FileNotFoundError L416); RuntimeError on empty L198. Classifier Router REJECT/LINK_AS_CHILD/CREATE_ORPHAN/match_threshold confirmed in `classifier/router.py`. |
| 3 | task-generation grounded in run_init_command + generate_tasks_dict + _build_tasks_payload; references plan-json-contract, does NOT duplicate task field schema | ✓ VERIFIED | `cli/init.py:257 PRD-<slug>.md`, `:260 overwrite refuse w/o --force` → EXIT_USER_ERROR; `prd_generator.py:213 _build_tasks_payload` (FileNotFoundError L245, json_repair L269, raw_dump L274, "No tasks generated" L280, TASK-NNN L289); `generate_tasks_dict:323` stamps plan_id. Spec explicitly scopes field schema OUT, references plan-json-contract. |
| 4 | prd-wizard grounded in PrdWizard/WizardResult/merge_tasks_into_plan + run_prd_wizard | ✓ VERIFIED | `prd_wizard.py:105 PrdWizard`, `:93 WizardResult` (elapsed_sec L102), `:131 start` daemon thread guarded by `is_running` L128, `:193 _run_claude_interactive` (tmux `whilly-prd-wizard`), `:274 _run_claude_noninteractive`, `:326 merge_tasks_into_plan` (_origin L365). `prd_launcher.py:116 run_prd_wizard`, `:90 _build_system_prompt`, --append-system-prompt L163, return 1/0. |
| 5 | decomposition grounded in needs_decompose/run_decompose, DECOMPOSE_EVERY=5; TRUTHFULLY states legacy/UNWIRED in v4 worker-claim path | ✓ VERIFIED | `decomposer.py:14 needs_decompose` (>=6 acceptance_criteria L25, 2+ `" и "` L27, 1+ `" + "` L29), `:67 _tasks_hash` sha256, `:73 run_decompose(tm: TaskManager, agent_model, use_tmux, log_dir)`, NO_DECOMPOSE cache L78. `config.py:76 DECOMPOSE_EVERY=5`. **grep of cli/run.py, cli/worker.py, worker/main.py = ZERO call sites; no external importers anywhere.** Spec requirement "Legacy unwired status" + REQUIREMENTS.md note both state this truthfully. |
| 6 | decision-gate auto-refuse <20 chars no-LLM + fail-open; plan-TRIZ pinned to DETERMINISTIC core/triz.analyze_plan_triz (NOT legacy triz_analyzer) | ✓ VERIFIED | `decision_gate.py:32 MIN_DESCRIPTION_LEN=20`, `:174 auto-refuse cost_usd=0.0 no runner`, `:184 fail-open exception→PROCEED`, `:188 non-zero exit→PROCEED`, parse_decision fail-open default PROCEED L142; Decision fields decision/reason/cost_usd/raw_text. Spec Purpose + "Deterministic plan-level TRIZ preflight" requirement pin `core/triz.py:241 analyze_plan_triz` — docstring + body confirm pure detectors, NO subprocess/network/Postgres. The legacy LLM `triz_analyzer.py:234 analyze_plan_triz` is NOT pinned for the plan-preflight contract (correct). |
| 7 | No delta headers (## ADDED/MODIFIED) in any main spec | ✓ VERIFIED | grep for `## ADDED/MODIFIED/REMOVED/RENAMED` across all 5 specs = NONE. |
| 8 | Documentation-only: git diff touched only openspec/specs/ and .planning/ | ✓ VERIFIED | `git diff 1eb37f5^..d904c2b --name-only`: only .planning/* and openspec/specs/* changed. grep for `^whilly/.*\.py$` = none. |
| 9 | PRD-01..05 in REQUIREMENTS.md all marked done, 1:1 spec mapping | ✓ VERIFIED | REQUIREMENTS.md L57-66: all `[x]`. PRD-01→prd-generation, PRD-02→prd-wizard, PRD-03→task-generation, PRD-04→decomposition, PRD-05→decision-gate. Coverage matrix L145-297 maps every module. |

**Score:** 9/9 truths verified (5/5 PRD requirements satisfied)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `openspec/specs/prd-generation/spec.md` | Normative prd-generation spec | ✓ VERIFIED | 6 requirements, 11 scenarios, Purpose 755 chars, strict-valid |
| `openspec/specs/task-generation/spec.md` | Normative task-generation spec | ✓ VERIFIED | 5 requirements, 12 scenarios, Purpose 795 chars, strict-valid |
| `openspec/specs/prd-wizard/spec.md` | Normative prd-wizard spec | ✓ VERIFIED | 5 requirements, 12 scenarios, Purpose 688 chars, strict-valid |
| `openspec/specs/decomposition/spec.md` | Normative decomposition spec | ✓ VERIFIED | 5 requirements, 10 scenarios, Purpose 722 chars, strict-valid, legacy status stated |
| `openspec/specs/decision-gate/spec.md` | Normative decision-gate spec (refuse/accept + TRIZ) | ✓ VERIFIED | 8 requirements, 17 scenarios, Purpose 814 chars, strict-valid |

### Key Link Verification

| From | To | Via | Status |
|------|----|-----|--------|
| prd-generation/spec.md | whilly/prd_generator.py | reverse-spec of generate_prd | ✓ WIRED |
| task-generation/spec.md | whilly/cli/init.py + prd_generator.py | reverse-spec of run_init_command→generate_tasks_dict | ✓ WIRED |
| prd-wizard/spec.md | whilly/prd_wizard.py + prd_launcher.py | reverse-spec of PrdWizard/merge_tasks_into_plan/run_prd_wizard | ✓ WIRED |
| decomposition/spec.md | whilly/decomposer.py | reverse-spec + truthful wiring-status statement | ✓ WIRED (legacy status correct) |
| decision-gate/spec.md | whilly/decision_gate.py + core/triz.py | reverse-spec of evaluate / DETERMINISTIC analyze_plan_triz | ✓ WIRED (correct triz pinned) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All 5 specs strict-valid | `openspec validate <slug> --strict` x5 | all "is valid", exit 0 | ✓ PASS |
| No delta headers | grep `## ADDED/MODIFIED` | none | ✓ PASS |
| Documentation-only | git diff name-only, grep whilly/*.py | zero py changes | ✓ PASS |
| Decomposer unwired | grep needs_decompose/run_decompose in run.py/worker.py/main.py | zero call sites | ✓ PASS |
| Deterministic triz pinned | inspect core/triz.analyze_plan_triz body | no subprocess/net/db | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| PRD-01 | 23-01 | prd-generation: non-interactive PRD synthesis | ✓ SATISFIED | spec strict-valid, grounded in generate_prd/classifier |
| PRD-02 | 23-02 | prd-wizard: interactive PRD authoring via Claude CLI | ✓ SATISFIED | spec strict-valid, grounded in PrdWizard/run_prd_wizard |
| PRD-03 | 23-01 | task-generation: PRD→tasks.json contract | ✓ SATISFIED | spec strict-valid, grounded in init.py/generate_tasks_dict, references plan-json-contract |
| PRD-04 | 23-02 | decomposition: mid-run task splitting | ✓ SATISFIED | spec strict-valid, grounded in decomposer.py, truthfully legacy/unwired |
| PRD-05 | 23-03 | decision-gate: refuse/accept + TRIZ | ✓ SATISFIED | spec strict-valid, grounded in decision_gate.py + deterministic core/triz.py |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| decomposition/spec.md | 40 | `TASK-XXXa`/`TASK-XXXb` | ℹ️ Info (false positive) | Literal subtask ID format from code, not a debt marker |
| decomposition/spec.md | 98 | "not aspirational wiring" | ℹ️ Info (false positive) | Descriptive prose asserting honesty, not a debt marker |

No genuine debt markers (TBD/FIXME/XXX/TODO/PLACEHOLDER) in any spec.

### Human Verification Required

None. All verification dimensions are programmatically checkable (spec validation, code grounding via grep/read, git diff, requirements cross-reference) and passed.

### Gaps Summary

No gaps. All five capability specs exist, pass `openspec validate --strict` with zero
errors/warnings, contain no delta headers, and are faithfully reverse-spec'd from the REAL
v4.7.0 code:

- **prd-generation** and **task-generation** correctly split the co-hosted prd_generator.py
  behavior (PRD-document synthesis vs PRD→tasks contract) and reference plan-json-contract
  without duplicating the field schema.
- **prd-wizard** is grounded in PrdWizard/WizardResult/merge_tasks_into_plan and run_prd_wizard.
- **decomposition** truthfully and normatively states its legacy/UNWIRED status — verified by
  grep showing zero call sites of needs_decompose/run_decompose in cli/run.py, cli/worker.py,
  worker/main.py and no external importers. This is the highest-risk groundedness check and it
  passes: the spec does NOT assert aspirational cadence-driven wiring.
- **decision-gate** correctly pins the DETERMINISTIC `core/triz.analyze_plan_triz` (no LLM,
  subprocess, network, or Postgres — confirmed in code) for the plan-preflight contract, NOT
  the legacy LLM-backed `triz_analyzer.analyze_plan_triz`. Per-task auto-refuse (<20 chars,
  no-LLM, cost_usd=0.0) and fail-open posture are exactly as implemented.

Phase is documentation-only (zero whilly/ Python changes). All PRD-01..05 marked done in
REQUIREMENTS.md with clean 1:1 spec mapping and full coverage-matrix accounting.

---

_Verified: 2026-06-16_
_Verifier: Claude (gsd-verifier)_
