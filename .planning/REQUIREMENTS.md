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

- [x] **INT-01**: `jira-integration` — Jira read/work-snapshot behavior and auth expectations are
  specified.
- [x] **INT-02**: `gitlab-integration` — GitLab CLI surface behavior is specified.
- [x] **INT-03**: `github-integration` — GitHub PR/projects/converter behavior is specified.
- [x] **INT-04**: `jira-watcher-daemon` — the watch loop daemon (phase 20) behavior and guarantees
  are specified.
- [x] **INT-05**: `notifications` — Slack/sink notification dispatch is specified.
- [x] **INT-06**: `mcp-integration` — MCP server/client integration surface is specified.

### Operator surface cluster (Phase 25)

- [x] **OPS-01**: `dashboard-tui` — Rich Live dashboard states and hotkeys are specified.
  (Done 25-01: openspec/specs/dashboard-tui/spec.md, validates --strict.)
- [x] **OPS-02**: `web-status-ui` — web status/API surface behavior is specified.
  (Done 25-02: openspec/specs/web-status-ui/spec.md, validates --strict; reverse-spec'd
  from the FastAPI control plane + worker HTTP transport + SSE + localhost web status,
  with the transport bootstrap/per-worker-bearer auth split and the read-only vs mutating
  boundary; references auth-security for the full session/OIDC/WebAuthn model.)
- [x] **OPS-03**: `reporting` — per-iteration JSON + end-of-run Markdown reporting is specified.
  (Done 25-01: openspec/specs/reporting/spec.md, validates --strict; spec records the
  legacy/unwired v4 worker-claim status of Reporter/generate_summary truthfully.)
- [x] **OPS-04**: `cli-surface` — CLI flags, headless behavior, and the real v4
  exit-code contract (EXIT_OK=0, EXIT_VALIDATION_ERROR=1, EXIT_ENVIRONMENT_ERROR=2,
  WORKSPACE_FAILED_EXIT_CODE=-4) are specified. (Done 25-03:
  openspec/specs/cli-surface/spec.md, validates --strict; pins real EXIT_* constants
  not legacy 0/1/2/3 lore; no-args prints HELP, unknown command returns 2; v3 flag
  shim + WHILLY_HEADLESS handling captured.)
- [x] **OPS-05**: `operator-views-logs` — operator views and log viewer behavior are specified.
  (Done 25-03: openspec/specs/operator-views-logs/spec.md, validates --strict;
  whilly logs list/show/tail + cleanup, operator-views taxonomy + hotkeys + route
  prefixes + artifact inventory, and the operator TUI hotkey state machine.)

### Platform cluster (Phase 26)

- [x] **PLAT-01**: `configuration` — `WhillyConfig.from_env()` env-var contract and defaults are
  specified. (Done 26-01: openspec/specs/configuration/spec.md, validates --strict; reverse-spec'd
  from whilly/config.py — env-var contract + defaults, five-layer precedence, _coerce typing,
  env:/keyring:/file: secret schemes, project-config surface, and truthful no-op state fields.)
- [x] **PLAT-02**: `auth-security` — session auth, gated password change, flag-gated OIDC/WebAuthn,
  and the task-id path-traversal sink class mitigation are specified. (Plan 26-02 — wrote
  openspec/specs/auth-security/spec.md: 16 requirements at subsystem altitude covering session
  auth, lockout, forced password-change gate, flag-gated OIDC/WebAuthn/TOTP, CSRF, rate-limit,
  route+auth audit, prod-mode, dashboard SSE bearer, secrets/secret-lint/prompt-sanitizer, and the
  ADR-001 validate_task_id path-sink mitigation. Passes openspec validate auth-security --strict.)
- [x] **PLAT-03**: `scheduling` — scheduler behavior is specified.
- [x] **PLAT-04**: `state-persistence` — the v4 Postgres persistence layer (asyncpg pool,
  optimistic-locked TaskRepository on `version`, events audit, Alembic migrations 001–028) is
  specified as primary; the v3 `StateStore` / `.whilly_state.json` resume path is marked
  legacy/no-op. Passes openspec validate state-persistence --strict.
- [x] **PLAT-05**: `self-update-doctor` — update, doctor, repair, rollback behaviors are
  specified. Reverse-spec'd from real v4 code (update.py/cli/update.py, doctor.py,
  repair/*, rollback/*); 10 requirements covering non-mutating update check, explicit
  install, fail-closed auto policy, read-only doctor, ghost/stale plan classification,
  bounded repair decide/escalate, repair task + audit events, rollback point creation,
  refusal-first preflight, and confirmed/dry-run restore. Passes openspec validate
  self-update-doctor --strict.

### Safety & quality cluster (Phase 27)

- [x] **SAFE-01**: `budget-resource-guards` — ResourceMonitor CPU/mem/disk/process/log-dir
  thresholds + the Postgres `plan.budget_exceeded` sentinel and secret-free smoke exit codes
  are specified (v3 budget→exit-2 lore superseded).
- [x] **SAFE-02**: `recovery-self-healing` — file-based recovery + self_healing excepthook are
  specified and marked legacy/unwired, pointing to the live `release_stale_tasks` sweep.
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
| INT-01, INT-04 | Phase 24 | Done (jira-integration + jira-watcher-daemon specs, both pass openspec validate --strict) |
| INT-03 | Phase 24 | Done (github-integration subsystem spec, passes openspec validate --strict) |
| INT-02, INT-05, INT-06 | Phase 24 | Done (gitlab-integration + notifications + mcp-integration specs, all pass openspec validate --strict) |
| OPS-01, OPS-03 | Phase 25 | Done (dashboard-tui + reporting specs, both pass openspec validate --strict) |
| OPS-02 | Phase 25 | Done (web-status-ui subsystem spec, passes openspec validate --strict) |
| OPS-04, OPS-05 | Phase 25 | Done (cli-surface + operator-views-logs specs, both pass openspec validate --strict) |
| PLAT-01 | Phase 26 | Done (configuration spec authored, passes openspec validate --strict) |
| PLAT-02 | Phase 26 | Done (auth-security spec authored, passes openspec validate --strict) |
| PLAT-03 | Phase 26 | Done (scheduling spec authored, passes openspec validate --strict) |
| PLAT-04 | Phase 26 | Done (state-persistence spec authored — Postgres layer primary, StateStore legacy/no-op; passes openspec validate --strict) |
| PLAT-05 | Phase 26 | Done (self-update-doctor spec authored — update/doctor/repair/rollback; passes openspec validate --strict) |
| SAFE-01 | Phase 27 | Done (budget-resource-guards spec authored — ResourceMonitor thresholds + plan.budget_exceeded sentinel; passes openspec validate --strict) |
| SAFE-02 | Phase 27 | Done (recovery-self-healing spec authored — legacy/unwired, live path = release_stale_tasks; passes openspec validate --strict) |
| SAFE-03..04 | Phase 27 | Pending |
| FWD-01..02, COV-01, VAL-01..02 | Phase 28 | Pending |

**Coverage:**
- v1.3 requirements: 41 total
- Mapped to phases: 41
- Unmapped: 0

---
*Requirements defined: 2026-06-13*
*Last updated: 2026-06-13 after /gsd-new-milestone questioning gate*
