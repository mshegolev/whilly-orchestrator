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

## Current Milestone

**v1.3 — OpenSpec Project Baseline** (started 2026-06-13)

Capture Whilly's current guaranteed behavior as ~30 normative, testable OpenSpec capability specs
with full module coverage. Spec-capture only — no `whilly/` behavior changes. After this baseline,
behavior changes flow through `opsx` proposals (forward delta-only); GSD keeps owning execution.

Requirements: `.planning/REQUIREMENTS.md` (41 requirements, 8 categories).

| Phase | Name | Requirements | Capabilities | Status |
|-------|------|--------------|--------------|--------|
| 21 | Spec Baseline & Taxonomy | BASE-01..04 | (scaffold) taxonomy, coverage matrix, conventions, project.md | Not started |
| 22 | Orchestration Cluster | ORCH-01..07 | orchestration-loop, task-model-fsm, plan-json-contract, batch-planning, agent-dispatch, worktree-isolation, result-collection | Not started |
| 23 | PRD Pipeline & Decision | PRD-01..05 | prd-generation, prd-wizard, task-generation, decomposition, decision-gate | Not started |
| 24 | Integrations Cluster | INT-01..06 | jira-integration, gitlab-integration, github-integration, jira-watcher-daemon, notifications, mcp-integration | Not started |
| 25 | Operator Surface Cluster | OPS-01..05 | dashboard-tui, web-status-ui, reporting, cli-surface, operator-views-logs | Not started |
| 26 | Platform Cluster | PLAT-01..05 | configuration, auth-security, scheduling, state-persistence, self-update-doctor | Not started |
| 27 | Safety & Quality Cluster | SAFE-01..04 | budget-resource-guards, recovery-self-healing, quality-compliance-audit, verification-gates | Not started |
| 28 | Forward Process, Coverage & Validation | FWD-01..02, COV-01, VAL-01..02 | (audit) coverage matrix 100%, `openspec validate --strict`, spec→main sync, contributor docs | Not started |

**Sequencing note:** Phase 21 must land first (taxonomy + conventions gate the spec format for all
later phases). Phases 22–27 are independent once 21 is done and can be reordered or parallelized.
Phase 28 closes the milestone (depends on all capability phases).

---
*Roadmap updated: 2026-06-13 — milestone v1.3 OpenSpec Project Baseline defined (phases 21–28)*
