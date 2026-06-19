---
phase: 22-orchestration-cluster
type: context
requirements: [ORCH-01, ORCH-02, ORCH-03, ORCH-04, ORCH-05, ORCH-06, ORCH-07]
source: orchestrator-authored (lean path — milestone decisions already locked)
---

# Phase 22 Context — Orchestration Cluster

## Goal

Capture the 7 load-bearing orchestration contracts as **normative, machine-checkable**
OpenSpec capability specs under `openspec/specs/<slug>/spec.md`. Each spec is
**reverse-spec'd from the real code** (descriptive-only specs are forbidden) and MUST
pass `openspec validate <slug> --strict` with zero errors/warnings.

## ⚠ ARCHITECTURE NOTE — code is v4.7.0, CLAUDE.md narrative is STALE v3

The codebase is **v4.7.0** (Postgres-backed, distributed worker-claim model). The
project `CLAUDE.md` and earlier planning prose describe the **v3 monolithic `run_plan`
loop**, which no longer exists. **Reverse-spec EVERY requirement from reading the actual
`whilly/` modules — do NOT trust CLAUDE.md / prose narrative for behavior.** Verified
v3→v4 drifts you MUST NOT re-assert as current behavior:

- ❌ No `run_plan` function and no `_original_cwd` / `chdir` anywhere. The v4 run path is
  the async worker-claim model in `whilly/cli/run.py` (`run_run_command` → `_async_run`).
  A separate `run_plan_command` exists in `whilly/cli/plan.py` and `whilly/cli/__init__.py`
  — read both to find the real iteration model before speccing ORCH-01.
- ❌ `validate_schema` lives in `whilly/cli/__init__.py` (NOT `task_manager.py`) and
  validates **every** task — there is NO "first 3 tasks only" limit.
- ❌ `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` are **removed no-ops since v4 (TASK-107)**
  (`whilly/config.py:93-94`). Do NOT gate any behavior on them. Find the real v4 trigger
  for `WorktreeManager` by reading the code (it may not be wired into the live run path).
- ✅ Still grounded in v4 (verified): `orchestrator.plan_batches` / `plan_batches_llm`
  (key_files-overlap batching); `result_parser.AgentResult` (usage/exit_code/is_complete)
  + `COMPLETION_MARKER = "<promise>COMPLETE</promise>"`; tmux dispatch with
  `whilly-{task_id}` session naming; `WorktreeManager.merge_back` (cherry-pick) /
  `cleanup` against `.whilly_worktrees`; `task-model-fsm` symbols (the Phase 21 exemplar
  is correctly v4-grounded). The v4 plan workspace is `workspaces.RepoTargetWorkspaceResolver`
  (`.whilly_workspaces/repos/<repo>/<plan>/<task>`), NOT `.whilly_workspaces/{slug}/`.

## Scope: 6 specs to write (task-model-fsm already done)

`task-model-fsm` (ORCH-02) was authored in Phase 21 as the reference exemplar and
already passes strict validation — **do NOT rewrite it**. This phase writes the
remaining 6:

| Req | Slug | Primary source modules to reverse-spec from |
|-----|------|----------------------------------------------|
| ORCH-01 | `orchestration-loop` | `whilly/cli/run.py` (`run_run_command`/`_async_run`), `whilly/cli/plan.py` (`run_plan_command`), `whilly/core/{models,gates,governance,task_id}.py`, `whilly/llm_ops.py`, `whilly/llm_otel.py` |
| ORCH-03 | `plan-json-contract` | `whilly/adapters/filesystem/plan_io.py` (`parse_plan`/`parse_plan_dict`/`serialize_plan`, `Plan`/`RepoTarget`/`PlanOrigin`/`VerificationCommand`), `whilly/cli/__init__.py` (`validate_schema` — validates ALL tasks), `whilly/task_manager.py` (`Task` dataclass: required no-default fields `id, phase, category, priority, description, status`) |
| ORCH-04 | `batch-planning` | `whilly/orchestrator.py` (`plan_batches`, `plan_batches_llm`) |
| ORCH-05 | `agent-dispatch` | `whilly/tmux_runner.py`, `whilly/agent_runner.py`, `whilly/core/agent_runner.py`, `whilly/adapters/runner/*`, `whilly/agents/*`, `whilly/worker/*` |
| ORCH-06 | `worktree-isolation` | `whilly/worktree_runner.py`, `whilly/workspaces.py` |
| ORCH-07 | `result-collection` | `whilly/adapters/runner/result_parser.py`, `AgentResult` |

(Module → capability assignments are authoritative in `openspec/COVERAGE-MATRIX.md`.)

## Locked decisions (milestone v1.3, 2026-06-13)

- **Capability = subsystem**, not one spec per module. One `spec.md` per slug.
- **Normative & testable**: every requirement body line contains `SHALL`/`MUST`;
  every requirement has ≥1 `#### Scenario:` with `WHEN`/`THEN` (`AND` optional).
  Avoid should/may/might/can — they are not testable.
- **Forward delta-only role**: OpenSpec = living WHAT, GSD = HOW. These specs
  describe current *guaranteed* behavior, not aspirational behavior.
- **Format authority**: `openspec/AUTHORING.md` (rules) + `openspec/specs/task-model-fsm/spec.md`
  (worked exemplar — copy its structure: `## Purpose` ≥50 chars, then `## Requirements`
  with `### Requirement:` blocks, each with `#### Scenario:` bullets).
- **Validation gate**: `openspec validate <slug> --strict` must pass (openspec 1.4.1).

## Per-capability scope notes (boundaries) — verify each against v4 code

- **orchestration-loop**: spec the REAL v4 iteration model by reading
  `cli/run.py::_async_run` and `cli/plan.py::run_plan_command` — the worker-claim /
  dispatch / collect / persist flow against Postgres. Do NOT spec `run_plan` or an
  `_original_cwd` chdir invariant (neither exists in v4). Do NOT re-specify the FSM
  (task-model-fsm) or budget thresholds (Phase 27) — reference, don't duplicate.
- **plan-json-contract**: required task fields per the actual `Task` dataclass
  (no-default: `id, phase, category, priority, description, status`; plus defaulted
  `dependencies, key_files, acceptance_criteria, test_steps`, etc.), the plan envelope
  and v4 extensions (`PlanOrigin`, `RepoTarget`, `VerificationCommand`), atomic writes,
  and round-trip tolerance. `validate_schema` (in `cli/__init__.py`) validates **all**
  tasks — spec that, NOT a first-3 limit.
- **batch-planning**: non-overlapping `key_files` grouping via `orchestrator.plan_batches`
  (LLM variant `plan_batches_llm` falls back to `plan_batches` on error). Tasks sharing
  `key_files` cannot run in parallel.
- **agent-dispatch**: tmux vs subprocess runner selection (tmux session `whilly-{task_id}`,
  grounded). Derive the per-task `WorktreeManager` trigger from the actual code — do NOT
  assert `WHILLY_WORKTREE=1` as the gate (it is a removed no-op).
- **worktree-isolation**: the v4 plan workspace is `RepoTargetWorkspaceResolver`
  (`.whilly_workspaces/repos/<repo>/<plan>/<task>`). The per-task worktree lifecycle in
  `worktree_runner.WorktreeManager` — create → `merge_back` (cherry-pick) → `cleanup`
  under `.whilly_worktrees` — is grounded; spec it from the class, and state its real
  activation condition (read the code; do not invent an env-flag gate).
- **result-collection**: `AgentResult(usage, exit_code, is_complete)` from the Claude CLI
  JSON via `result_parser`; `is_complete` is set iff `COMPLETION_MARKER`
  (`<promise>COMPLETE</promise>`) is present in the agent output.

## Out of scope (defer to later phases)

- PRD pipeline / Decision Gate (Phase 23), integrations (24), operator surface (25),
  platform/config/auth (26), safety/budget/verification (27).
- Any `whilly/` Python changes. **Documentation only.**

## Success criteria (from ROADMAP)

1. Each of the 6 remaining capabilities has a normative `spec.md`.
2. `plan-json-contract` enumerates required task fields + round-trip tolerance matching `task_manager.py`.
3. `task-model-fsm` already specifies legal transitions (done in Phase 21).
4. Each spec has ≥1 testable `#### Scenario:` and all pass `openspec validate --strict`.
5. Every module these capabilities cover is checked in the coverage matrix.
