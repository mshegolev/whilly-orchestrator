# Documentation Pack Alignment Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this roadmap task-by-task.

**Goal:** Bring the current Whilly repository into honest alignment with the
documentation pack in `/Users/m.v.shchegolev/Downloads/whilly_orchestrator_documentation_pack.zip`.

**Architecture:** Preserve the deterministic task state machine. Add project
profiles, verification, human review, and configured sinks as opt-in layers around
the existing worker/repository/control-plane paths.

**Tech Stack:** Python 3.12, asyncpg/Postgres, FastAPI, Pydantic v2, pytest,
Ruff, import-linter.

---

## Current Alignment Summary

Overall status: **PARTIAL**.

Whilly is correctly positioned as an AI-assisted orchestration control plane,
not a fully autonomous developer. Core queueing, state transitions, sources,
workers, guards, events, metrics, dashboard, PR helper paths, and repo-target
metadata exist. Project-config tasks now have an audit-event runtime overlay,
and configured verification commands can block `DONE`. The main remaining gaps
are profile-native verification wiring, human review approval/enforcement,
configured sinks, bounded repair, and governance policy.

## Capability Matrix

| Capability from archive | Current state | Evidence | Gap |
|---|---|---|---|
| Control-plane positioning | Mostly aligned | `README.md`, `docs/Project-Description.md` | Some stale docs still describe removed v3 workspace behavior. |
| JSON plan import/run | Implemented | `whilly/cli/plan.py`, `whilly/cli/run.py` | Preserve. |
| GitHub/Jira/Forge intake | Implemented | `whilly/sources/*`, `whilly/forge/intake.py` | Validate docs and nondeterminism warning. |
| Deterministic task state | Implemented | `whilly/core/state_machine.py`, `TaskStatus` | Preserve; do not add risky statuses yet. |
| Decision gates | Implemented | `whilly/core/gates.py`, `plan apply --strict` | Preserve. |
| Prompt and shell guards | Implemented | `whilly/core/prompts.py`, `whilly/core/agent_runner.py` | Preserve, extend to profile verification commands. |
| Project profiles | Partial | `whilly/project_config/*`, `whilly/cli/project_config.py` | Current shape differs from PRD: dataclasses, TOML/JSON, missing `python_backend` and `documentation`, not wired into worker runtime. |
| Built-in profiles | Partial | `presets.py` supports `etl`, `graphql_api`, `feature_development`, `generic` | PRD expects `python_backend`, `graphql_api`, `etl_pipeline`, `documentation`, `generic`. |
| Profile validation | Partial | Loader checks duplicate steps, unknown dependencies/repo roles | Missing runner/source/sink validation, required stage ids, unsafe command policy, contradictory human-review checks. |
| Configurable pipeline stages | Implemented MVP | `whilly/pipeline/events.py`, local/remote workers | Stage lifecycle is audit-event based; no dedicated profile executor yet. |
| Required verification before DONE | Implemented when configured | `whilly/pipeline/verification.py`, `whilly run --verify-command` | Profile-native verification command wiring remains future work. |
| Human review checkpoints | Partial | `whilly/pipeline/human_review.py`, event endpoint allowlist | Checkpoint events exist; no full approval capture/enforcement/dashboard queue yet. |
| PR creation as configured sink | Partial | `WHILLY_AUTO_OPEN_PR=1` post-complete hook | Env-gated hook, not project-profile sink/stage. |
| PR review feedback loop | Partial/future | `whilly pr-feedback poll` one-shot poller | Not automatic repair loop; keep documented as future. |
| Multi-repo execution | Partial/future | `repo_targets`, `task_repo_targets`, `whilly/workspaces.py` | Per-task repo workspace exists, but full multi-repo orchestration is not current product guarantee. |
| Compliance report generation | Missing | No `compliance` CLI/report module | Add report command matching archive guide. |

## Roadmap

### Phase 0: Documentation Baseline And Vocabulary

**Files:**
- Create: `docs/target/01_BRD.md`, `docs/target/02_PRD.md`, `docs/target/03_ADR.md`, `docs/target/04_Compliance_Validation_Guide.md`
- Modify: `README.md`, `docs/index.md`, `docs/Project-Description.md`, `docs/Project-Config.md`

- [ ] Import the archive docs into a stable `docs/target/` location or link them from there.
- [ ] Replace stale “v4.6.1 baseline” and migration-chain text with the current post-013 schema state.
- [ ] Remove or clearly mark v3/worktree documents as historical, especially `docs/documents/task-execution-phases.md`.
- [ ] Fix human-loop wording: current `TaskStatus` has no `BLOCKED` or `HUMAN_LOOP`; describe these as planned checkpoint events unless implemented.
- [ ] Add a “Current vs Target” page that says Whilly is currently between Level 1 and Level 2 from the archive roadmap.

Validation:

```bash
rg -n "fully autonomous|DONE always means verified|automatic PR review feedback|BLOCKED|HUMAN_LOOP|full multi-repo|semantic long-term memory" README.md docs/
.venv/bin/python -m ruff check whilly/ tests/
```

### Phase 1: Compliance Validation Command

**Files:**
- Create: `whilly/compliance/__init__.py`, `whilly/compliance/models.py`, `whilly/compliance/collector.py`, `whilly/compliance/report.py`
- Create: `whilly/cli/compliance.py`
- Modify: `whilly/cli/__init__.py`
- Test: `tests/unit/test_compliance_report.py`

- [ ] Add `CapabilityStatus = PASS | PARTIAL | FAIL | UNKNOWN`.
- [ ] Encode the archive capability matrix as data, with evidence probes for known local modules and runtime wiring.
- [ ] Add `whilly compliance report --format markdown|json --out PATH`.
- [ ] Include repository commit, date, and detected version in every report.
- [ ] Add tests that prove “helper exists but not wired” becomes `PARTIAL`, not `PASS`.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
```

### Phase 2: Project Profile Schema Consolidation

**Files:**
- Modify/Create: `whilly/project_config/models.py`, `whilly/project_config/loader.py`, `whilly/project_config/presets.py`, `whilly/project_config/validator.py`
- Modify: `whilly/cli/project_config.py`, `docs/Project-Config.md`
- Test: `tests/unit/test_project_config.py`

- [ ] Decide the public name: use the archive’s `ProjectProfile` terminology while keeping `ProjectConfig` as a compatibility alias.
- [ ] Move profile models to Pydantic v2 or add strict validation equivalent to Pydantic.
- [ ] Support the PRD project types: `python_backend`, `graphql_api`, `etl_pipeline`, `documentation`, `generic`.
- [ ] Keep aliases for current values: `etl -> etl_pipeline`, `feature_development -> python_backend` or document why not.
- [ ] Support YAML if `PyYAML` is accepted as a dependency; otherwise update target docs to say JSON/TOML are the implementation contract.
- [ ] Validate source types, sink types, runner names, required stage ids, human-review contradictions, and unsafe verification commands.

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

**Files:**
- Create: `whilly/pipeline/human_review.py`
- Modify: `whilly/adapters/transport/server.py`, `whilly/api/templates/index.html.j2`, `whilly/cli/plan.py`
- Test: `tests/unit/test_human_review_checkpoint.py`, `tests/integration/test_dashboard_human_review.py`

- [x] Add checkpoint events: `human_review.required`, `human_review.approved`, `human_review.rejected`, `human_review.changes_requested`.
- [ ] Surface tasks/checkpoints needing human input in plan show, API, and dashboard.
- [x] Keep approval as auditable data, not a new terminal task state for MVP.
- [ ] Block configured risky sinks/stages until approval evidence exists.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_human_review_checkpoint.py tests/integration/test_transport_tasks.py::test_record_task_event_accepts_pipeline_verification_and_human_review_events
```

### Phase 6: Configured Sinks And PR Policy

**Files:**
- Create: `whilly/pipeline/sinks.py`
- Modify: `whilly/sinks/post_complete_pr_hook.py`, `whilly/cli/run.py`, `whilly/project_config/models.py`
- Test: `tests/unit/test_configured_sinks.py`, `tests/integration/test_pr_sink_profile.py`

- [ ] Move PR creation from env-only behavior toward a profile sink/stage.
- [ ] Keep `WHILLY_AUTO_OPEN_PR=1` as backwards-compatible opt-in.
- [ ] Require human review or explicit profile approval before externally visible PR/release actions when configured.
- [ ] Document PR review feedback as manual one-shot polling until bounded repair is implemented.

Validation:

```bash
.venv/bin/python -m pytest -q tests/unit/test_configured_sinks.py tests/integration/test_pr_sink_profile.py
```

### Phase 7: Autonomous Developer v0 Preparation

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

Phases 0-5 now have an MVP path in the repository. The next cut should finish
the remaining Phase 5 workflow surface (approval capture, dashboard/API queue,
and blocking policy) before moving PR sinks and bounded repair into Phases 6-7.
