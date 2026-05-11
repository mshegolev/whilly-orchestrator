# Phase 9: Profile-native verification wiring - Research

**Researched:** 2026-05-08
**Domain:** Whilly project-config plan metadata, worker verification execution, and compliance evidence
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

## Implementation Decisions

### Command Sources And Precedence
- Profile-native verification commands should be generated from `ProjectConfig.verification_commands` into task/plan metadata that workers can execute.
- Explicit CLI verification commands remain supported and must not be silently replaced.
- When both profile-native and explicit CLI commands exist, execute the union in deterministic order: profile-native commands first, then explicit CLI commands.
- Required verification failures continue to block normal `DONE` and route through the existing `verification_failed` worker behavior.

### Runtime Wiring
- Local and remote workers should use the same verification command resolution semantics.
- Profile-native verification must reuse `whilly/pipeline/verification.py` result models, redaction, and event builders.
- Phase 8 secret lint and runner environment allowlist boundaries should remain intact; this phase should not bypass guard/redaction behavior when verification commands come from profile config.
- Generated plans should carry verification evidence clearly enough for compliance reporting to distinguish profile-native commands from ad hoc CLI commands.

### Compliance Evidence
- Compliance should report profile-native verification separately from explicit CLI verification support.
- Compliance wording must stay honest: this phase proves configured profile commands feed runtime verification, not that every project profile has exhaustive test coverage.
- Current-vs-target docs should only be updated if needed to align compliance evidence; avoid broad documentation rewrites.

### Claude's Discretion
- The exact internal representation of generated verification commands is at Claude's discretion as long as it is typed, testable, and does not break existing public CLI behavior.

### Deferred Ideas (OUT OF SCOPE)

## Deferred Ideas

- CI polling and bounded repair loops belong to Phase 11.
- Rollback behavior belongs to Phase 10.
- Governance and semantic-memory target status belong to Phase 12.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| VER-01 | Project-profile verification commands are wired into generated plans and worker execution. | Use top-level typed plan verification metadata from `ProjectConfig.verification_commands`, persist it through plan import/export, resolve it together with explicit CLI commands, and feed the existing local/remote worker `verification_runner` path. |
</phase_requirements>

## Summary

Phase 9 should not create a second verification system. The existing verification runner already handles command execution, required/optional semantics, event building, command scanning, timeout handling, environment allowlists, and output redaction in `whilly/pipeline/verification.py`. Local and remote workers already block `DONE` by failing tasks with `reason="verification_failed"` when a required verification result fails.

The missing link is the profile command data path. `ProjectConfig.verification_commands` is loaded and validated, but `whilly/project_config/plan_builder.py` does not emit it into generated plan JSON. Even if it did today, `whilly.adapters.filesystem.plan_io` would ignore the extra top-level key because the canonical `Plan` model has no verification field, and `whilly plan import` would not persist it. Remote workers are even stricter: `whilly-worker` currently builds a synthetic `Plan(id=plan_id, name=plan_id)` and receives only a `Task` from the control plane claim path, so profile commands must become persisted plan metadata that remote runtime can fetch or receive from the control plane.

**Primary recommendation:** add a typed plan-level `verification_commands` contract, persist it with imported plans, tag command source as `profile` or `cli`, and build one shared resolution helper that returns profile commands first and explicit CLI commands second.

## Standard Stack

### Core

| Library / Module | Version | Purpose | Why Standard |
|------------------|---------|---------|--------------|
| Python | `>=3.12` from `pyproject.toml` | Runtime and typing baseline | Repository target version; Ruff target is `py312`. |
| `dataclasses` value models | stdlib | `Plan`, `Task`, and recommended verification command value object | Existing core model style is immutable dataclasses, not Pydantic. |
| `asyncpg` / Postgres | `asyncpg>=0.29` | Plan/task/event persistence | Current worker and CLI runtime store plans in Postgres. |
| Alembic | `alembic>=1.13` | Schema migration for plan verification metadata | Existing database evolution path under `whilly/adapters/db/migrations/versions/`. |
| Pydantic | `>=2.6` | HTTP transport payloads for remote workers | Existing `whilly.adapters.transport.schemas` uses Pydantic wire DTOs. |
| `pytest` / `pytest-asyncio` | `pytest>=8.0`, `pytest-asyncio>=0.23` | Unit and async worker tests | Existing test framework in `pyproject.toml`. |
| Ruff | `0.11.5` | Format and lint | Repository-pinned formatter/linter. |

### Internal Components

| Component | Purpose | Required Phase 9 Role |
|-----------|---------|-----------------------|
| `whilly.project_config.loader` | Loads and validates `ProjectConfig.verification_commands`; blocks unsafe commands with `scan_command`. | Keep as the profile config validation boundary. |
| `whilly.project_config.plan_builder` | Generates canonical Whilly plan payloads from `ProjectConfig`. | Emit top-level plan verification metadata. |
| `whilly.adapters.filesystem.plan_io` | Canonical JSON `Plan`/`Task` parser and serializer. | Preserve verification metadata through parse/serialize. |
| `whilly.cli.plan` | Imports/exports plans to/from Postgres. | Persist and export plan verification metadata. |
| `whilly.cli.run` | Local worker composition root and explicit CLI verification flags. | Resolve `profile + cli` commands and build the verification runner. |
| `whilly.cli.worker` / `whilly.worker.remote` | Remote worker composition and loop. | Fetch or receive plan verification metadata and pass the same verification runner semantics. |
| `whilly.pipeline.verification` | Verification runner, result models, events, redaction, command policy. | Reuse; extend with source tagging rather than duplicating logic. |
| `whilly.compliance` | Deterministic repo inspection report. | Report profile-native wiring separately from ad hoc CLI verification. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Top-level typed plan metadata | Parse verification commands from `description` or `test_steps` | Avoid. It is brittle string parsing and bypasses the canonical plan model. |
| `plans.verification_commands JSONB` | New `plan_verification_commands` table | A table is more queryable, but JSONB is simpler and matches ordered plan-level JSON metadata. Use a table only if per-command querying becomes a requirement. |
| Fetch plan metadata once in remote CLI | Add full plan metadata to every claim response | Claim response already has optional `plan`, but fetching once is lower churn and avoids repeating stable plan metadata on every task claim. |
| New verification statuses | Reuse `FAILED` with `reason=verification_failed` | New statuses are out of scope and would disturb the deterministic state machine. |

## Architecture Patterns

### Recommended Project Structure

```text
whilly/
├── core/models.py                    # Add pure typed verification command value object and Plan field
├── project_config/plan_builder.py     # Emit profile verification commands into generated plans
├── adapters/filesystem/plan_io.py     # Parse and serialize top-level verification_commands
├── adapters/db/schema.sql             # Add plan verification metadata storage
├── adapters/db/migrations/versions/   # Add migration 015 for metadata persistence
├── cli/plan.py                        # Import/export plan verification metadata
├── cli/run.py                         # Resolve profile + CLI command union for local workers
├── cli/worker.py                      # Fetch profile commands for remote worker composition
├── worker/local.py                    # Existing verification failure path; extend source-aware details
├── worker/remote.py                   # Existing verification failure path; extend source-aware details
├── pipeline/verification.py           # Add source to specs/results/events and shared helpers
└── compliance/__init__.py             # Distinguish profile-native and CLI verification evidence
```

### Pattern 1: Typed Plan-level Verification Metadata

**What:** Make verification commands a first-class field on `Plan`, not an extra JSON key. Generate this field from `ProjectConfig.verification_commands`, parse it in `plan_io`, persist it through `whilly plan import`, and export it back.

**When to use:** Use for all profile-native commands. These commands are project/plan metadata, not task prose.

**Recommended shape:**

```python
# Source: repo-local pattern from whilly/core/models.py immutable dataclasses.
@dataclass(frozen=True)
class VerificationCommand:
    name: str
    command: str
    required: bool = True
    source: str = "profile"


@dataclass(frozen=True)
class Plan:
    id: PlanId
    name: str
    tasks: tuple[Task, ...] = ()
    origin: PlanOrigin | None = None
    repo_targets: tuple[RepoTarget, ...] = ()
    verification_commands: tuple[VerificationCommand, ...] = ()
```

Use a pure core value object or equivalent. Do not make `whilly.core` import `whilly.pipeline` or `whilly.project_config`; `.importlinter` protects core and worker import purity.

### Pattern 2: Deterministic Command Resolution

**What:** Resolve all command sources before constructing the worker `verification_runner`.

**When to use:** In local `whilly run` and remote worker composition.

**Recommended helper contract:**

```python
# Source: based on existing whilly/cli/run.py::_build_verification_specs and
# whilly/pipeline/verification.py::VerificationCommandSpec.
def resolve_verification_specs(
    *,
    profile_commands: Sequence[VerificationCommandLike],
    required_cli: Sequence[str] = (),
    optional_cli: Sequence[str] = (),
) -> tuple[VerificationCommandSpec, ...]:
    return (
        *profile_commands_as_specs_with_source_profile,
        *required_cli_specs_with_source_cli,
        *optional_cli_specs_with_source_cli,
    )
```

This preserves the locked precedence: profile-native first, explicit CLI second. It also preserves explicit CLI behavior when no profile commands exist.

### Pattern 3: One Runtime Path for Local and Remote Workers

**What:** Keep `run_local_worker` and `run_remote_worker` accepting a `verification_runner` callable. Build that callable in the composition roots from the resolved specs.

**Local path:** `_async_run()` already loads a full `Plan` from Postgres through `_select_plan_with_tasks()`, so it can use `plan.verification_commands` directly and append `--verify-command` / `--optional-verify-command` specs.

**Remote path:** `whilly-worker` currently constructs `Plan(id=plan_id, name=plan_id)` with no DB read. Add a control-plane metadata fetch before entering `run_remote_worker_with_heartbeat`, using the existing `/api/v1/plans/{plan_id}` surface expanded to include `verification_commands`. The remote worker entry must still avoid `asyncpg`, FastAPI, SQLAlchemy, and project-config imports.

### Pattern 4: Source-aware Verification Events

**What:** Add command source to verification specs, results, and event payloads.

**When to use:** Always. Existing events already carry `name`, redacted `command`, `required`, `succeeded`, `warning`, return code, duration, timeout, and block status. Add `source` so compliance and audit evidence can separate `profile` from `cli`.

**Example event payload additions:**

```python
payload = {
    "task_id": task_id,
    "plan_id": plan_id,
    "name": result.name,
    "source": result.source,  # "profile" or "cli"
    "command": redact_secrets(result.command),
    "required": result.required,
    "succeeded": result.succeeded,
}
```

### Anti-Patterns to Avoid

- **Parsing commands from task prose:** `plan_io` currently ignores unknown JSON keys by design; formalize the data contract instead of scraping descriptions or test steps.
- **Replacing CLI flags:** `--verify-command` and `--optional-verify-command` must still work. The phase adds profile commands to the command set; it does not switch the CLI path off.
- **New task statuses:** Required verification failure already routes to `FAILED` with `reason="verification_failed"`. Keep that path.
- **Remote worker importing server-only code:** `whilly.cli.worker`, `whilly.worker.remote`, and `whilly.adapters.transport.client` are covered by `worker-entry-purity`; do not import `asyncpg`, FastAPI, SQLAlchemy, Alembic, or project-config loader there.
- **Unredacted failure detail:** `_verification_failure_detail()` in both local and remote workers currently includes `result.command`. When adding source-aware details, redact commands there too or omit raw command from fail detail.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Shell execution and timeout handling | A new subprocess runner | `run_verification_commands()` | Already handles sequential execution, timeouts, process group kill, env allowlist, command policy, and output caps. |
| Verification event construction | Ad hoc event payloads | `make_verification_started_event()` and `make_verification_result_event()` | Existing event names and payload shape are already accepted by transport tests. |
| Required failure blocking | A new status or custom branch | Existing `verification_failed` worker branch | Local and remote workers already fail the task and avoid normal `DONE`. |
| Profile command validation | New unsafe-command regexes | `scan_command()` in loader and runtime verification | Phase 8 guard behavior stays centralized. |
| Secret redaction | String replacement in workers | `redact_secrets()` via verification event builders | Keeps Phase 8 redaction behavior consistent. |
| Remote metadata persistence | Local config reads on remote worker | Control-plane plan metadata | Remote worker has no project config file and must remain transport-only. |

**Key insight:** profile-native verification is a data plumbing phase, not a verification engine phase. The expensive edge cases are already covered by the verification runner and worker failure branches; the planner should focus on preserving typed metadata across generator, parser, persistence, transport, and composition roots.

## Common Pitfalls

### Pitfall 1: Plan Metadata Evaporates

**What goes wrong:** `plan_builder` emits `verification_commands`, but `parse_plan_dict()` ignores it, so `whilly plan import` persists no commands and workers never see them.

**Why it happens:** `plan_io` intentionally ignores extra JSON keys and `Plan` has no verification field.

**How to avoid:** Add `Plan.verification_commands`, parser/serializer support, import/export persistence, and tests that round-trip through JSON and Postgres.

**Warning signs:** generated JSON contains commands, but exported plan JSON or worker `Plan` does not.

### Pitfall 2: Local Works, Remote Does Not

**What goes wrong:** local `whilly run` uses plan metadata from Postgres, but `whilly-worker` still creates a synthetic empty `Plan`.

**Why it happens:** remote workers do not have DB access and `RemoteWorkerClient.claim()` currently returns only `Task`.

**How to avoid:** Expand the remote metadata path. Recommended: add verification commands to `/api/v1/plans/{plan_id}` and fetch once in `whilly.cli.worker` before the loop. Keep a typed transport schema or parser for the response.

**Warning signs:** unit tests pass for `run_remote_worker(..., verification_runner=stub)` but CLI/HTTP remote execution has no profile commands.

### Pitfall 3: CLI Commands Are Replaced Instead Of Unioned

**What goes wrong:** profile commands are present, but explicit `--verify-command` flags are skipped.

**Why it happens:** composition checks only one source to decide whether to create `verification_runner`.

**How to avoid:** build a single tuple from profile commands first and CLI commands second; create the runner when the tuple is non-empty.

**Warning signs:** test with both sources records only profile command names.

### Pitfall 4: Compliance Overclaims Verification Coverage

**What goes wrong:** compliance says verification is globally complete even though profile commands only prove configured commands flow into runtime.

**Why it happens:** current `_verification_status()` reports `PASS` for the CLI/worker path and the gap says profile-native wiring is future work.

**How to avoid:** update evidence wording to label `profile-native` and `explicit CLI` support separately. Do not claim every project has exhaustive tests.

**Warning signs:** report gap still says profile-native wiring is future work after implementation, or evidence does not mention command source.

### Pitfall 5: Source Tags Leak Or Drift

**What goes wrong:** verification events cannot distinguish `profile` from `cli`, or fail details leak raw commands.

**Why it happens:** `VerificationCommandResult` has no source field, and worker failure detail currently copies `result.command`.

**How to avoid:** add `source` to specs/results/events and use redacted commands in `_verification_failure_detail()`.

**Warning signs:** `verification.failed` payload has no `source`, or persisted fail detail contains a secret-like command string.

## Code Examples

Verified patterns from local sources:

### Current CLI-only Verification Builder

```python
# Source: whilly/cli/run.py
verification_specs = _build_verification_specs(
    required=verify_commands,
    optional=optional_verify_commands,
)
if verification_specs:
    async def verification_runner(task: Task):
        return await run_verification_commands(
            verification_specs,
            cwd=task_workspaces.get(task.id, Path.cwd()),
            timeout_s=verify_timeout,
            env_allowlist=_VERIFICATION_ENV_ALLOWLIST,
        )
```

### Current Worker Failure Branch

```python
# Source: whilly/worker/local.py and whilly/worker/remote.py
for verification_result in verification_outcome.results:
    await _record_pipeline_event(
        repo_or_client,
        make_verification_result_event(task.id, verification_result, plan_id=plan.id),
    )
if verification_outcome.required_failed:
    detail = _verification_failure_detail(verification_outcome)
    await fail_task_or_client_fail(task.id, task.version, "verification_failed", detail=detail)
```

### Recommended Source-aware Event Assertion

```python
payloads = [
    payload
    for _task_id, event_type, payload, _detail in repo.event_calls
    if event_type in {"verification.succeeded", "verification.failed", "verification.warning"}
]
assert [payload["source"] for payload in payloads] == ["profile", "cli"]
assert [payload["name"] for payload in payloads] == ["profile-unit", "cli-unit"]
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Agent success marker alone could imply `DONE`. | Required verification commands can block `DONE` when configured via CLI. | Already implemented before Phase 9. | Keep existing failure path. |
| Project config loads verification commands but generated plans do not carry them. | Phase 9 should make profile commands typed plan metadata. | Phase 9 target. | Enables generated plans and workers to share the same command source. |
| Compliance reports required verification as CLI/worker support with profile-native gap. | Compliance should distinguish profile-native and explicit CLI verification support. | Phase 9 target. | Avoids overclaiming while proving the new data path. |
| Remote worker uses a synthetic plan with no metadata. | Remote worker should fetch or receive plan verification metadata from control plane. | Phase 9 target. | Makes remote execution match local semantics. |

**Deprecated/outdated:**

- CLI-only verification as the whole story: keep it supported, but it is no longer sufficient evidence for project-profile verification.
- Profile command strings only in config objects: they must be emitted into generated plans and persisted.
- Raw command failure details: source-aware additions should redact or omit raw commands.

## Open Questions

1. **Should profile commands run after every successful task or only after leaf/verify stages?**
   - What we know: current explicit CLI verification runs after every successful agent task in the worker completion path.
   - What's unclear: `ProjectConfig.verification_commands` is project-level, not stage-specific.
   - Recommendation: match existing CLI semantics for Phase 9: run profile commands wherever explicit CLI commands would run. Stage-specific verification belongs to a future profile executor.

2. **Should storage be JSONB column or normalized table?**
   - What we know: commands are ordered plan-level metadata and are naturally represented in generated plan JSON.
   - What's unclear: future reporting might want SQL-level per-command queries.
   - Recommendation: use `plans.verification_commands JSONB NOT NULL DEFAULT '[]'::jsonb` for this phase. Switch to a table only if later requirements need command-level queries.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | `pytest>=8.0`, `pytest-asyncio>=0.23` |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`, `asyncio_mode = "auto"`, `testpaths = ["tests"]`) |
| Quick run command | `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/unit/test_cli_run.py tests/unit/test_verification_runner.py` |
| Full suite command | `make test` |
| Architecture guard | `.venv/bin/lint-imports --config .importlinter` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| VER-01 | `ProjectConfig.verification_commands` appears in generated plan JSON. | unit | `.venv/bin/python -m pytest -q tests/unit/test_project_config.py::test_project_config_plan_emits_profile_verification_commands -x` | Existing file, new test |
| VER-01 | Plan parser/serializer preserves `verification_commands`. | unit | `.venv/bin/python -m pytest -q tests/unit/test_plan_io.py::test_plan_verification_commands_round_trip -x` | Existing file, new test |
| VER-01 | Plan import/export persists verification commands. | integration | `.venv/bin/python -m pytest -q tests/integration/test_plan_io.py::test_import_export_preserves_profile_verification_commands -x` | Existing file, new test |
| VER-01 | Local runtime executes profile commands before CLI commands and keeps CLI commands. | unit | `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py::test_async_run_unions_profile_and_cli_verification_commands -x` | Existing file, new test |
| VER-01 | Required profile verification failure blocks `DONE`. | unit | `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py::test_required_verification_failure_blocks_complete_and_records_events -x` | Existing file, extend assertions |
| VER-01 | Remote runtime receives/executes profile commands. | unit | `.venv/bin/python -m pytest -q tests/unit/test_remote_worker.py::test_remote_required_verification_failure_blocks_complete_and_records_events -x` | Existing file, extend or add CLI composition test |
| VER-01 | Verification events distinguish profile and CLI command sources. | unit | `.venv/bin/python -m pytest -q tests/unit/test_verification_runner.py::test_verification_result_event_includes_source_and_redacts_command -x` | Existing file, new test |
| VER-01 | Compliance report distinguishes profile-native from explicit CLI verification. | unit | `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py::test_profile_native_verification_compliance_is_distinct_from_cli_verification -x` | Existing file, new test |

### Sampling Rate

- **Per task commit:** focused file(s), usually one of the commands above.
- **Per wave merge:** `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/unit/test_cli_run.py tests/unit/test_verification_runner.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1`
- **Phase gate:** `make test`, `make lint`, and `.venv/bin/lint-imports --config .importlinter` before `/gsd:verify-work`.

### Wave 0 Gaps

- [ ] `whilly.core.models` lacks a typed plan-level verification command field.
- [ ] `whilly.adapters.filesystem.plan_io` ignores top-level `verification_commands`.
- [ ] `whilly.cli.plan` and schema do not persist plan verification metadata.
- [ ] `whilly.cli.worker` has no remote plan metadata fetch carrying verification commands.
- [ ] `tests/integration/test_alembic_015_profile_verification_commands.py` or equivalent migration coverage is needed if a new `plans.verification_commands` column is added.
- [ ] Existing worker tests cover failure blocking but not source-aware profile-vs-CLI evidence.

## Recommended Plan Decomposition

1. **Data contract and generation**
   - Add pure typed verification command metadata to `Plan`.
   - Emit `verification_commands` from `build_plan_payload()` with `source="profile"`.
   - Parse/serialize it in `plan_io`.
   - Add unit tests in `test_project_config.py` and `test_plan_io.py`.

2. **Persistence and transport**
   - Add migration 015 and update `schema.sql`.
   - Persist metadata in `whilly.cli.plan` import/export.
   - Expand `/api/v1/plans/{plan_id}` and a transport client helper so remote CLI can fetch plan verification commands without importing server-only modules.
   - Add import/export and transport tests.

3. **Runtime resolution**
   - Extend `VerificationCommandSpec`/result/event with `source`.
   - Replace CLI-only `_build_verification_specs()` with a helper that returns profile specs first, CLI required specs second, CLI optional specs third.
   - Build local and remote `verification_runner` from the same resolved tuple.

4. **Worker failure detail and compliance**
   - Ensure local/remote failure detail redacts or omits raw command strings.
   - Extend worker assertions for `source`.
   - Update compliance probes/evidence to label profile-native and CLI verification separately and remove the stale future-work gap.

## Sources

### Primary (HIGH confidence)

- `.planning/phases/09-profile-native-verification-wiring/09-CONTEXT.md` - locked phase decisions.
- `.planning/REQUIREMENTS.md` - VER-01 requirement.
- `.planning/ROADMAP.md` - Phase 9 success criteria.
- `.planning/STATE.md` - Phase 8 completion and current constraints.
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md` - canonical Task 4 backlog.
- `AGENTS.md`, `CLAUDE.md`, `.importlinter`, `pyproject.toml`, `Makefile` - repo conventions and validation stack.
- `whilly/project_config/models.py`, `loader.py`, `plan_builder.py`, `cli/project_config.py`.
- `whilly/core/models.py`, `whilly/adapters/filesystem/plan_io.py`, `whilly/cli/plan.py`.
- `whilly/cli/run.py`, `whilly/cli/worker.py`, `whilly/worker/local.py`, `whilly/worker/remote.py`, `whilly/worker/main.py`.
- `whilly/pipeline/verification.py`, `whilly/pipeline/events.py`, `whilly/core/agent_runner.py`, `whilly/security/secret_lint.py`.
- `whilly/adapters/db/schema.sql`, migrations `013` and `014`.
- `whilly/adapters/transport/server.py`, `client.py`, `schemas.py`.
- `whilly/compliance/__init__.py`, `tests/unit/test_compliance_report.py`.
- `tests/unit/test_project_config.py`, `test_plan_io.py`, `test_cli_run.py`, `test_verification_runner.py`, `test_local_worker.py`, `test_remote_worker.py`, `test_transport_schemas.py`, `tests/integration/test_plan_io.py`.

### Secondary (MEDIUM confidence)

- `docs/Project-Config.md`, `docs/Current-vs-Target.md`, `README.md` - current public wording and scope boundaries.

### Tertiary (LOW confidence)

- None. No web or package-registry research was needed; the phase is repository-internal.

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - verified from `pyproject.toml`, `Makefile`, `.importlinter`, and existing code.
- Architecture: HIGH - traced actual generator, parser, DB import/export, local worker, remote worker, and compliance paths.
- Pitfalls: HIGH - based on observed current drops in the code path and existing worker tests.

**Research date:** 2026-05-08
**Valid until:** 2026-06-07, unless plan schema or worker transport changes first.

**What might be missed:** exact migration naming may shift if another migration lands before implementation; verify latest migration head immediately before writing Phase 9 code.
