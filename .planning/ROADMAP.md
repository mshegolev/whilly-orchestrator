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
| v1.3 OpenSpec Project Baseline | Active | — | This file |

## Current Milestone: v1.3 OpenSpec Project Baseline

**Goal:** Capture Whilly's current *guaranteed* behavior as ~30 normative, testable OpenSpec
capability specs under `openspec/specs/`, with a `module → capability` coverage matrix proving all
242 `whilly/` modules are accounted for. Spec-capture only — no `whilly/` behavior changes. After the
baseline, behavior changes flow through `opsx` proposals (forward delta-only); GSD keeps owning
execution.

**Phase numbering:** v1.0 used phases 1–12; v1.1 used 13–17; v1.2 used 18–20. v1.3 starts at Phase 21.

**Decisions locked (2026-06-13):** capability = subsystem (not one spec per module); posture is
normative & testable (MUST/SHALL + `#### Scenario:` that passes `openspec validate --strict`); role
is forward delta-only (OpenSpec = living WHAT, GSD = HOW). Treat the milestone as a hypothesis test:
after Phase 22, review "continue or cut" before committing to phases 23–28.

## Phases

- [x] **Phase 21: Spec Baseline & Taxonomy** - Capability taxonomy, authoring conventions, coverage-matrix scaffold, and one reference spec — gates the spec format for every later phase
- [x] **Phase 22: Orchestration Cluster** - The seven load-bearing orchestration contracts captured as normative specs (the core loop's guarantees)
- [ ] **Phase 23: PRD Pipeline & Decision** - PRD generation/wizard, task generation, decomposition, and the Decision Gate specified
- [ ] **Phase 24: Integrations Cluster** - Jira, GitLab, GitHub, watcher daemon, notifications, and MCP integration surfaces specified
- [ ] **Phase 25: Operator Surface Cluster** - TUI dashboard, web status/API, reporting, CLI exit-code contract, and operator views/logs specified
- [ ] **Phase 26: Platform Cluster** - Config env-var contract, auth/security, scheduling, state persistence, and self-update/doctor specified
- [ ] **Phase 27: Safety & Quality Cluster** - Budget/resource guards, recovery/self-healing, quality/compliance/audit, and verification gates specified
- [ ] **Phase 28: Forward Process, Coverage & Validation** - Coverage matrix proven 100%, all specs validated, forward delta-only process documented — closes the milestone

## Phase Details

### Phase 21: Spec Baseline & Taxonomy

**Goal**: The OpenSpec capability taxonomy, authoring conventions, and coverage-matrix scaffold are established so every later phase writes specs in one validated, consistent format with provable full coverage.
**Depends on**: Nothing (foundational — gates all later phases)
**Requirements**: BASE-01, BASE-02, BASE-03, BASE-04
**Success Criteria** (what must be TRUE):

  1. `openspec/specs/` carries a documented taxonomy of ~30 capabilities, each with a slug and one-line purpose
  2. A `module → capability` coverage matrix lists all 242 `whilly/` modules, each mapped to exactly one capability (gaps explicitly marked, none silent)
  3. An authoring-conventions doc defines MUST/SHALL normative language and the requirement + `#### Scenario:` format that `openspec validate --strict` accepts
  4. `openspec/project.md` (or config context) carries Whilly's stack, conventions, and domain glossary
  5. One capability is written end-to-end as a reference exemplar and passes `openspec validate --strict`

**Plans**: 4 plans across 3 waves
- [ ] 21-01-PLAN.md — Authoring conventions (BASE-03) + project context (BASE-04) — format/context gates [wave 1]
- [ ] 21-02-PLAN.md — Capability taxonomy index + 32 capability stub directories (BASE-01) [wave 2]
- [ ] 21-03-PLAN.md — task-model-fsm reference exemplar spec passing `openspec validate --strict` (SC-5) [wave 3]
- [ ] 21-04-PLAN.md — module→capability coverage matrix, zero silent gaps (BASE-02) [wave 3]

### Phase 22: Orchestration Cluster

**Goal**: The seven load-bearing orchestration contracts are captured as normative specs so the core loop's guarantees are explicit and machine-checkable.
**Depends on**: Phase 21 (spec format + conventions)
**Requirements**: ORCH-01, ORCH-02, ORCH-03, ORCH-04, ORCH-05, ORCH-06, ORCH-07
**Success Criteria** (what must be TRUE):

  1. Each of the 7 capabilities (orchestration-loop, task-model-fsm, plan-json-contract, batch-planning, agent-dispatch, worktree-isolation, result-collection) has a normative spec.md
  2. `plan-json-contract` enumerates required task fields and round-trip tolerance matching `task_manager.py`
  3. `task-model-fsm` specifies legal status transitions matching the code
  4. Each spec has ≥1 testable `#### Scenario:` and all 7 pass `openspec validate --strict`
  5. Every module these capabilities cover is checked off in the coverage matrix

**Plans**: 4 plans, all wave 1 (specs are independent, documentation-only — disjoint files → fully parallel). ORCH-02 (task-model-fsm) already done in Phase 21 — not re-planned.
- [ ] 22-01-PLAN.md — orchestration-loop spec (ORCH-01) [wave 1]
- [ ] 22-02-PLAN.md — plan-json-contract (ORCH-03) + result-collection (ORCH-07) specs [wave 1]
- [ ] 22-03-PLAN.md — agent-dispatch spec (ORCH-05) [wave 1]
- [ ] 22-04-PLAN.md — batch-planning (ORCH-04) + worktree-isolation (ORCH-06) specs [wave 1]

### Phase 23: PRD Pipeline & Decision

**Goal**: The PRD generation/wizard pipeline, task generation, decomposition, and the Decision Gate are specified normatively.
**Depends on**: Phase 21
**Requirements**: PRD-01, PRD-02, PRD-03, PRD-04, PRD-05
**Success Criteria** (what must be TRUE):

  1. 5 capabilities specced (prd-generation, prd-wizard, task-generation, decomposition, decision-gate)
  2. `decision-gate` spec captures the refuse/accept criteria and the TRIZ contradiction-analysis role
  3. `task-generation` spec defines the PRD → `tasks.json` contract
  4. Each spec has ≥1 scenario and all pass `openspec validate --strict`
  5. Covered modules checked off in the coverage matrix

**Plans**: 3 plans (wave 1, all parallel — disjoint spec files)
- [ ] 23-01-PLAN.md — prd-generation (PRD-01) + task-generation (PRD-03) specs
- [x] 23-02-PLAN.md — prd-wizard (PRD-02) + decomposition (PRD-04) specs
- [ ] 23-03-PLAN.md — decision-gate (PRD-05) spec

### Phase 24: Integrations Cluster

**Goal**: External integration surfaces are specified with their auth expectations and read-only vs mutating boundaries.
**Depends on**: Phase 21
**Requirements**: INT-01, INT-02, INT-03, INT-04, INT-05, INT-06
**Success Criteria** (what must be TRUE):

  1. 6 capabilities specced (jira-integration, gitlab-integration, github-integration, jira-watcher-daemon, notifications, mcp-integration)
  2. `jira-watcher-daemon` spec captures lifecycle, pause/readiness gates, and fail-closed behavior (Phase 20 shipped behavior)
  3. Each integration spec states auth expectations and the read-only vs mutating boundary
  4. Each spec has ≥1 scenario and all pass `openspec validate --strict`
  5. Covered modules checked off in the coverage matrix

**Plans**: TBD (defined during /gsd-plan-phase 24)

### Phase 25: Operator Surface Cluster

**Goal**: Operator-facing surfaces are specified, including the CLI exit-code contract relied on by headless callers.
**Depends on**: Phase 21
**Requirements**: OPS-01, OPS-02, OPS-03, OPS-04, OPS-05
**Success Criteria** (what must be TRUE):

  1. 5 capabilities specced (dashboard-tui, web-status-ui, reporting, cli-surface, operator-views-logs)
  2. `cli-surface` spec enumerates flags, headless JSON output, and exit codes `0/1/2/3`
  3. `dashboard-tui` spec captures dashboard states and hotkeys
  4. Each spec has ≥1 scenario and all pass `openspec validate --strict`
  5. Covered modules checked off in the coverage matrix

**Plans**: TBD (defined during /gsd-plan-phase 25)

### Phase 26: Platform Cluster

**Goal**: Platform foundations are specified, including the existing auth-hardening guarantees — captured as current behavior, not changed.
**Depends on**: Phase 21
**Requirements**: PLAT-01, PLAT-02, PLAT-03, PLAT-04, PLAT-05
**Success Criteria** (what must be TRUE):

  1. 5 capabilities specced (configuration, auth-security, scheduling, state-persistence, self-update-doctor)
  2. `configuration` spec enumerates the `WHILLY_` env-var contract and defaults
  3. `auth-security` spec captures session auth, gated password change, flag-gated OIDC/WebAuthn, and the ADR-001 path-sink mitigation as existing behavior
  4. `state-persistence` spec captures the resume contract (plan/iteration/cost/sessions)
  5. Each spec has ≥1 scenario, all pass `openspec validate --strict`, covered modules checked

**Plans**: TBD (defined during /gsd-plan-phase 26)

### Phase 27: Safety & Quality Cluster

**Goal**: Safety guards and quality gates are specified with their concrete thresholds.
**Depends on**: Phase 21
**Requirements**: SAFE-01, SAFE-02, SAFE-03, SAFE-04
**Success Criteria** (what must be TRUE):

  1. 4 capabilities specced (budget-resource-guards, recovery-self-healing, quality-compliance-audit, verification-gates)
  2. `budget-resource-guards` spec captures the 80% warn / 100% kill→exit 2 thresholds
  3. `recovery-self-healing` spec captures deadlock skip, stall pause, and retry/backoff
  4. Each spec has ≥1 scenario, all pass `openspec validate --strict`, covered modules checked

**Plans**: TBD (defined during /gsd-plan-phase 27)

### Phase 28: Forward Process, Coverage & Validation

**Goal**: The baseline is closed out — full coverage proven, all specs validated, and the forward delta-only process documented so future changes update specs via `opsx`.
**Depends on**: Phase 22, Phase 23, Phase 24, Phase 25, Phase 26, Phase 27
**Requirements**: FWD-01, FWD-02, COV-01, VAL-01, VAL-02
**Success Criteria** (what must be TRUE):

  1. Coverage matrix audited at 100% — every one of the 242 modules mapped to a capability, zero gaps
  2. `openspec validate --strict` passes across all capability specs
  3. `CLAUDE.md` and `AGENTS.md` require an `opsx` spec delta for any behavior change and point at `openspec/specs/`
  4. The forward delta-only workflow (propose → apply → archive) is documented
  5. Every capability spec has been reviewed for normative accuracy against the code it describes (no descriptive-only specs)

**Plans**: TBD (defined during /gsd-plan-phase 28)

---
*Roadmap updated: 2026-06-15 — Phase 22 planned (4 plans, 1 wave; ORCH-02 already done in Phase 21)*
