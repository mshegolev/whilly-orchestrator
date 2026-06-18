---
gsd_state_version: 1.0
milestone: v1.5
milestone_name: Semantic Drift-Guard
status: planning
last_updated: "2026-06-19T00:00:00.000Z"
last_activity: 2026-06-19 — Completed 30-02-PLAN.md (review_spec pipeline + claude_reviewer + --slug CLI, DETECT-01)
progress:
  total_phases: 4
  completed_phases: 0
  total_plans: 2
  completed_plans: 2
  percent: 25
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-18)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** v1.5 — build an agent-assisted *semantic* spec-fidelity checker that detects
spec↔code drift the v1.4 mechanical gate cannot.

## Current Position

Phase: 30 — Detection Engine Core (in progress)
Plan: 02 of 02 complete
Status: Plans 30-01 (core) and 30-02 (pipeline + CLI) done; DETECT-01..04 satisfied — phase ready for verification
Last activity: 2026-06-19 — Completed 30-02-PLAN.md (review_spec pipeline + claude_reviewer + --slug CLI, DETECT-01)

## Active Roadmap

See: `.planning/ROADMAP.md`

| Phase | Name | Requirements | Status |
|-------|------|--------------|--------|
| 30 | Detection Engine Core | DETECT-01..04 | 🟡 In progress (2/2 plans) |
| 31 | Cluster-Parallel Run & Reporting | RUN-01..03, REPORT-01..02 | ⬜ Not started |
| 32 | Scheduled CI Integration | CI-01..02 | ⬜ Not started |
| 33 | Known-Drift Fixture Validation | VALID-01 | ⬜ Not started |

**Progress:** [██████████] 100%

## Active Scope

**Milestone type:** internal tooling build. Adds a new semantic drift-detection mechanism;
**additive** to v1.4 — the mechanical gate (`scripts/audit-coverage-matrix.py` + per-PR CI) stays.

**Build path:** detection engine core (30) → cluster-parallel run + reporting (31) → scheduled CI
integration (32) → known-drift fixture validation (33).

**Reuse baseline:** `openspec/COVERAGE-MATRIX.md` (spec→module review set, no second mapping),
`scripts/audit-coverage-matrix.py` (standalone-audit-script model), and the existing Claude CLI
shell-out path (`whilly/adapters/runner/claude_cli.py`). The proven manual-audit pattern is the
6-cluster parallel fan-out (orchestration, prd-decision, integrations, operator-surface, platform,
safety-quality) over all 32 specs.

**Cadence decision:** LLM-assisted ⇒ non-deterministic and costly ⇒ runs on a scheduled CI cadence
(cron/manual dispatch), NOT every PR. Findings must be evidence-backed (`file:line`) and
reproducible (run records model + reviewed commit).

## Recent Decisions

- v1.5 (2026-06-18): Semantic check is strictly additive — v1.4's mechanical gate is not replaced or
  weakened. The two run on different cadences (per-PR mechanical vs scheduled semantic).

- v1.5 (2026-06-18): Detection engine (Phase 30) lands before orchestration — fan-out is only worth
  building once a single-spec review reliably produces triaged, evidence-backed findings.

- v1.5 (2026-06-18): The guard ships proven, not plausible — Phase 33 validates against a planted
  known-drift fixture (detect a HIGH, report clean as clean) before the milestone closes.

## Accumulated Context

### Roadmap Evolution

- Phases 18-20 shipped for milestone v1.2 (migration validation, live smoke, watcher daemon),
  archived 2026-06-12.

- Phases 21-28 shipped for milestone v1.3 (OpenSpec normative baseline across the whole project).

- Phase 29 shipped for milestone v1.4 (mechanical spec-drift CI gate).

- Phases 30-33 defined for milestone v1.5: agent-assisted semantic spec-fidelity drift detection.

## Previous Milestones

- v1.0 shipped and archived on 2026-05-08.
- v1.1 shipped and archived on 2026-05-11.
- v1.2 shipped and archived on 2026-06-12.
- v1.3 shipped on 2026-06-16.
- v1.4 shipped on 2026-06-18.

Archives:

- `.planning/milestones/v1.0-ROADMAP.md`, `v1.0-REQUIREMENTS.md`, `v1.0-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-ROADMAP.md`, `v1.1-REQUIREMENTS.md`, `v1.1-MILESTONE-AUDIT.md`, `v1.1-RETROSPECTIVE.md`
- `.planning/milestones/v1.2-ROADMAP.md`, `v1.2-REQUIREMENTS.md`, `v1.2-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.3-ROADMAP.md`, `v1.3-REQUIREMENTS.md`
- `.planning/milestones/v1.4-ROADMAP.md`, `v1.4-REQUIREMENTS.md`, `v1.4-MILESTONE-AUDIT.md`, `v1.4-RETROSPECTIVE.md`

## Deferred Items

- Auto-opening `opsx` proposals or code-fix PRs from confirmed findings — v1.5 detects/reports only.
- Per-PR (diff-scoped) semantic checking — possible once cost/latency are characterized.
- Historical drift trend tracking / dashboards.

## Next Step

Plan Phase 30 with `/gsd-plan-phase 30`.

## Operator Next Steps

- Plan the first phase: `/gsd-plan-phase 30` (Detection Engine Core).
