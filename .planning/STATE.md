---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: OpenSpec Project Baseline
status: Phase 25 complete (verified) — ready for `/gsd-plan-phase 26`
last_updated: "2026-06-16T00:00:00.000Z"
last_activity: 2026-06-16 — Phase 25 verified + closed (5 operator-surface specs; 23/23 strict-valid)
progress:
  total_phases: 8
  completed_phases: 5
  total_plans: 17
  completed_plans: 17
  percent: 62
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-13)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** v1.3 — capture Whilly's behavior as normative OpenSpec capability specs.

## Current Position

Phase: 25 — Operator Surface Cluster (complete)
Plan: 25-01 + 25-02 + 25-03 complete — all five OPS specs written (OPS-01 `dashboard-tui`, OPS-02 `web-status-ui`, OPS-03 `reporting`, OPS-04 `cli-surface`, OPS-05 `operator-views-logs`).
Status: 23/23 specs pass `openspec validate --strict`. 25-03 reverse-spec'd the `whilly` CLI surface — pinning the REAL v4 exit-code contract (EXIT_OK=0, EXIT_VALIDATION_ERROR=1, EXIT_ENVIRONMENT_ERROR=2, WORKSPACE_FAILED_EXIT_CODE=-4) over the legacy 0/1/2/3 lore, the no-args-prints-HELP behavior, unknown-command→2, and the v3 legacy flag shim incl. WHILLY_HEADLESS — plus the operator log viewer (list/show/tail + cleanup), the operator-views taxonomy (surfaces/tables/actions/hotkeys/route-prefixes/artifacts), and the operator TUI hotkey state machine. Documentation-only; zero whilly/ changes.
Last activity: 2026-06-16 — Phase 25 plan 25-03 executed (cli-surface + operator-views-logs). Phase 25 complete; next: plan/execute Phase 26 (Platform cluster).

## Active Roadmap

See: `.planning/ROADMAP.md`

| Phase | Name | Requirements | Status |
|-------|------|--------------|--------|
| 21 | Spec Baseline & Taxonomy | BASE-01..04 | ✅ Complete |
| 22 | Orchestration Cluster | ORCH-01..07 | ✅ Complete (verified) |
| 23 | PRD Pipeline & Decision | PRD-01..05 | ✅ Complete (verified) |
| 24 | Integrations Cluster | INT-01..06 | ✅ Complete (verified) |
| 25 | Operator Surface Cluster | OPS-01..05 | ✅ Complete (verified) |
| 26 | Platform Cluster | PLAT-01..05 | Not started |
| 27 | Safety & Quality Cluster | SAFE-01..04 | Not started |
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
