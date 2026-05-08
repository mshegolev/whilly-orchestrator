# Roadmap: Whilly Orchestrator

## Overview

This roadmap makes GSD the canonical high-level execution plan for Whilly while preserving
superpowers plans as detailed evidence. It starts with the already shipped operator UI parity work,
continues the remaining WUI/TUI audit backlog in foundation-first order, and then moves into the
larger documentation-pack and v6 hardening backlog.

## Phases

- [x] **Phase 1: Operator pause parity** - Global worker pause/resume and WUI/TUI hotkeys aligned.
- [x] **Phase 2: Shared review decision path** - TUI and WUI review decisions use one backend command.
- [x] **Phase 3: WUI state-preserving refresh** - WUI retains local operator state across refresh/SSE swaps.
- [x] **Phase 4: Compact operator identity panel** - Admin bearer and reviewer inputs move out of the permanent topbar.
- [x] **Phase 5: Shared operator table contract** - TUI and WUI table columns follow an explicit shared contract.
- [ ] **Phase 6: Mobile WUI row actions** - Mobile tables expose row details and actions without cramped horizontal scroll.
- [ ] **Phase 7: Review action affordances** - Reject/request-changes paths become clearer and safer.
- [ ] **Phase 8: Sandbox and secrets hardening** - `a3-a4` security scope gets concrete guards and honest residual-risk docs.
- [ ] **Phase 9: Profile-native verification wiring** - Project profile verification commands run through worker execution.
- [ ] **Phase 10: Rollback safety net** - Backup tags, branch preflight, and explicit rollback CLI are operator-ready.
- [ ] **Phase 11: CI polling and bounded repair** - CI/PR feedback can create bounded auditable repair loops.
- [ ] **Phase 12: Governance and semantic-memory decision** - Risk policy and semantic-memory scope are explicit.

## Phase Details

### Phase 1: Operator pause parity
**Goal**: Make WUI and TUI expose the same worker pause/resume semantics, backed by shared global control state.
**Depends on**: Nothing (migrated from completed superpowers work)
**Requirements**: OPUI-01, OPUI-02, DOC-01, DOC-02, DOC-03
**Canonical refs**: `docs/superpowers/plans/2026-05-08-operator-ui-parity-and-global-pause.md`, `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
**Success Criteria** (what must be TRUE):
  1. Operator `p` pauses workers in both TUI and WUI.
  2. Operator `R` resumes workers in both TUI and WUI.
  3. WUI continues refreshing while workers are paused.
  4. Local and remote workers release or avoid work at safe pause checkpoints.
**Plans**: Completed outside GSD before migration

Plans:
- [x] 01-01: Add global control state schema, repository API, admin API, worker enforcement, WUI controls, and TUI controls.

### Phase 2: Shared review decision path
**Goal**: Route WUI/API and TUI human-review decisions through one shared command path.
**Depends on**: Phase 1
**Requirements**: OPUI-03
**Canonical refs**: `docs/superpowers/plans/2026-05-08-shared-review-decision-service.md`
**Success Criteria** (what must be TRUE):
  1. API and TUI use the same event type mapping for approval, rejection, and requested changes.
  2. API and TUI produce the same payload contract for shared fields.
  3. Existing admin auth and TUI reviewer validation remain intact.
**Plans**: Completed outside GSD before migration

Plans:
- [x] 02-01: Add shared review-decision command and wire API/TUI through it.

### Phase 3: WUI state-preserving refresh
**Goal**: Preserve local WUI operator state across manual refresh, HTMX refresh, and SSE fragment swaps.
**Depends on**: Phase 2
**Requirements**: OPUI-04
**Canonical refs**: `docs/superpowers/plans/2026-05-08-wui-state-preserving-refresh.md`
**Success Criteria** (what must be TRUE):
  1. Active surface is restored after full-body refresh.
  2. Filter text is restored and reapplied after refresh.
  3. Selected review row index is restored when still present.
  4. Dashboard input focus is restored for allowed operator inputs.
**Plans**: Completed outside GSD before migration

Plans:
- [x] 03-01: Add fail-soft browser state save/restore around WUI refresh and swap events.

### Phase 4: Compact operator identity panel
**Goal**: Move admin bearer and reviewer identity inputs into a compact panel while preserving existing behavior.
**Depends on**: Phase 3
**Requirements**: OPUI-05
**Canonical refs**: `docs/superpowers/plans/2026-05-08-compact-operator-identity-panel.md`
**Success Criteria** (what must be TRUE):
  1. Admin bearer and reviewer inputs keep their existing ids.
  2. Pause/resume and filter controls remain visible in the primary topbar.
  3. Focus restoration opens the identity panel before focusing hidden identity inputs.
**Plans**: Completed outside GSD before migration

Plans:
- [x] 04-01: Add compact operator identity details panel and static dashboard contract tests.

### Phase 5: Shared operator table contract
**Goal**: Define and apply a shared TUI/WUI table-column contract for operator surfaces.
**Depends on**: Phase 4
**Requirements**: OPUI-07
**Canonical refs**: `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`, `.planning/ROADMAP-ANALYSIS.md`
**Success Criteria** (what must be TRUE):
  1. Task table columns are named consistently or documented as intentional per-medium differences.
  2. Worker table ordering is consistent or intentionally medium-specific.
  3. Review queue and event table fields map to a shared operator contract.
  4. Contract tests pin labels and field mapping before mobile layout work begins.
**Plans**: Complete

Plans:
- [x] 05-01: Define shared operator table contract and align TUI/WUI where practical.

### Phase 6: Mobile WUI row actions
**Goal**: Make WUI tables usable on mobile without relying on cramped horizontal scrolling for critical row actions.
**Depends on**: Phase 5
**Requirements**: OPUI-06
**Canonical refs**: `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`, `.planning/ROADMAP-ANALYSIS.md`
**Success Criteria** (what must be TRUE):
  1. Mobile review rows expose details and actions in a touch-friendly layout.
  2. Task, worker, and event rows remain scannable below 900px width.
  3. Critical action buttons do not overlap row text or require precise horizontal scrolling.
  4. Existing desktop table behavior remains unchanged.
  5. Phase 5 table-contract labels are preserved or intentionally adapted for mobile display.
**Plans**: Ready to plan

Plans:
- [ ] 06-01: Design and implement mobile row-detail/action layout for WUI tables.

### Phase 7: Review action affordances
**Goal**: Make reject and request-changes actions clearer and safer without slowing expert hotkeys.
**Depends on**: Phase 6
**Requirements**: OPUI-08
**Canonical refs**: `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
**Success Criteria** (what must be TRUE):
  1. Review action controls communicate approve/reject/request-changes clearly.
  2. Destructive or blocking decisions have confirmation, undo, or stronger comment affordances.
  3. TUI and WUI hotkeys remain efficient for expert operators.
**Plans**: Ready to plan

Plans:
- [ ] 07-01: Improve review action labels, tooltips, prompts, and recovery affordances.

### Phase 8: Sandbox and secrets hardening
**Goal**: Implement `a3-a4-sandbox-and-secrets-lint` from `docs/CODEX-MISSION.md` without overclaiming full VM isolation.
**Depends on**: Phase 7
**Requirements**: SEC-01, SEC-02, SEC-03
**Canonical refs**: `docs/CODEX-MISSION.md`, `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`, `.planning/ROADMAP-ANALYSIS.md`
**Success Criteria** (what must be TRUE):
  1. Secret linting covers task descriptions, comments, config values, runner prompts, and external feedback.
  2. Runner environments use explicit allowlists plus configured required tokens.
  3. Blocked work emits auditable reasons.
  4. Docs clearly state residual sandbox risk.
**Plans**: Ready to plan

Plans:
- [ ] 08-01: Add sandbox/secrets hardening and update compliance evidence.

### Phase 9: Profile-native verification wiring
**Goal**: Wire `ProjectConfig.verification_commands` into generated plans and local/remote worker execution.
**Depends on**: Phase 8
**Requirements**: VER-01
**Canonical refs**: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`, `.planning/ROADMAP-ANALYSIS.md`
**Success Criteria** (what must be TRUE):
  1. Profile verification commands flow into worker execution without replacing explicit CLI verification commands.
  2. Required verification failures block normal `DONE`.
  3. Compliance report can distinguish profile-native verification from ad hoc CLI verification.
**Plans**: Ready to plan

Plans:
- [ ] 09-01: Wire profile verification commands into runtime and compliance evidence.

### Phase 10: Rollback safety net
**Goal**: Add explicit backup-tag, branch-protection preflight, and smart rollback CLI behavior.
**Depends on**: Phase 9
**Requirements**: ROLL-01, ROLL-02, ROLL-03
**Canonical refs**: `docs/CODEX-MISSION.md`, `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`
**Success Criteria** (what must be TRUE):
  1. Operators can create and list rollback points before risky branch mutation.
  2. Push/merge/restore preflight checks are explicit and auditable.
  3. Restore operations are confirmation-gated and do not silently destroy unrelated work.
**Plans**: Ready to plan

Plans:
- [ ] 10-01: Implement backup tag, branch preflight, and rollback CLI safety net.

### Phase 11: CI polling and bounded repair
**Goal**: Model `execute -> verify/CI -> repair attempt N -> verify/CI -> escalate` as an auditable loop.
**Depends on**: Phase 10
**Requirements**: CI-01, CI-02
**Canonical refs**: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`
**Success Criteria** (what must be TRUE):
  1. CI polling can run as a configured verification or sink stage.
  2. Repair attempts have explicit retry budgets and stop conditions.
  3. Escalation events make exhausted repair loops visible to operators.
**Plans**: Ready to plan

Plans:
- [ ] 11-01: Add CI polling and bounded repair-loop primitives.

### Phase 12: Governance and semantic-memory decision
**Goal**: Make governance policy and semantic-memory scope explicit in code and docs.
**Depends on**: Phase 11
**Requirements**: DOC-04, GOV-01, GOV-02
**Canonical refs**: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`, `docs/Current-vs-Target.md`
**Success Criteria** (what must be TRUE):
  1. Governance policy scores risk for migrations, auth, infra, dependencies, release actions, and external PR behavior.
  2. Semantic memory is either deterministic and evidence-backed or explicitly deferred.
  3. Compliance output and docs use the same current-vs-target wording.
**Plans**: Ready to plan

Plans:
- [ ] 12-01: Settle governance policy and semantic-memory target status.

## Progress

**Execution Order:**
Phases execute in numeric order. Phases 1-4 were completed through superpowers artifacts before
GSD initialization and are now tracked here as completed history.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Operator pause parity | 1/1 | Complete | 2026-05-08 |
| 2. Shared review decision path | 1/1 | Complete | 2026-05-08 |
| 3. WUI state-preserving refresh | 1/1 | Complete | 2026-05-08 |
| 4. Compact operator identity panel | 1/1 | Complete | 2026-05-08 |
| 5. Shared operator table contract | 1/1 | Complete | 2026-05-08 |
| 6. Mobile WUI row actions | 0/1 | Not started | - |
| 7. Review action affordances | 0/1 | Not started | - |
| 8. Sandbox and secrets hardening | 0/1 | Not started | - |
| 9. Profile-native verification wiring | 0/1 | Not started | - |
| 10. Rollback safety net | 0/1 | Not started | - |
| 11. CI polling and bounded repair | 0/1 | Not started | - |
| 12. Governance and semantic-memory decision | 0/1 | Not started | - |
