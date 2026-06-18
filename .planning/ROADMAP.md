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
| v1.5 Semantic Drift-Guard | Active | — | `.planning/REQUIREMENTS.md`, this roadmap |

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

## Current Milestone: v1.5 Semantic Drift-Guard

**Goal:** Add a repeatable, agent-assisted *semantic* spec-fidelity check that reviews each
capability spec's `SHALL`/`MUST` requirements against the live `whilly/` code it maps to (via
`openspec/COVERAGE-MATRIX.md`), emitting severity-rated, `file:line`-evidence-backed findings
triaged as code-bug vs spec-overstatement. Additive to v1.4's mechanical gate (which stays).
LLM-assisted ⇒ runs as a **scheduled** CI job, not per-PR. Validated by reproducing the recent
manual audit's known findings against a fixture.

**Build path:** detection engine core → cluster-parallel run + reporting → scheduled CI
integration → known-drift fixture validation.

**Granularity:** standard. Phase numbering continues from v1.4 (which ended at Phase 29), so v1.5
begins at **Phase 30**.

## Phases

- [ ] **Phase 30: Detection Engine Core** - Per-spec SHALL/MUST semantic review against mapped modules, emitting triaged, evidence-backed findings.
- [ ] **Phase 31: Cluster-Parallel Run & Reporting** - Bounded, resilient 6-cluster fan-out over all 32 specs with self-describing run metadata and machine + human reports.
- [ ] **Phase 32: Scheduled CI Integration** - Wire the check as a scheduled (non-PR) job with artifact upload and configurable report-only vs fail-on-HIGH gating.
- [ ] **Phase 33: Known-Drift Fixture Validation** - Prove the guard detects a planted HIGH drift and reports a clean spec as clean.

## Phase Details

### Phase 30: Detection Engine Core
**Goal**: A single capability spec can be reviewed against its mapped `whilly/` code, producing structured per-requirement findings that are severity-rated, triaged, and backed by file:line evidence.
**Depends on**: Nothing (first phase of v1.5; builds on existing v1.4 artifacts)
**Requirements**: DETECT-01, DETECT-02, DETECT-03, DETECT-04
**Success Criteria** (what must be TRUE):
  1. Operator can run the checker against one capability slug and get per-`SHALL`/`MUST`-requirement findings reviewing the spec text against its mapped modules.
  2. Each emitted finding carries severity (HIGH/MEDIUM/LOW), capability slug, requirement name, a one-line drift description, and `file:line` code evidence.
  3. Each finding is labeled `code-bug` or `spec-overstatement` with a short rationale.
  4. The module review set for any spec is derived live from `openspec/COVERAGE-MATRIX.md`, not a hand-maintained second mapping (changing the matrix changes the review set).
**Plans**: TBD

### Phase 31: Cluster-Parallel Run & Reporting
**Goal**: A single run fans out the detection engine across all 32 specs in the proven 6-cluster pattern — bounded, resilient, self-describing — and emits both a machine-readable findings artifact and a human summary.
**Depends on**: Phase 30
**Requirements**: RUN-01, RUN-02, RUN-03, REPORT-01, REPORT-02
**Success Criteria** (what must be TRUE):
  1. One invocation reviews all 32 capability specs via parallel cluster fan-out and the run completes covering every spec.
  2. A cluster or spec review that errors degrades to a recorded per-unit error instead of aborting the whole run.
  3. The run output records the model used and the spec/code commit (or tree state) reviewed, so the findings set is reproducible.
  4. The run writes a machine-readable findings artifact (e.g. JSON) plus a human summary with per-cluster H/M/L and clean tallies.
  5. The summary reports coverage (specs reviewed / 32) and distinguishes confirmed findings from clean specs.
**Plans**: TBD

### Phase 32: Scheduled CI Integration
**Goal**: The semantic check runs unattended on a schedule, surfaces its results as CI artifacts and summary, and gates per a configurable posture — without touching the v1.4 per-PR mechanical gate.
**Depends on**: Phase 31
**Requirements**: CI-01, CI-02
**Success Criteria** (what must be TRUE):
  1. A scheduled (cron) or manually-dispatched CI job runs the semantic check separately from, and without blocking, the v1.4 per-PR mechanical gate.
  2. The scheduled job uploads the findings artifact and renders the human summary in CI output.
  3. The job's gating posture is configurable between report-only (always green) and fail-on-HIGH (red when a HIGH finding is present).
**Plans**: TBD

### Phase 33: Known-Drift Fixture Validation
**Goal**: The guard is demonstrably trustworthy — proven against a deliberately drifted spec/code pair to detect a real HIGH drift while reporting an undrifted spec as clean.
**Depends on**: Phase 32
**Requirements**: VALID-01
**Success Criteria** (what must be TRUE):
  1. A known-drift fixture (a deliberately drifted spec/code pair) exists and the checker run against it reports a HIGH semantic-drift finding with file:line evidence.
  2. The same run reports a non-drifted control spec as clean (no false-positive finding).
  3. The validation is reproducible — documented inputs and expected verdict so a future run confirms the guard still works.
**Plans**: TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 30. Detection Engine Core | 0/0 | Not started | - |
| 31. Cluster-Parallel Run & Reporting | 0/0 | Not started | - |
| 32. Scheduled CI Integration | 0/0 | Not started | - |
| 33. Known-Drift Fixture Validation | 0/0 | Not started | - |
