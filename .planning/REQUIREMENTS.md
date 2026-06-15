# Requirements: Whilly Orchestrator — v1.3 OpenSpec Project Baseline

**Defined:** 2026-06-13
**Core Value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.

**Milestone goal:** Capture Whilly's current *guaranteed* behavior as a complete set of normative,
testable OpenSpec capability specs (`openspec/specs/<capability>/spec.md`), with a module→capability
coverage matrix proving all 242 modules are accounted for. After this baseline, all behavior changes
flow through `opsx` proposals (forward delta-only); GSD continues to own milestone execution.

**Decisions locked (questioning gate, 2026-06-13):**
- Scope: full reverse-spec — *full module coverage*, organized as ~30 subsystem-level capabilities.
- Posture: normative & testable (MUST/SHALL language + scenarios), not descriptive snapshots.
- Future role: forward delta-only — OpenSpec is the living WHAT, GSD remains the HOW.
- Granularity: capability = subsystem, with a coverage matrix (not one spec per module).

## v1.3 Requirements

### Baseline & Taxonomy (Phase 21)

- [ ] **BASE-01**: A capability taxonomy of ~30 capabilities is defined under `openspec/specs/`,
  with a documented naming convention and one-line purpose per capability.
- [ ] **BASE-02**: A `module → capability` coverage matrix exists mapping every one of the 242
  `whilly/` modules to exactly one capability (zero unmapped, zero double-mapped).
- [ ] **BASE-03**: Spec authoring conventions are documented (MUST/SHALL normative language, the
  requirement + `#### Scenario:` format OpenSpec validates, testability bar).
- [ ] **BASE-04**: `openspec/project.md` (or `config.yaml` context) carries Whilly's tech stack,
  conventions, and domain glossary so generated specs are consistent.

### Orchestration cluster (Phase 22)

- [x] **ORCH-01**: `orchestration-loop` — the v4 worker-claim iteration model (composition root
  → claim → start → dispatch → route → idle/terminate) is specified normatively with scenarios.
  (Done 22-01: openspec/specs/orchestration-loop/spec.md, validates --strict.)
- [x] **ORCH-02**: `task-model-fsm` — task status state machine (`pending → in_progress →
  done | failed | skipped`) and legal transitions are specified.
  (Done 21-03: openspec/specs/task-model-fsm/spec.md reference exemplar, validates --strict.)
- [x] **ORCH-03**: `plan-json-contract` — required task fields and plan envelope
  (`project`, `prd_file`, `tasks[]`) and round-trip tolerance are specified.
  (Done 22-02: openspec/specs/plan-json-contract/spec.md, validates --strict.)
- [x] **ORCH-04**: `batch-planning` — non-overlapping `key_files` batching and first-batch dispatch
  re-evaluation rule are specified.
  (Done 22-04: openspec/specs/batch-planning/spec.md, validates --strict.)
- [x] **ORCH-05**: `agent-dispatch` — tmux vs subprocess runner selection and per-task isolation
  preconditions are specified.
  (Done 22-03: openspec/specs/agent-dispatch/spec.md, validates --strict.)
- [x] **ORCH-06**: `worktree-isolation` — plan workspace and per-task worktree lifecycle
  (create → cherry-pick on done → cleanup) is specified.
  (Done 22-04: openspec/specs/worktree-isolation/spec.md, validates --strict.)
- [x] **ORCH-07**: `result-collection` — `AgentResult` parsing and the `<promise>COMPLETE</promise>`
  completion signal are specified.
  (Done 22-02: openspec/specs/result-collection/spec.md, validates --strict.)

### PRD pipeline & decision (Phase 23)

- [x] **PRD-01**: `prd-generation` — non-interactive PRD synthesis is specified.
  (Done 23-01: openspec/specs/prd-generation/spec.md, validates --strict.)
- [x] **PRD-02**: `prd-wizard` — interactive PRD authoring via Claude CLI is specified.
  (Done 23-02: openspec/specs/prd-wizard/spec.md, validates --strict.)
- [x] **PRD-03**: `task-generation` — PRD → `tasks.json` generation contract is specified.
  (Done 23-01: openspec/specs/task-generation/spec.md, validates --strict.)
- [x] **PRD-04**: `decomposition` — mid-run splitting of oversized tasks every `DECOMPOSE_EVERY`
  is specified. (Done 23-02: openspec/specs/decomposition/spec.md, validates --strict;
  spec states the legacy/unwired v4 worker-claim status truthfully.)
- [x] **PRD-05**: `decision-gate` — the Decision Gate + TRIZ contradiction analysis refusal/accept
  criteria are specified.

### Integrations cluster (Phase 24)

- [ ] **INT-01**: `jira-integration` — Jira read/work-snapshot behavior and auth expectations are
  specified.
- [ ] **INT-02**: `gitlab-integration` — GitLab CLI surface behavior is specified.
- [ ] **INT-03**: `github-integration` — GitHub PR/projects/converter behavior is specified.
- [ ] **INT-04**: `jira-watcher-daemon` — the watch loop daemon (phase 20) behavior and guarantees
  are specified.
- [ ] **INT-05**: `notifications` — Slack/sink notification dispatch is specified.
- [ ] **INT-06**: `mcp-integration` — MCP server/client integration surface is specified.

### Operator surface cluster (Phase 25)

- [ ] **OPS-01**: `dashboard-tui` — Rich Live dashboard states and hotkeys are specified.
- [ ] **OPS-02**: `web-status-ui` — web status/API surface behavior is specified.
- [ ] **OPS-03**: `reporting` — per-iteration JSON + end-of-run Markdown reporting is specified.
- [ ] **OPS-04**: `cli-surface` — CLI flags, headless JSON output, and exit codes
  (`0/1/2/3`) are specified.
- [ ] **OPS-05**: `operator-views-logs` — operator views and log viewer behavior are specified.

### Platform cluster (Phase 26)

- [ ] **PLAT-01**: `configuration` — `WhillyConfig.from_env()` env-var contract and defaults are
  specified.
- [ ] **PLAT-02**: `auth-security` — session auth, gated password change, flag-gated OIDC/WebAuthn,
  and the task-id path-traversal sink class mitigation are specified.
- [ ] **PLAT-03**: `scheduling` — scheduler behavior is specified.
- [ ] **PLAT-04**: `state-persistence` — `StateStore` resume contract (plan/iteration/cost/sessions)
  is specified.
- [ ] **PLAT-05**: `self-update-doctor` — update, doctor, repair, rollback behaviors are specified.

### Safety & quality cluster (Phase 27)

- [ ] **SAFE-01**: `budget-resource-guards` — budget thresholds (80% warn / 100% kill→exit 2) and
  resource monitoring are specified.
- [ ] **SAFE-02**: `recovery-self-healing` — deadlock detection, stall pause, retry/backoff, and
  self-healing are specified.
- [ ] **SAFE-03**: `quality-compliance-audit` — quality/compliance/audit-event behavior is
  specified.
- [ ] **SAFE-04**: `verification-gates` — verifier and human-review gate behavior is specified.

### Forward process, coverage & validation (Phase 28)

- [ ] **FWD-01**: The forward delta-only workflow is documented — future behavior changes require an
  `opsx` proposal that updates the relevant capability spec.
- [ ] **FWD-02**: `CLAUDE.md` and `AGENTS.md` are updated to require spec deltas for behavior changes
  and to point contributors at `openspec/specs/`.
- [ ] **COV-01**: The coverage matrix (BASE-02) is verified at 100% — every module mapped, audited
  against the final capability set.
- [ ] **VAL-01**: `openspec validate --strict` passes for all capability specs.
- [ ] **VAL-02**: Every capability spec has been peer/UAT reviewed for normative accuracy against the
  code it describes (no descriptive-only specs).

## Out of Scope

| Item | Reason |
|------|--------|
| One spec file per module (242 specs) | Contradicts normative+testable posture; most modules are internal helpers with no external contract. Covered via the matrix instead. |
| Rewriting / refactoring any `whilly/` code | This milestone is spec capture only. Behavior changes belong to later milestones via `opsx` deltas. |
| Migrating GSD planning into OpenSpec | Role decision is forward delta-only; GSD keeps owning execution. No planning-tool migration. |
| External/domain research | Reverse-speccing existing code needs no new market/domain research. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| BASE-01..04 | Phase 21 | Done (taxonomy, AUTHORING, project.md, coverage matrix; exemplar) |
| ORCH-01..07 | Phase 22 | Done (7 specs authored, all pass openspec validate --strict) |
| PRD-01, PRD-03 | Phase 23 | Done (prd-generation + task-generation specs, both pass openspec validate --strict) |
| PRD-02, PRD-04 | Phase 23 | Done (prd-wizard + decomposition specs, both pass openspec validate --strict) |
| PRD-05 | Phase 23 | Complete |
| INT-01..06 | Phase 24 | Pending |
| OPS-01..05 | Phase 25 | Pending |
| PLAT-01..05 | Phase 26 | Pending |
| SAFE-01..04 | Phase 27 | Pending |
| FWD-01..02, COV-01, VAL-01..02 | Phase 28 | Pending |

**Coverage:**
- v1.3 requirements: 41 total
- Mapped to phases: 41
- Unmapped: 0

---
*Requirements defined: 2026-06-13*
*Last updated: 2026-06-13 after /gsd-new-milestone questioning gate*
