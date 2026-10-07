# Proposal: Opt-in durable local swarm (`whilly swarm`)

## Why

Operators want one persistent conversation that plans and runs bounded,
verified agent work across several local Git projects (development,
telemetry, evaluation, analysis, visualization) without a second queue, shared
working copies, or unverified "success". Whilly already has a PostgreSQL task
queue with optimistic transitions; the swarm reuses it.

## What Changes

- **ADDED** `orchestration-loop` requirements for the local swarm runtime:
  validated JSON registry; durable conversation with revisioned, validated,
  atomically applied plans; a single leased coordinator per session that
  claims through `TaskRepository` with a session tag and dependency gating;
  per-attempt worktrees from the recorded base SHA; bounded agent processes
  with process-group timeout/cancel; coordinator-run verification plus
  independent read-only review before `DONE`; bounded crash recovery; addressed
  peer messages; status/report without merge or push.
- **ADDED** `cli-surface` requirement for the `whilly swarm` command family:
  13 top-level commands, including the nested `registry validate` leaf, and
  exit codes 0/1/2/4.
- **ADDED** `state-persistence` extension for swarm-specific metadata tables
  via migrations `029_swarm_runtime` and `030_swarm_agent_calls`; call costs
  are nullable when usage is unknown.
- **ADDED** `configuration` requirement for the JSON registry schema
  (projects, roles, limits, agent config), validation, and environment
  defaults.

## Non-goals

- Replacing the Whilly queue, the HTTP control plane, or the shared board.
- Automatic push, merge, deployment, or production data export.
- OS-level sandboxing beyond the selected engine's controls.
- Token-level interruption of a running agent by new messages.
- Multi-host coordinators for one session (worktrees and process groups are local).

## Capabilities Affected

The following 32-capability taxonomy slugs have their coverage impacted:

| Slug | Impact | Module(s) |
|------|--------|-----------|
| `orchestration-loop` | EXTENDED | New `whilly/swarm/plan.py`, `runtime.py`; new coordinator execution model, dependency gating, lease-based mutual exclusion. |
| `cli-surface` | EXTENDED | New `whilly/cli/swarm.py`; 13 top-level commands, 14 executable leaves, exit codes 0/1/2/4. |
| `state-persistence` | EXTENDED | Migrations `029_swarm_runtime` and `030_swarm_agent_calls`; durable calls and nullable unknown costs. |
| `configuration` | EXTENDED | Registry projects, roles, limits, agent defaults, dual-engine configuration, and task/role engine overrides. |
| `agent-dispatch` | EXTENDED | Bounded Claude/Codex subprocesses with engine-specific parsing and restrictions. |
| `result-collection` | EXTENDED | New `whilly/swarm/prompts.py`; structured worker/reviewer output parsing and validation. |
| `auth-security` | EXTENDED | Admin permission guard resolves the canonical session email and preserves fail-closed uniqueness behavior. |

## Impact

- **Code**: 10 new modules under `whilly/swarm/`:
  - `__init__.py` — package docstring
  - `registry.py` — project/role registry and validation
  - `plan.py` — plan contract and validation
  - `prompts.py` — planner/worker/reviewer prompt builders
  - `agent.py` — bounded subprocess execution (process groups, timeouts)
  - `engines.py` — Claude/Codex argv and structured-output adapters
  - `gitops.py` — worktree creation, SHAs, diffs
  - `context.py` — execution context helpers
  - `store.py` — swarm-specific PostgreSQL persistence, call reservations, and atomic acceptance
  - `runtime.py` — conversation service and coordinator
  - Plus `whilly/cli/swarm.py` and lazy dispatch in `whilly/cli/__init__.py`.

- **Database**: Migrations `029_swarm_runtime` and `030_swarm_agent_calls` (030 also makes attempt cost nullable).
  - `swarm_sessions` — conversation state, applied revision, coordinator lease
  - `swarm_plan_revisions` — revisioned plans (proposed/invalid/applied/superseded)
  - `swarm_messages` — chat and peer messages with durable IDs and acknowledgement
  - `swarm_task_context` — per-task project/role/verification context
  - `swarm_task_attempts` — execution attempts with worktree, branch, result, review, cost
  - `swarm_agent_calls` — durable engine-call reservations, nullable cost, usage, and errors

- **Tests**:
  - `tests/unit/test_swarm_registry.py` — registry validation
  - `tests/unit/test_swarm_agent_process.py` — subprocess lifecycle
  - `tests/unit/test_swarm_plan.py` — plan contract
  - `tests/integration/test_swarm_runtime.py` — real PostgreSQL, real Git worktrees, injected deterministic agent
  - `tests/integration/test_alembic_full_chain.py` — migration chain
  - `tests/swarm_helpers.py` — test fixtures and utilities

- **Docs**:
  - `docs/Local-Swarm.md` — concise public user guide
  - `openspec/COVERAGE-MATRIX.md` — current module rows and live counts
  - This proposal (expanded)

## Compatibility

- **Existing Whilly commands**: No changes. `whilly run`, `whilly plan`, etc. remain unaffected.
- **Existing task queue**: Swarm tasks use Whilly's `plans` and `tasks` tables, but with session-scoped tags (`swarm:SESSION_ID`) and dependency gating tags (`swarm:waiting`). Existing generic workers will not claim swarm tasks.
- **Database migrations**: 030 adds a call table and relaxes attempt cost to allow unknown usage.
- **CLI exit codes**: New 0/1/2/4 scheme is swarm-specific; other `whilly` commands unaffected.

## Verification

Implementation and documentation remain uncommitted. BrowserQA found and
covered the seeded-admin email guard regression: 3 red unit tests were added,
the canonical session-email lookup was restored, router tests (17) and
transport tests (22) passed, and the integrated UI/server path is mounted.
PostgreSQL runtime and atomic/budget/mailbox checks passed. A live synthetic
Claude-to-Codex dependency chain passed, with bidirectional messages,
coordinator-owned Codex commit, real verification and independent review.
Browser login, discussion, plan generation, resume and persistence after
restart passed. Two final-review process-launch crash gaps were reproduced,
fixed and independently rechecked. No business task, production evaluation,
merge or deployment was performed.
