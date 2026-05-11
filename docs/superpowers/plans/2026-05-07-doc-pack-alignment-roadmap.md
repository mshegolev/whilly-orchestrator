# Documentation Pack Alignment Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this roadmap task-by-task.

**Goal:** Bring the current Whilly repository into honest alignment with the
documentation pack in `/Users/m.v.shchegolev/Downloads/whilly_orchestrator_documentation_pack.zip`.

**Architecture:** Preserve the deterministic task state machine. Add project
profiles, verification, human review, and configured sinks as opt-in layers around
the existing worker/repository/control-plane paths.

**Tech Stack:** Python 3.12, asyncpg/Postgres, FastAPI, project-config
dataclasses with strict loader validation, pytest, Ruff, import-linter.

---

## Current Alignment Summary

Current compliance status: **FAIL**, because the target pack still contains
future capabilities that are not implemented or are only partial.

Fresh evidence command:

```bash
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
.venv/bin/python -m whilly compliance report --format json --out out/compliance-report.json
```

Latest refreshed report at the time of this roadmap update:

- Repository commit: `5c601b3`
- Date: `2026-05-08`
- Ignored local evidence files: `out/compliance-report.md`,
  `out/compliance-report.json`

Whilly is correctly positioned as an AI-assisted orchestration control plane,
not a fully autonomous developer. Core queueing, state transitions, sources,
workers, guards, events, metrics, dashboard, PR helper paths, repo-target
metadata, project profiles, project-config plan generation, stage audit events,
required verification commands, human-review approval/rejection/change-request
controls in the web dashboard and TUI, and configured PR sink stages now exist.

The remaining capability gaps are:

1. Automatic PR creation remains opt-in runtime behavior, not default behavior.
2. PR review feedback remains polling-based, not an automatic repair loop.
3. Multi-repo execution has metadata/workspace support but no full planner.
4. Sandbox/VM isolation is not enforced per task.
5. Semantic memory is not implemented in this repository slice.
6. Git rollback exists only as verifier-helper behavior, not a general smart
   rollback system.
7. Profile-native verification wiring is still a gap even though ad hoc
   required verification commands can block `DONE`.

## Capability Matrix

| Capability from archive | Current state | Evidence | Gap |
|---|---|---|---|
| Control-plane positioning | Mostly aligned | `README.md`, `docs/Project-Description.md` | Some stale docs still describe removed v3 workspace behavior. |
| JSON plan import/run | Implemented | `whilly/cli/plan.py`, `whilly/cli/run.py` | Preserve. |
| GitHub/Jira/Forge intake | Implemented | `whilly/sources/*`, `whilly/forge/intake.py` | Validate docs and nondeterminism warning. |
| Deterministic task state | Implemented | `whilly/core/state_machine.py`, `TaskStatus` | Preserve; do not add risky statuses yet. |
| Decision gates | Implemented | `whilly/core/gates.py`, `plan apply --strict` | Preserve. |
| Prompt and shell guards | Implemented | `whilly/core/prompts.py`, `whilly/core/agent_runner.py` | Preserve, extend to profile verification commands. |
| Project profiles | Implemented MVP | `whilly/project_config/*`, `whilly/cli/project_config.py` | Uses dataclasses plus strict validation, not Pydantic. Preserve compatibility. |
| Built-in profiles | Implemented MVP | `presets.py` supports `python_backend`, `etl_pipeline`, `documentation`, `graphql_api`, `generic`; aliases map `etl` and `feature_development`. | Preserve aliases and documented public names. |
| Profile validation | Implemented MVP | Loader validates source/sink/runner names, dependencies, repo roles, human-loop contradictions, and unsafe verification commands. | Profile-native runtime wiring remains separate. |
| Configurable pipeline stages | Implemented MVP | `whilly/pipeline/events.py`, local/remote workers | Stage lifecycle is audit-event based; no dedicated profile executor yet. |
| Required verification before DONE | Implemented when configured | `whilly/pipeline/verification.py`, `whilly run --verify-command` | Profile-native verification command wiring remains future work. |
| Human review checkpoints | Implemented | `whilly/pipeline/human_review.py`, admin human-review API, release-hold enforcement, dashboard/TUI operator controls | Keep reviewer identity and admin-token handling documented. |
| PR creation as configured sink | Partial | `WHILLY_AUTO_OPEN_PR=1`, `whilly/pipeline/sinks.py`, post-complete PR hook | Opt-in and credential-dependent; do not claim unconditional behavior. |
| PR review feedback loop | Partial/future | `whilly pr-feedback poll` one-shot poller | Not automatic repair loop; keep documented as future. |
| Multi-repo execution | Partial/future | `repo_targets`, `task_repo_targets`, `whilly/workspaces.py` | Per-task repo workspace exists, but full multi-repo orchestration is not current product guarantee. |
| Compliance report generation | Implemented | `whilly/compliance/__init__.py`, `whilly/cli/compliance.py`, `tests/unit/test_compliance_report.py` | Fix false-positive documentation mismatch detection. |
| Sandbox/VM isolation | Partial/future | Command guards and restricted runner flags | Add per-task isolation or keep documented as residual risk. |
| Semantic memory | Missing/future | No deterministic runtime module | Decide whether to implement or explicitly keep out of current target. |
| Smart rollback | Partial/future | Verifier helper can revert on verification failure | Add general backup-tag/restore/preflight flow before claiming robust rollback. |

## Roadmap

### Current Task Decomposition: Plan > Act > Verify

This is the active execution backlog. Each task is intentionally small enough
to plan, implement, verify, and commit independently.

#### Task 1: Refresh Compliance Roadmap Evidence

**Files:**
- Modify: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`
- Generated evidence, not committed: `out/compliance-report.md`,
  `out/compliance-report.json`

**Plan**

- Regenerate markdown and JSON compliance reports from the current checkout.
- Make the roadmap point at the report command as the repeatable evidence
  source.
- Mark stale roadmap claims where code already exists.

**Act**

- Update this roadmap summary and capability matrix.
- Preserve the target/current boundary: do not turn future autonomous-developer
  capabilities into current claims.

**Verify**

```bash
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
.venv/bin/python -m whilly compliance report --format json --out out/compliance-report.json
git diff --check
```

Expected evidence: reports are written, roadmap diff has no whitespace errors.

#### Task 2: Fix Compliance Documentation-Mismatch False Positives

**Files:**
- Modify: `whilly/compliance/__init__.py`
- Test: `tests/unit/test_compliance_report.py`

**Plan**

- Inspect the README mismatch detector that currently reports phrases inside
  negative statements as claims.
- Add a regression test with text like "Do not describe current Whilly as full
  sandbox or VM isolation, semantic long-term memory, or reliable git rollback."
- Keep positive claims flagged, for example "Whilly provides full sandbox/VM
  isolation."

**Act**

- Make mismatch detection context-aware enough to distinguish explicit
  non-goals from capability claims.
- Keep the report conservative: ambiguous marketing claims should still be
  flagged.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
rg -n "Documentation Mismatches|claims full sandbox|claims semantic" out/compliance-report.md
```

Expected evidence: tests pass, and the report no longer flags README non-goal
sentences as positive claims.

#### Task 3: Human Review Approval Workflow Hardening

Status: **implemented and pushed in `5c601b3`**.

**Files:**
- Modify: `whilly/pipeline/human_review.py`
- Modify: `whilly/adapters/transport/server.py`
- Modify: `whilly/api/templates/index.html.j2`
- Modify: `whilly/dashboard.py`
- Test: `tests/unit/test_human_review_checkpoint.py`
- Test: `tests/integration/test_htmx_dashboard.py`
- Test: `tests/integration/test_transport_tasks.py`

**Plan**

- Treat existing approval capture, worker hold/release enforcement, and
  dashboard gap projection as implemented evidence.
- Define only the remaining operator hardening: approval controls, rejection or
  change-request controls, dashboard/TUI affordances, and compliance probe
  alignment.
- Keep approval as auditable event data, not a new `TaskStatus`.
- Require existing admin/bearer auth for mutation endpoints.

**Act**

- Added dashboard/TUI controls for approval, rejection, and change-request
  actions.
- Updated compliance probes so API capture, release-hold enforcement, and
  operator controls are reported as implemented.
- Kept configured risky stages/sinks blocked until matching approval evidence
  exists.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_human_review_checkpoint.py
.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py tests/integration/test_transport_tasks.py --maxfail=1
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: compliance reports `Human review checkpoint model` as
`PASS`, with dashboard/TUI operator-control evidence.

#### Task 4: Profile-Native Verification Wiring

**Files:**
- Modify: `whilly/project_config/plan_builder.py`
- Modify: `whilly/cli/run.py`
- Modify: `whilly/worker/local.py`
- Modify: `whilly/worker/remote.py`
- Test: `tests/unit/test_project_config.py`
- Test: `tests/unit/test_verification_runner.py`
- Test: `tests/unit/test_local_worker.py`
- Test: `tests/unit/test_remote_worker.py`

**Plan**

- Map `ProjectConfig.verification_commands` into runtime verification settings
  for generated plans.
- Preserve the existing explicit `whilly run --verify-command` behavior.
- Define precedence when both profile commands and CLI commands are present.

**Act**

- Thread generated profile verification commands into local and remote worker
  execution.
- Emit the same `verification.*` events used by explicit commands.
- Keep required failures blocking `DONE`.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_verification_runner.py
.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: the "Required verification before DONE" row can say profile
verification wiring exists, not only ad hoc CLI verification.

#### Task 5: Sandbox And Secrets Hardening (`a3-a4`)

**Files:**
- Create or modify: `whilly/security/secret_lint.py`
- Modify: `whilly/security/prompt_sanitizer.py`
- Modify: `whilly/core/agent_runner.py`
- Modify: `whilly/worker/local.py`
- Modify: `whilly/worker/remote.py`
- Test: `tests/unit/test_prompt_sanitizer.py`
- Test: `tests/unit/test_prompt_sanitizer_wiring.py`
- Test: `tests/unit/test_local_worker.py`
- Test: `tests/unit/test_remote_worker.py`

**Plan**

- Define the minimum v6 hardening contract: secret lint, environment allowlist,
  command deny-list coverage, and clear residual sandbox risk.
- Do not claim full VM/container isolation unless an actual isolation backend is
  wired into worker execution.

**Act**

- Add or extend secret linting for task descriptions, comments, config values,
  runner prompts, and external issue/PR feedback.
- Scrub runner environments to an explicit allowlist plus required configured
  tokens.
- Ensure blocked tasks emit auditable reasons.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py
.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: sandbox/security rows describe concrete guards and residual
risk without overclaiming VM isolation.

#### Task 6: Backup Tag, Branch Protection, And Smart Rollback

**Files:**
- Create or modify: `whilly/rollback/`
- Create or modify: `whilly/cli/rollback.py`
- Modify: `whilly/cli/__init__.py`
- Test: `tests/unit/test_rollback.py`
- Test: `tests/integration/test_rollback_cli.py`

**Plan**

- Implement rollback as an explicit operator safety-net, not an automatic
  destructive cleanup.
- Add backup tags before risky branch mutation.
- Add branch protection/preflight checks before push, merge, or restore.

**Act**

- Add commands for creating a backup tag, listing rollback points, and restoring
  a worktree/branch with operator confirmation.
- Store rollback evidence in audit/report output.
- Keep destructive operations opt-in.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_rollback.py
.venv/bin/python -m pytest -q tests/integration/test_rollback_cli.py --maxfail=1
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: the Git rollback row can move beyond verifier-helper
behavior.

#### Task 7: CI Polling And Bounded Repair Loop

**Files:**
- Create or modify: `whilly/ci/`
- Create or modify: `whilly/repair/`
- Modify: `whilly/cli/pr_feedback.py`
- Modify: `whilly/sources/github_pr_feedback.py`
- Modify: `whilly/cli/run.py`
- Test: `tests/unit/test_ci_polling.py`
- Test: `tests/unit/test_repair_loop.py`

**Plan**

- Model `execute -> verify/CI -> repair attempt N -> verify/CI -> escalate`.
- Set explicit retry budgets and stop conditions.
- Reuse PR feedback polling as an input, not as an always-on hidden loop.

**Act**

- Add CI status polling as a configured verification/sink stage.
- Add repair task creation from verification/CI/PR feedback failure evidence.
- Emit repair attempt events and escalation events.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: PR review feedback loop and CI verification can be described
as bounded and auditable.

#### Task 8: Multi-Repo Boundary Or Full Planner Decision

**Files:**
- Modify: `docs/Current-vs-Target.md`
- Modify: `docs/Project-Config.md`
- Modify if implementing: `whilly/workspaces.py`, `whilly/project_config/plan_builder.py`
- Test if implementing: `tests/unit/test_project_config.py`

**Plan**

- Decide whether the next milestone implements true cross-repo scheduling or
  keeps multi-repo as repo-target metadata plus per-task workspace preparation.
- Do not leave docs ambiguous.

**Act**

- If deferring: document the boundary and keep compliance row `PARTIAL`.
- If implementing: add dependency-aware cross-repo task planning, workspace
  mapping, and integration verification.

**Verify**

```bash
rg -n "full multi-repo|multi-repo execution|repo-target" README.md docs/
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: docs and compliance use the same multi-repo wording.

#### Task 9: Governance Policy And Semantic Memory Decision

**Files:**
- Create or modify: `whilly/governance/`
- Modify: `docs/target/06_Autonomous_Developer_Roadmap.md`
- Modify: `docs/Current-vs-Target.md`
- Modify: `docs/target/04_Compliance_Validation_Guide.md`
- Test if implementing code: `tests/unit/test_governance_policy.py`

**Plan**

- Decide whether semantic memory is a current target, a future target, or a
  non-goal for this milestone.
- Define governance/risk categories for migrations, auth, infra, dependencies,
  release actions, and externally visible PR/release behavior.

**Act**

- If semantic memory is deferred, change compliance target wording so missing
  semantic memory is not a current failure.
- If implemented, use deterministic event/PR/task history first; avoid opaque
  semantic recall as an authority source.
- Add risk scoring and approval requirements for high-risk actions.

**Verify**

```bash
.venv/bin/python -m pytest -q tests/unit/test_governance_policy.py
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

Expected evidence: compliance no longer fails on an ambiguous future-only
semantic-memory row, and governance policy is explicit.

### Phase 0: Documentation Baseline And Vocabulary

Status: **mostly complete; keep as documentation hygiene**.

**Files:**
- Create: `docs/target/01_BRD.md`, `docs/target/02_PRD.md`, `docs/target/03_ADR.md`, `docs/target/04_Compliance_Validation_Guide.md`
- Modify: `README.md`, `docs/index.md`, `docs/Project-Description.md`, `docs/Project-Config.md`

- [x] Import the archive docs into a stable `docs/target/` location or link them from there.
- [x] Add a “Current vs Target” page that says Whilly is currently between Level 1 and Level 2 from the archive roadmap.
- [x] Fix human-loop wording: current `TaskStatus` has no `BLOCKED` or `HUMAN_LOOP`; describe these as checkpoint events/evidence unless implemented as states.
- [ ] Replace any remaining stale “v4.6.1 baseline” and migration-chain text with the current post-013 schema state where it appears in current docs.
- [ ] Remove or clearly mark v3/worktree documents as historical, especially `docs/documents/task-execution-phases.md`.
- [ ] Fix compliance mismatch detection so negative non-goal wording is not reported as a positive README claim.

Validation:

```bash
rg -n "fully autonomous|DONE always means verified|automatic PR review feedback|BLOCKED|HUMAN_LOOP|full multi-repo|semantic long-term memory" README.md docs/
.venv/bin/python -m ruff check whilly/ tests/
```

### Phase 1: Compliance Validation Command

Status: **implemented; next action is detector quality**.

**Files:**
- Create: `whilly/compliance/__init__.py`, `whilly/compliance/models.py`, `whilly/compliance/collector.py`, `whilly/compliance/report.py`
- Create: `whilly/cli/compliance.py`
- Modify: `whilly/cli/__init__.py`
- Test: `tests/unit/test_compliance_report.py`

- [x] Add `CapabilityStatus = PASS | PARTIAL | FAIL | UNKNOWN`.
- [x] Encode the archive capability matrix as data, with evidence probes for known local modules and runtime wiring.
- [x] Add `whilly compliance report --format markdown|json --out PATH`.
- [x] Include repository commit, date, and detected version in every report.
- [x] Add tests that prove “helper exists but not wired” becomes `PARTIAL`, not `PASS`.
- [ ] Add tests for negative/non-goal documentation wording so the mismatch detector does not report false positives.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

### Phase 2: Project Profile Schema Consolidation

Status: **implemented MVP; Pydantic is not required for the current contract**.

**Files:**
- Modify/Create: `whilly/project_config/models.py`, `whilly/project_config/loader.py`, `whilly/project_config/presets.py`, `whilly/project_config/validator.py`
- Modify: `whilly/cli/project_config.py`, `docs/Project-Config.md`
- Test: `tests/unit/test_project_config.py`

- [x] Decide the public name: document target profile shape while keeping `ProjectConfig` as the implementation API.
- [x] Add strict validation equivalent to Pydantic for the current JSON/TOML contract.
- [x] Support the PRD project types: `python_backend`, `graphql_api`, `etl_pipeline`, `documentation`, `generic`.
- [x] Keep aliases for current values: `etl -> etl_pipeline`, `feature_development -> python_backend`.
- [x] Keep YAML out of the current implementation contract; JSON/TOML are the supported formats.
- [x] Validate source types, sink types, runner names, required stage ids, human-review contradictions, and unsafe verification commands.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_project_config.py
.venv/bin/python -m whilly project-config validate examples/project-config-etl.toml
```

### Phase 3: Runtime Pipeline Stage Events

**Files:**
- Create: `whilly/pipeline/__init__.py`, `whilly/pipeline/models.py`, `whilly/pipeline/executor.py`, `whilly/pipeline/events.py`
- Modify: `whilly/adapters/db/repository.py`, `whilly/worker/local.py`, `whilly/worker/remote.py`
- Test: `tests/unit/test_pipeline_events.py`, `tests/integration/test_pipeline_runtime_events.py`

- [x] Represent stage lifecycle as audit events first: `pipeline.stage.started`, `pipeline.stage.succeeded`, `pipeline.stage.failed`, `pipeline.stage.skipped`.
- [x] Store profile/stage snapshot in event payloads rather than changing `TaskStatus`.
- [x] Wire stage start/success/failure around local and remote worker execution.
- [x] Keep legacy plans profile-free and emit no stage events unless a profile is attached.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_pipeline_events.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py
```

### Phase 4: Verification Before Full Success

**Files:**
- Create: `whilly/pipeline/verification.py`
- Modify: `whilly/worker/local.py`, `whilly/worker/remote.py`, `whilly/cli/run.py`
- Test: `tests/unit/test_verification_runner.py`, `tests/integration/test_worker_verification.py`

- [x] Add a verification command runner with cwd, timeout, env allowlist, output capture, and shell-command policy scanning.
- [x] Emit `verification.started`, `verification.succeeded`, `verification.failed`, and `verification.warning`.
- [x] Required verification failure must prevent the task from being reported as fully successful; MVP can mark task `FAILED` with reason `verification_failed` or complete plus emit `VERIFICATION_FAILED` only if the reporting layer distinguishes it.
- [x] Optional verification failure should keep the task terminal path but emit warning evidence.
- [x] Add one test where runner success plus required verification failure does not become normal `DONE`.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_verification_runner.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py
```

### Phase 5: Human Review Checkpoint Model

Status: **implemented; approval workflow is event-backed and exposed in WUI/TUI**.

**Files:**
- Create: `whilly/pipeline/human_review.py`
- Modify: `whilly/adapters/transport/server.py`, `whilly/api/templates/index.html.j2`, `whilly/cli/plan.py`
- Test: `tests/unit/test_human_review_checkpoint.py`, `tests/integration/test_htmx_dashboard.py`

- [x] Add checkpoint events: `human_review.required`, `human_review.approved`, `human_review.rejected`, `human_review.changes_requested`.
- [x] Surface tasks/checkpoints needing human input in the API, dashboard, and browserless TUI.
- [x] Add plan-show checkpoint markers from task/event evidence.
- [x] Keep approval as auditable data, not a new terminal task state for MVP.
- [x] Block configured risky sinks/stages until approval evidence exists.
- [x] Complete dashboard/API/TUI approval queue hardening so compliance can move from `PARTIAL` to `PASS`.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_human_review_checkpoint.py tests/unit/test_local_worker.py::test_configured_pipeline_task_records_stage_and_human_review_events tests/unit/test_local_worker.py::test_configured_pipeline_task_completes_after_human_review_approval tests/unit/test_remote_worker.py::test_configured_remote_pipeline_task_records_stage_and_human_review_events tests/unit/test_remote_worker.py::test_configured_remote_pipeline_task_completes_after_human_review_approval tests/unit/test_remote_client.py::test_list_task_events_gets_filtered_events tests/integration/test_transport_tasks.py::test_record_task_event_accepts_pipeline_verification_and_human_review_events tests/integration/test_transport_tasks.py::test_human_review_release_holds_task_until_admin_approval tests/integration/test_per_worker_auth.py::test_cross_worker_bearer_on_task_events_returns_403
```

### Phase 6: Configured Sinks And PR Policy

Status: **implemented MVP; remains opt-in and credential-dependent**.

**Files:**
- Create: `whilly/pipeline/sinks.py`
- Modify: `whilly/sinks/post_complete_pr_hook.py`, `whilly/cli/run.py`, `whilly/project_config/models.py`
- Test: `tests/unit/test_configured_sinks.py`, `tests/integration/test_pr_sink_profile.py`

- [x] Move PR creation from env-only behavior toward a profile sink/stage.
- [x] Keep `WHILLY_AUTO_OPEN_PR=1` as backwards-compatible opt-in.
- [x] Require human review or explicit profile approval before externally visible PR/release actions when configured.
- [x] Document PR review feedback as manual one-shot polling until bounded repair is implemented.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_configured_sinks.py tests/integration/test_pr_sink_profile.py
```

### Phase 7: Autonomous Developer v0 Preparation

Status: **next major capability wave**.

**Files:**
- Create: `whilly/repair/`, `whilly/ci/`, `whilly/governance/`
- Modify after prior phases only.

- [ ] Add bounded repair attempts after verification/CI failure.
- [ ] Add CI polling as a configured verification/sink stage.
- [ ] Add risk scoring and approval policy for migrations, auth, infra, dependency upgrades, and release actions.
- [ ] Keep auto-merge and production release out of scope; require human approval.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit tests/integration --maxfail=3
.venv/bin/python -m ruff check whilly/ tests/
.venv/bin/lint-imports --config .importlinter
```

## Recommended First Cut

Use the task decomposition above as the active backlog.

Recommended order:

1. Task 5: implement `a3-a4-sandbox-and-secrets-lint` from `docs/CODEX-MISSION.md`.
2. Task 4: wire profile-native verification commands into runtime.
3. Task 6: implement backup tag, branch protection preflight, and smart rollback CLI.
4. Task 7: add CI polling and bounded repair.
5. Task 9: settle governance and semantic-memory target status.
