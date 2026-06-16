---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: OpenSpec Project Baseline
status: ✅ Milestone v1.3 SHIPPED — all 8 phases (21-28) complete, 32 specs, 275/275 coverage, 32/0 strict
last_updated: "2026-06-16T00:00:00.000Z"
last_activity: 2026-06-16 — Phase 28 verified + closed; milestone v1.3 OpenSpec Project Baseline complete
progress:
  total_phases: 8
  completed_phases: 8
  total_plans: 26
  completed_plans: 26
  percent: 100
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-13)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** v1.3 — capture Whilly's behavior as normative OpenSpec capability specs.

## Current Position

Phase: 28 — Forward Process, Coverage & Validation (closeout gates complete)
Plan: 28-02 complete — COV-01 coverage matrix audited at 100% (live `find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l` = 275 == 275 body rows, 0 UNMAPPED, 0 double-mapped, capability column is exactly the 32 TAXONOMY slugs and all 32 each map ≥1 module; bijective path set vs live find; no drift so rows unchanged); VAL-01 `openspec validate --all --strict` → 32 passed / 0 failed; VAL-02 consolidated normative-accuracy review (32/32 specs SHALL/MUST + ≥1 #### Scenario:, six cluster VERIFICATION reports phases 22-27 all passed and mapped to slugs, legacy-as-current sweep confirmed decomposition/reporting/recovery-self-healing/verification-gates/state-persistence/budget-resource-guards still mark legacy/unwired/no-op). All three recorded as dated notes in openspec/COVERAGE-MATRIX.md. Documentation-only; zero whilly/ changes; no capability spec needed a fix. Prior: 28-01 (FWD-01 + FWD-02).
Status: 27-02 reverse-spec'd two subsystem-altitude specs from real v4 code, both passing openspec validate --strict (0 errors/0 warnings). quality-compliance-audit (SAFE-03): per-language QualityGate Protocol (detect/run → GateResult, never raises on lint/test failure or missing binary/timeout) + multi-language detect_gates/run_all aggregation (gate_kind="multi", no-gates = passed True), deterministic target-doc ComplianceReport via `whilly compliance report` (PASS/PARTIAL/FAIL/UNKNOWN, present-but-unwired = PARTIAL), append-only JsonlEventSink.record (one JSON obj/line, OSError swallowed best-effort mirror of Postgres events), and qa-release collect/plan/scaffold-tests (refuses to clobber non-generated tests without --force). verification-gates (SAFE-04): LIVE pipeline run_verification_commands → VerificationRunOutcome (required_failed gates DONE, warning-only does not), started/result events with secret redaction, env allowlist + non-hanging timeout/blocked, human-review checkpoint gate (requires_human_review/build_human_review_checkpoint/is_human_review_approved + required/approved/rejected/changes_requested events), CI verification run_ci_verification; legacy verifier.verify_task commit-revert path marked unwired (confirmed: no worker-path callers). Documentation-only; zero whilly/ changes.
Last activity: 2026-06-16 — Phase 28 plan 28-02 executed (COV-01 audit + VAL-01 validate + VAL-02 normative review; all recorded in COVERAGE-MATRIX.md). v1.3 milestone closeout gates satisfied (FWD-01, FWD-02, COV-01, VAL-01, VAL-02 all done).

## Active Roadmap

See: `.planning/ROADMAP.md`

| Phase | Name | Requirements | Status |
|-------|------|--------------|--------|
| 21 | Spec Baseline & Taxonomy | BASE-01..04 | ✅ Complete |
| 22 | Orchestration Cluster | ORCH-01..07 | ✅ Complete (verified) |
| 23 | PRD Pipeline & Decision | PRD-01..05 | ✅ Complete (verified) |
| 24 | Integrations Cluster | INT-01..06 | ✅ Complete (verified) |
| 25 | Operator Surface Cluster | OPS-01..05 | ✅ Complete (verified) |
| 26 | Platform Cluster | PLAT-01..05 | ✅ Complete (verified) |
| 27 | Safety & Quality Cluster | SAFE-01..04 | ✅ Complete (verified) |
| 28 | Forward Process, Coverage & Validation | FWD-01..02, COV-01, VAL-01..02 | ✅ Complete (verified) |

## Active Scope

**Milestone type:** spec capture, not feature build. No `whilly/` behavior changes this milestone.

**Tooling baseline:** OpenSpec 1.4.1 initialized 2026-06-13 (schema `spec-driven`, Claude tool wired,
5 `/opsx:*` commands). `openspec/specs/` and `openspec/changes/` are empty — this milestone fills
`specs/`.

**Granularity decision:** capability = subsystem (~30 capabilities) + a `module → capability`
coverage matrix proving all 242 modules are mapped. NOT one spec file per module.

**Posture decision:** specs are normative & testable (MUST/SHALL + `#### Scenario:` blocks that pass
`openspec validate --strict`), not descriptive snapshots.

**Role decision:** forward delta-only after baseline. OpenSpec = living WHAT; GSD = HOW/execution.

## Recent Decisions

- v1.3 (2026-06-13): Rejected literal per-module specs (242 files) — chose subsystem capabilities +
  coverage matrix to keep specs normative and maintainable while still proving full coverage.

- v1.3 (2026-06-13): Phase 21 (taxonomy + conventions) gates all later phases — the spec format must
  be fixed once, before 30 specs are written against it.

- v1.3 (2026-06-13): Phases 22–27 are independent once 21 lands and may be reordered/parallelized;
  Phase 28 (coverage audit + validate + sync) closes the milestone.

- v1.3 (2026-06-13): Auth/security carried context — ADR-001 path-sink fixes and the flag-gated
  OIDC/WebAuthn stack are existing behavior to be specified (PLAT-02), not changed.

## Accumulated Context

### Roadmap Evolution

- Phases 18-20 shipped for milestone v1.2 (migration validation, live smoke, watcher daemon),
  archived 2026-06-12.

- Phases 21-28 defined for milestone v1.3: OpenSpec normative baseline across the whole project.

## Previous Milestones

- v1.0 shipped and archived on 2026-05-08.
- v1.1 shipped and archived on 2026-05-11.
- v1.2 shipped and archived on 2026-06-12.

Archives:

- `.planning/milestones/v1.0-ROADMAP.md`, `v1.0-REQUIREMENTS.md`, `v1.0-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-ROADMAP.md`, `v1.1-REQUIREMENTS.md`, `v1.1-MILESTONE-AUDIT.md`, `v1.1-RETROSPECTIVE.md`
- `.planning/milestones/v1.2-ROADMAP.md`, `v1.2-REQUIREMENTS.md`, `v1.2-MILESTONE-AUDIT.md`

## Deferred Items

- Browser/screen-reader QA for the complete WUI operator workflow (OPQA-01).
- Behavior changes surfaced while speccing → capture as `opsx` proposals / future milestone, not
  this one.

## Next Step

Plan Phase 21 with `/gsd-plan-phase 21`.

## Operator Next Steps

- Plan the first phase: `/gsd-plan-phase 21` (Spec Baseline & Taxonomy).
