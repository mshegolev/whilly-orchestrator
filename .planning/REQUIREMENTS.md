# Requirements: Whilly Orchestrator GSD Roadmap

**Defined:** 2026-05-08
**Core Value:** Operators can safely coordinate AI-assisted engineering work with auditable state, human control, and verification before claiming success.

## v1 Requirements

### Operator UI

- [x] **OPUI-01**: TUI and WUI expose the same `p` pause workers, `R` resume workers, and refresh semantics.
- [x] **OPUI-02**: Worker pause/resume is global backend state, not a UI refresh freeze.
- [x] **OPUI-03**: TUI and WUI human-review decisions use one shared review-decision command path.
- [x] **OPUI-04**: WUI preserves active surface, filter text, selected review row, and dashboard input focus across refresh/SSE swaps.
- [x] **OPUI-05**: WUI admin bearer and reviewer inputs are available without permanently occupying topbar space.
- [ ] **OPUI-06**: WUI mobile table layouts expose row details/actions without cramped horizontal scrolling.
- [ ] **OPUI-07**: TUI and WUI table columns follow a shared operator contract or document intentional differences.
- [ ] **OPUI-08**: Reject and request-changes actions have clearer labels, tooltips, and recovery affordances.

### Documentation Pack Alignment

- [x] **DOC-01**: README/docs describe Whilly as a control plane with explicit current-vs-target boundaries.
- [x] **DOC-02**: Compliance reports distinguish implemented, partial, and future capabilities.
- [x] **DOC-03**: Negative non-goal wording is not treated as a positive capability claim.
- [ ] **DOC-04**: Current docs and compliance wording stay synchronized as hardening phases ship.

### Runtime Verification And Safety

- [ ] **VER-01**: Project-profile verification commands are wired into generated plans and worker execution.
- [ ] **SEC-01**: Secret linting covers task descriptions, comments, config values, runner prompts, and external feedback.
- [ ] **SEC-02**: Runner environments are scrubbed to an explicit allowlist plus configured required tokens.
- [ ] **SEC-03**: Command and prompt guard failures emit auditable reasons.
- [ ] **ROLL-01**: Operators can create backup tags before risky branch mutation.
- [ ] **ROLL-02**: Branch protection/preflight checks run before push, merge, or restore operations.
- [ ] **ROLL-03**: Rollback restore is explicit, auditable, and confirmation-gated.

### Repair And Governance

- [ ] **CI-01**: CI status polling can be used as a configured verification or sink stage.
- [ ] **CI-02**: Repair attempts are bounded, auditable, and stop with escalation when budgets are exhausted.
- [ ] **GOV-01**: Governance policy scores risk for migrations, auth, infra, dependencies, release actions, and external PR behavior.
- [ ] **GOV-02**: Semantic memory is either implemented deterministically from event/task history or explicitly deferred from current scope.

## v2 Requirements

### Future Capability

- **AUTO-01**: Automatic PR review feedback repair loop runs continuously with clear budgets and approval gates.
- **MULTI-01**: True dependency-aware multi-repo scheduling and workspace orchestration.
- **ISO-01**: Full per-task VM/container isolation backend.
- **MEM-01**: Semantic long-term memory that never overrides deterministic evidence.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Default auto-merge or production release | Externally visible mutation must remain opt-in and human-approved. |
| Claiming full sandbox/VM isolation before implementation | Current hardening is guards and allowlists, not full isolation. |
| Treating semantic recall as source of truth | Audit events, task history, PR evidence, and verification logs remain primary. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| OPUI-01 | Phase 1 | Complete |
| OPUI-02 | Phase 1 | Complete |
| OPUI-03 | Phase 2 | Complete |
| OPUI-04 | Phase 3 | Complete |
| OPUI-05 | Phase 4 | Complete |
| OPUI-06 | Phase 5 | Pending |
| OPUI-07 | Phase 6 | Pending |
| OPUI-08 | Phase 7 | Pending |
| DOC-01 | Phase 1 | Complete |
| DOC-02 | Phase 1 | Complete |
| DOC-03 | Phase 1 | Complete |
| DOC-04 | Phase 12 | Pending |
| VER-01 | Phase 8 | Pending |
| SEC-01 | Phase 9 | Pending |
| SEC-02 | Phase 9 | Pending |
| SEC-03 | Phase 9 | Pending |
| ROLL-01 | Phase 10 | Pending |
| ROLL-02 | Phase 10 | Pending |
| ROLL-03 | Phase 10 | Pending |
| CI-01 | Phase 11 | Pending |
| CI-02 | Phase 11 | Pending |
| GOV-01 | Phase 12 | Pending |
| GOV-02 | Phase 12 | Pending |

**Coverage:**
- v1 requirements: 23 total
- Mapped to phases: 23
- Unmapped: 0

---
*Requirements defined: 2026-05-08*
*Last updated: 2026-05-08 after migrating superpowers artifacts into GSD*
