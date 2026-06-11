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
| v1.2 Adoption & live-ops | Active | — | This file |

## Current Milestone: v1.2 Adoption & live-ops

**Goal:** Take Whilly from "functionally complete on the dev machine" to "operable against real
Jira/GitLab work on an operator machine" by closing the deferred live-validation and ops backlog.

**Phase numbering:** v1.0 used phases 1–12; v1.1 used phases 13–17 (plus 13.1, 13.2). v1.2 starts
at Phase 18.

## Phases

- [x] **Phase 18: Migration Chain Validation** - Alembic chain runs green from empty Postgres in Docker with a repeatable CI entry point (completed 2026-06-11)
- [x] **Phase 19: Live Authenticated Smoke** - Jira and GitLab smoke runs execute on a real operator machine and produce persisted audit evidence (completed 2026-06-11)
- [ ] **Phase 20: Jira Watcher Daemon** - Long-running `whilly jira watch` daemon wraps one-shot poll with configurable interval, lifecycle controls, and global-pause/readiness gates

## Phase Details

### Phase 18: Migration Chain Validation

**Goal**: The full Alembic migration chain is verified repeatable from a clean state, giving operators and CI confidence in the data layer before live integration work begins.
**Depends on**: Nothing (standalone Docker infrastructure)
**Requirements**: MIG-01, MIG-02
**Success Criteria** (what must be TRUE):

  1. Operator runs a single command against an empty Docker Postgres and all migrations apply without error
  2. The same command re-runs from a reset container and produces the identical green result (idempotency proof)
  3. A CI entry point (script or Makefile target) exists that can be invoked without manual steps or operator-specific environment setup
  4. The chain result is recorded as evidence an operator can inspect (exit code, migration count, final schema hash or revision)**Plans**: 2 plans

**Wave 1**

  - [x] 18-01-PLAN.md — Extend full-chain test to 028 + write inspectable evidence (MIG-01)

**Wave 2** *(blocked on Wave 1 completion)*

  - [x] 18-02-PLAN.md — migrate-chain Makefile target + migration-chain CI job (MIG-02)

### Phase 19: Live Authenticated Smoke

**Goal**: Jira and GitLab integrations are validated on a real operator machine with real credentials, and every smoke run leaves persisted audit evidence for review.
**Depends on**: Phase 18 (data layer verified before live sessions hit the DB)
**Requirements**: LIVE-01, LIVE-02, LIVE-03
**Success Criteria** (what must be TRUE):

  1. Operator follows documented setup steps, runs `whilly jira smoke` (or equivalent), and gets a pass/fail result against a real Jira project with classify, history, comments, and link checks exercised
  2. Operator runs `whilly gitlab smoke` (or equivalent) and gets a pass/fail result against a real repository with link-refresh and repo-hint checks exercised
  3. Each smoke run writes a persisted report file (JSON or Markdown) the operator can read after the run completes
  4. Smoke failure messages identify which check failed and what the operator should verify (credentials, project key, repo path), not just a raw exception

**Plans**: 4 plans

**Wave 1**

  - [x] 19-01-PLAN.md — Shared SmokeReport helper: accumulator, redacted report writer, exit codes (LIVE-03)

**Wave 2** *(blocked on Wave 1 completion)*

  - [x] 19-02-PLAN.md — `whilly jira smoke` action: read-only poll-cycle checks + report (LIVE-01, LIVE-03)
  - [x] 19-03-PLAN.md — `whilly gitlab` group + smoke action: token-auth ping + repo-hint + registration (LIVE-02, LIVE-03)

**Wave 3** *(blocked on Wave 2 completion)*

  - [x] 19-04-PLAN.md — Live smoke docs section + docs regression test (LIVE-01, LIVE-02, LIVE-03)

### Phase 20: Jira Watcher Daemon

**Goal**: Operators can run a continuous Jira intake daemon that wraps the validated one-shot poll cycle, with full lifecycle controls and the existing global-pause and readiness gates honored before any autonomous work is dispatched.
**Depends on**: Phase 19 (poll cycle validated on real Jira before daemon wraps it)
**Requirements**: WATCH-01, WATCH-02, WATCH-03
**Success Criteria** (what must be TRUE):

  1. Operator runs `whilly jira watch` and the daemon executes the one-shot poll cycle on a configurable interval without manual intervention
  2. Operator can stop the watcher gracefully and inspect its current status (running/stopped, last poll time, error count) via a status command or log
  3. Transient Jira/GitLab failures are retried with exponential backoff and each retry and failure is recorded as an audit event the operator can query
  4. When global worker pause is active, the watcher does not dispatch any autonomous work until pause is lifted
  5. When code/test readiness gates are not satisfied, the watcher records the block reason as an audit event and waits rather than dispatching

**Plans**: TBD

## Progress Table

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 18. Migration Chain Validation | 2/2 | Complete    | 2026-06-11 |
| 19. Live Authenticated Smoke | 4/4 | Complete   | 2026-06-11 |
| 20. Jira Watcher Daemon | 0/? | Not started | - |

## Deferred Scope

- Browser and assistive-technology QA for the full WUI operator workflow (OPQA-01, future milestone).
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacement of the current Jinja/HTMX WUI or Rich TUI architecture.

---
*Roadmap created: 2026-06-11 for milestone v1.2*
