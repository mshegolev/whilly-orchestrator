# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. Completed milestone evidence is archived
under `.planning/milestones/`; `.planning/ROADMAP.md` stays small and describes only the active or
next milestone state.

## Milestones

| Milestone | Status | Shipped | Evidence |
|-----------|--------|---------|----------|
| v1.0 | Shipped | 2026-05-08 | `.planning/milestones/v1.0-ROADMAP.md`, `.planning/milestones/v1.0-REQUIREMENTS.md`, `.planning/milestones/v1.0-MILESTONE-AUDIT.md` |
| v1.1 UI parity completion | Shipped | 2026-05-11 | `.planning/milestones/v1.1-ROADMAP.md`, `.planning/milestones/v1.1-REQUIREMENTS.md`, `.planning/milestones/v1.1-MILESTONE-AUDIT.md`, `.planning/milestones/v1.1-RETROSPECTIVE.md` |
| v1.2 Adoption & live-ops | Shipped | 2026-06-12 | `.planning/milestones/v1.2-ROADMAP.md`, `.planning/milestones/v1.2-REQUIREMENTS.md`, `.planning/milestones/v1.2-MILESTONE-AUDIT.md` |
| v1.3 OpenSpec Project Baseline | Shipped | 2026-06-16 | `.planning/milestones/v1.3-ROADMAP.md`, `.planning/milestones/v1.3-REQUIREMENTS.md` |
| v1.4 Spec Drift-Guard CI | Shipped | 2026-06-18 | `.planning/milestones/v1.4-ROADMAP.md`, `.planning/milestones/v1.4-REQUIREMENTS.md`, `.planning/milestones/v1.4-MILESTONE-AUDIT.md`, `.planning/milestones/v1.4-RETROSPECTIVE.md` |
| v1.5 Semantic Drift-Guard | Shipped | 2026-06-19 | `.planning/milestones/v1.5-ROADMAP.md`, `.planning/milestones/v1.5-REQUIREMENTS.md`, `.planning/milestones/v1.5-MILESTONE-AUDIT.md` |

## v1.3 OpenSpec Project Baseline — summary

Captured Whilly's guaranteed behavior as **32 normative, testable OpenSpec capability specs**
(`openspec/specs/<slug>/spec.md`), all passing `openspec validate --strict` (32/0), with a
`module → capability` coverage matrix proving **all 275 `whilly/` modules** are accounted for
(0 unmapped, 0 double-mapped). Forward delta-only process documented
(`openspec/FORWARD-PROCESS.md`); `CLAUDE.md` + `AGENTS.md` now require an `opsx` spec delta for
any behavior change. Phases 21–28, all verified. Spec-capture only — zero `whilly/` behavior
changes. Full phase detail: `.planning/milestones/v1.3-ROADMAP.md`.

## v1.4 Spec Drift-Guard CI — summary

Operationalized the v1.3 baseline with an automated **mechanical** CI gate (phase 29, 2 plans, all
verified): every PR/push proves the 32 capability specs still validate `--strict` and the
`module → capability` coverage matrix is still complete (275/275, 0 gaps), via
`scripts/audit-coverage-matrix.py` and a CI job. This catches structural drift (a spec deleted, a
module unmapped) but — by construction — **cannot detect semantic drift**: a spec whose `SHALL`
text still validates while the code it maps to no longer behaves that way. That gap is v1.5's scope.

## v1.5 Semantic Drift-Guard — summary

Shipped a repeatable, agent-assisted **semantic** spec-fidelity checker
(`scripts/semantic_drift_check.py` + a scheduled `semantic-drift.yml` workflow) that reviews each
of the 32 capability specs' `SHALL`/`MUST` requirements against the live `whilly/` code they map to
(via `openspec/COVERAGE-MATRIX.md`), emitting severity-rated, `file:line`-evidence-backed findings
triaged code-bug vs spec-overstatement. Phases 30–33: single-spec engine → bounded/resilient
6-cluster fan-out over all 32 specs with JSON artifact + human summary → scheduled (non-PR) CI job
with `--fail-on {none,high}` gating → known-drift fixture validation (live canary confirmed
drifted→HIGH, clean→clean). Additive to v1.4's mechanical gate; **zero `whilly/` behavior change**
(tooling under `scripts/`, no opsx delta). 12/12 requirements, 4/4 phases verified, audit passed.
Full detail: `.planning/milestones/v1.5-ROADMAP.md`.

## Current Milestone

None active. Start the next milestone with `/gsd-new-milestone` (defines fresh requirements +
roadmap). Behavior changes flow through `opsx` proposals updating the relevant
`openspec/specs/<slug>/spec.md` (see `openspec/FORWARD-PROCESS.md`).
