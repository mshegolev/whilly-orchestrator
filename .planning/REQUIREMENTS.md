# Requirements: Whilly Orchestrator — Milestone v1.2 Adoption & live-ops

**Defined:** 2026-06-11
**Core Value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.

## v1.2 Requirements

Requirements for milestone v1.2. Each maps to roadmap phases.

### Jira Watcher

- [ ] **WATCH-01**: Operator can run a long-running `whilly jira watch` daemon that wraps the
  one-shot poll cycle on a configurable interval.
- [ ] **WATCH-02**: Operator can start, stop, and inspect watcher status; transient Jira/GitLab
  failures are retried with backoff and recorded as audit events.
- [ ] **WATCH-03**: Watcher honors global worker pause and code/test readiness gates before
  dispatching any autonomous work.

### Live Smoke

- [ ] **LIVE-01**: Operator can run an authenticated Jira smoke (classify, history, comments,
  links) against a real Jira project with documented setup.
- [ ] **LIVE-02**: Operator can run an authenticated GitLab smoke (link refresh, repo hints)
  against a real repository.
- [ ] **LIVE-03**: Smoke runs produce persisted audit evidence/reports an operator can review.

### Migration Chain

- [x] **MIG-01**: The full Alembic migration chain runs green from an empty Postgres in Docker.
- [x] **MIG-02**: Chain validation is repeatable via a scripted/CI entry point, not a one-off
  manual run.

## Future Requirements

Deferred to a future milestone. Tracked but not in the current roadmap.

### Operator QA

- **OPQA-01**: Browser and assistive-technology QA for the complete WUI operator workflow.

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| A1a/A1b auth-form annotation fix | Defect never reproduced; real CI failure was fixed via A0 (PR #271) |
| Auto-merge / fully autonomous release | Standing exclusion — externally visible mutation stays opt-in and auditable |
| Full VM/container isolation claims | No per-task isolation backend implemented yet |
| Replacement of Jinja/HTMX dashboard or Rich TUI | Architecture replacement is not adoption work |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| MIG-01 | Phase 18 | Complete |
| MIG-02 | Phase 18 | Complete |
| LIVE-01 | Phase 19 | Pending |
| LIVE-02 | Phase 19 | Pending |
| LIVE-03 | Phase 19 | Pending |
| WATCH-01 | Phase 20 | Pending |
| WATCH-02 | Phase 20 | Pending |
| WATCH-03 | Phase 20 | Pending |

**Coverage:**
- v1.2 requirements: 8 total
- Mapped to phases: 8
- Unmapped: 0 ✓

---
*Requirements defined: 2026-06-11*
*Last updated: 2026-06-11 — phase mappings added after roadmap creation*
