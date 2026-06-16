---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: OpenSpec Project Baseline
status: Phase 27 complete (verified) — all 32 specs written; ready for `/gsd-plan-phase 28` (milestone closeout)
last_updated: "2026-06-16T00:00:00.000Z"
last_activity: 2026-06-16 — Phase 27 verified + closed (4 safety/quality specs; ALL 32 specs pass openspec validate --all --strict)
progress:
  total_phases: 8
  completed_phases: 7
  total_plans: 24
  completed_plans: 24
  percent: 87
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-13)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** v1.3 — capture Whilly's behavior as normative OpenSpec capability specs.

## Current Position

Phase: 27 — Safety & Quality Cluster (in progress)
Plan: 27-02 complete — SAFE-03 `quality-compliance-audit` + SAFE-04 `verification-gates` specs written. All four Phase 27 specs (SAFE-01..04) authored. Remaining for Phase 27: phase verification/wrap.
Status: 27-02 reverse-spec'd two subsystem-altitude specs from real v4 code, both passing openspec validate --strict (0 errors/0 warnings). quality-compliance-audit (SAFE-03): per-language QualityGate Protocol (detect/run → GateResult, never raises on lint/test failure or missing binary/timeout) + multi-language detect_gates/run_all aggregation (gate_kind="multi", no-gates = passed True), deterministic target-doc ComplianceReport via `whilly compliance report` (PASS/PARTIAL/FAIL/UNKNOWN, present-but-unwired = PARTIAL), append-only JsonlEventSink.record (one JSON obj/line, OSError swallowed best-effort mirror of Postgres events), and qa-release collect/plan/scaffold-tests (refuses to clobber non-generated tests without --force). verification-gates (SAFE-04): LIVE pipeline run_verification_commands → VerificationRunOutcome (required_failed gates DONE, warning-only does not), started/result events with secret redaction, env allowlist + non-hanging timeout/blocked, human-review checkpoint gate (requires_human_review/build_human_review_checkpoint/is_human_review_approved + required/approved/rejected/changes_requested events), CI verification run_ci_verification; legacy verifier.verify_task commit-revert path marked unwired (confirmed: no worker-path callers). Documentation-only; zero whilly/ changes.
Last activity: 2026-06-16 — Phase 27 plan 27-02 executed (SAFE-03 + SAFE-04). Next: Phase 27 verification/wrap, then Phase 28 (coverage audit + validate + sync).

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
| 28 | Forward Process, Coverage & Validation | FWD-01..02, COV-01, VAL-01..02 | Not started |

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
