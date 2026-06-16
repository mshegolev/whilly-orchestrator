---
phase: 28-forward-process-coverage-validation
type: context
requirements: [FWD-01, FWD-02, COV-01, VAL-01, VAL-02]
source: orchestrator-authored (autonomous run — milestone closeout)
---

# Phase 28 Context — Forward Process, Coverage & Validation (milestone closeout)

## Goal

Close milestone v1.3: document the forward delta-only process, make CLAUDE.md/AGENTS.md
require spec deltas, audit the coverage matrix at 100%, and confirm all 32 specs are
strict-valid and normatively accurate. **This phase is docs + audit + review — NOT
reverse-speccing new capabilities.** Still documentation-only: NO `whilly/` Python changes.

## Current state (verified at phase start)

- **32/32** capability specs exist (`openspec/specs/*/spec.md`) and pass
  `openspec validate --all --strict` (32 passed, 0 failed) → VAL-01 essentially met; just document/assert it.
- Coverage matrix: **275 rows = live `find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`** (no drift; 0 UNMAPPED, all 32 capabilities covered) → COV-01 re-audit.
- `CLAUDE.md` already carries openspec pointers (added when it was rewritten v3→v4) but the wording is "should be reflected" — FWD-02 wants a stronger "require an opsx spec delta" rule.
- `AGENTS.md` exists (root) — FWD-02 must update it too.
- Each cluster phase (22–27) already produced a `*-VERIFICATION.md` with adversarial code-grounding → VAL-02 can consolidate these + a final sweep rather than re-reviewing from scratch.

## 5 requirements to satisfy

| Req | What | How |
|-----|------|-----|
| FWD-01 | Forward delta-only workflow documented | Write a forward-process doc (recommend `openspec/FORWARD-PROCESS.md`, referenced from `openspec/project.md`/`AUTHORING.md`): behavior changes require an `opsx` proposal (propose → apply → archive) that updates the relevant `openspec/specs/<slug>/spec.md`; OpenSpec = living WHAT, GSD = HOW. |
| FWD-02 | CLAUDE.md + AGENTS.md require spec deltas | Strengthen `CLAUDE.md` "When editing" to **require** an `opsx` spec delta for any behavior change and point at `openspec/specs/`. Add the same rule to `AGENTS.md`. Keep both accurate to v4. |
| COV-01 | Coverage matrix verified 100% | Re-audit `openspec/COVERAGE-MATRIX.md`: body-row count == live `find` count (275 today), 0 UNMAPPED, 0 double-map, every slug ∈ the final 32 in TAXONOMY, every one of the 32 capabilities has ≥1 module. Record the audit result (e.g., a COV-01 note/section). If live count drifted, reconcile. |
| VAL-01 | `openspec validate --strict` passes for all specs | Run `openspec validate --all --strict`; assert 32 passed / 0 failed; record it. |
| VAL-02 | Every spec peer/UAT-reviewed for normative accuracy | Confirm no descriptive-only specs: every spec has SHALL/MUST requirement bodies + ≥1 `#### Scenario:`, and is grounded in real v4 code. Leverage the 7 phase `*-VERIFICATION.md` reports (they already adversarially grounded each cluster) + a final spot-sweep for any spec that pins removed/legacy behavior as live. Produce a consolidated VAL-02 review note. |

## Grounding discipline

The forward-process docs must reflect the REAL workflow (opsx commands exist: propose/apply/archive/sync per the `/opsx:*` skills and OpenSpec 1.4.1). Don't invent commands. CLAUDE.md/AGENTS.md edits must stay v4-accurate (the v3→v4 drift already burned us once).

## Out of scope

Any `whilly/` Python changes. New capability specs (all 32 done). Behavior changes (those are future opsx proposals).

## Success criteria (ROADMAP Phase 28 / milestone closeout)

1. Coverage matrix audited at 100% (275/275, zero gaps) — COV-01.
2. `openspec validate --strict` passes across all 32 specs — VAL-01.
3. CLAUDE.md + AGENTS.md require an opsx spec delta for behavior changes and point at `openspec/specs/` — FWD-02.
4. Forward delta-only process documented — FWD-01.
5. Every capability spec reviewed for normative accuracy (no descriptive-only) — VAL-02.

## Spec/doc format

FWD docs are plain Markdown (not capability specs — no openspec validate gate on them). Capability specs are frozen; only touch them if VAL-02 finds an inaccuracy (then note it). Documentation-only.
