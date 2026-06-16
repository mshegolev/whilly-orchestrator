# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Whilly is a Python implementation of the **Whilly Wiggum loop** — Ralph Wiggum's smarter brother in Ghuntley's technique lineage. Ralph picks a task, tries, shouts "I'm helping!", repeats. Whilly does the same but with TRIZ contradiction analysis, a Decision Gate that refuses nonsense tasks upfront, and a PRD wizard that understands the problem first.

**As of v4, Whilly is a Postgres-backed, horizontally-scalable worker cluster** (not the v3 single-process loop). Plans and tasks live in Postgres; one or more **workers** claim tasks from the queue, run a Claude CLI agent per task, and write results back with optimistic locking. Ships two console scripts: `whilly` (`whilly.cli:main`, full CLI + v3-compat shim) and `whilly-worker` (`whilly.cli.worker:main`, a slim remote-worker binary that needs only httpx + pydantic — no asyncpg). Version lives in `whilly/__init__.py` and `pyproject.toml` (keep them in sync; currently 4.7.0).

Requires **Python 3.12+** (`requires-python = ">=3.12"`). Most task-execution paths shell out to the Claude CLI on `PATH` (override with `CLAUDE_BIN`); tests that need it skip when it isn't present. The run/server paths need a Postgres DSN in `WHILLY_DATABASE_URL`.

> **Behavior is being captured as normative specs under `openspec/specs/<capability>/spec.md`** (see `openspec/TAXONOMY.md` and the `openspec/COVERAGE-MATRIX.md` module→capability map). When a spec exists for a subsystem, treat it as the source of truth for *what* the code guarantees; this file is the orientation guide for *how*.

## Common commands

```bash
# Dev install (editable, with dev deps)
pip install -e '.[dev]'

# v4 run path needs Postgres (point at your DSN)
export WHILLY_DATABASE_URL=postgres://user:pass@localhost:5432/whilly
whilly plan import PLAN.json     # load a JSON plan into Postgres
whilly run --plan <plan-id>      # start a local worker that drains the plan's queue

# v3-compat shim still works (rewrites to subcommands under the hood)
whilly --tasks tasks.json        # → run --plan ... ; ./whilly.py and python -m whilly are equivalent launchers

# Lint + format (same commands CI runs)
ruff check whilly/ tests/
ruff format --check whilly/ tests/
ruff format whilly/ tests/                   # apply formatting

# Tests
pytest -q
pytest tests/test_whilly_dashboard.py        # single file
pytest tests/test_whilly_dashboard.py::test_name   # single test
pytest -k budget                             # by keyword
```

Line length is 120 (`[tool.ruff]` in `pyproject.toml`). Target `py312`.

## CLI surface worth knowing

`whilly` with no args prints help (`_print_help`). The real surface is subcommands dispatched in `whilly/cli/__init__.py`:

- **`whilly plan`** — import / export / show / reset / apply a plan in Postgres (`whilly/cli/plan.py`; this does NOT execute tasks).
- **`whilly run --plan <id>`** — start a **local worker** that drains the plan's task queue (`whilly/cli/run.py` → `whilly/worker/local.py`). Flags: `--max-iterations`, `--idle-wait`, `--heartbeat-interval`, `--worker-id`.
- **`whilly server`** — FastAPI control plane for remote workers + web dashboard (`whilly/api/`, `whilly/adapters/transport/server.py`).
- **`whilly-worker --connect URL --token TOKEN --plan ID`** — standalone **remote worker** over HTTP (`whilly/worker/remote.py`).
- **`whilly init [DESC] [--interactive]`** — PRD → tasks pipeline / interactive PRD wizard.
- **`whilly dashboard`**, **`whilly logs`**, **`whilly admin`**, **`whilly forge`**, **`whilly jira`**, **`whilly gitlab`**, **`whilly scheduler`**, plus `qa-release`, `project-config`, `github-projects`, `compliance`, `tui`, `pr-feedback`, `rollback`, `update`, `feedback`, `quick-setup`, `skill`.

**v3-compat shim** (`whilly/cli/__init__.py`): `--tasks PATH`→`run --plan`, `--headless`→sets `WHILLY_HEADLESS=1`, `--init DESC`→`init`, `--prd-wizard [SLUG]`→`init --interactive`, `--from-jira KEY [--go]`→`jira import`. `--resume`/`--reset`/`--all` are diagnostic no-ops (state lives in Postgres now). `--workspace`/`--worktree`/`--no-workspace`/`--no-worktree` are silently consumed no-ops.

Config is layered (`whilly/config.py`): dataclass defaults → user TOML (platformdirs) → repo `whilly.toml` → `.env` → `WHILLY_*` shell env → CLI flags. Read by `WhillyConfig.from_env()` / `load_layered()`. Defaults: model `claude-opus-4-6[1m]`, `MAX_PARALLEL=3`, `HEARTBEAT_INTERVAL=1`, `LOG_DIR=whilly_logs/`, `MAX_ITERATIONS=0` (unbounded). String fields support `env:` / `keyring:` / `file:` secret-scheme prefixes via `.resolved()`. **No-op legacy fields** (kept for `.env` compat, do nothing in v4): `WHILLY_WORKTREE`, `WHILLY_USE_WORKSPACE`, `WHILLY_USE_TMUX`, `WHILLY_STATE_FILE`, `WHILLY_ORCHESTRATOR`.

## Architecture big picture

The v4 execution model is a **Postgres task queue with claiming workers**, not an in-process loop.

1. **Composition root.** `whilly/cli/run.py::run_run_command` → `_async_run`: open the asyncpg pool (`adapters/db/pool.create_pool` from `WHILLY_DATABASE_URL`), register the worker (`INSERT INTO workers ... ON CONFLICT DO UPDATE`), `_select_plan_with_tasks`, build the per-task runner (workspace-aware), call `run_worker`, and `close_pool` in `finally`. Missing DSN or unknown plan → exit code 2.
2. **Worker loop.** `whilly/worker/local.py::run_local_worker` (remote: `whilly/worker/remote.py` over HTTP). Each iteration: `repo.claim_task(worker_id, plan_id)` (atomic `SELECT … FOR UPDATE SKIP LOCKED`, PENDING→CLAIMED, or `None`) → `repo.start_task(id, version)` (CLAIMED→IN_PROGRESS) → `runner(task, prompt)` → route by result: `is_complete and exit_code == 0` → `complete_task` (→DONE) else `fail_task` (→FAILED). On `claim_task` returning `None`, idle-wait and poll again. Terminates on the optional `stop` event (graceful: releases the in-flight task back to PENDING), `max_iterations` (test-only), or cancellation. `VersionConflictError` (a peer won the optimistic-lock race) is logged and the loop continues.
3. **Parallelism.** Achieved by **running multiple workers against the same plan** — Postgres `FOR UPDATE SKIP LOCKED` is the mutex. There is no in-process batching in the live path. `whilly/orchestrator.py::plan_batches`/`plan_batches_llm` and `MAX_PARALLEL` are **legacy v3 and unwired** in the run path.
4. **Persistence.** Postgres is the source of truth. `whilly/adapters/db/repository.py::TaskRepository` wraps claim/start/complete/fail/release + worker registration/heartbeat; `release_stale_tasks` is the visibility-timeout sweep (stale CLAIMED/IN_PROGRESS → PENDING). Schema in `whilly/adapters/db/migrations/versions/` (001–028): `workers, plans, tasks, events, repo_targets, work_intents, sessions, users, pull_requests, control_state, scheduler_rules/cycles`, plus auth tables. Every state transition writes an `events` audit row (batched via `whilly/api/event_flusher.py::EventFlusher`).
5. **Agent dispatch.** `whilly/adapters/runner/` invokes the Claude CLI as an async subprocess (`claude_cli.py`; binary `CLAUDE_BIN` default `claude`, model `WHILLY_MODEL`, `--output-format json`) and parses stdout via `result_parser.py` → `AgentResult(output, usage, exit_code, is_complete)`. `is_complete` is true iff `COMPLETION_MARKER` (`<promise>COMPLETE</promise>`) is in the output. Retries with 5/10/20/40/60s backoff on retriable API errors; auth errors fail fast. Default v4.7 posture is **deny-by-default** tools (`--disallowedTools Write,Edit,…,Bash`); `WHILLY_AGENT_ALLOW_SHELL=1` restores unattended shell. Backends: `whilly/agents/` (`claude` | `opencode`, plus `claude_handoff` for human-in-the-loop).
6. **Workspaces.** `whilly/workspaces.py::RepoTargetWorkspaceResolver` prepares a git checkout per task that has a `repo_target_id` (layout `.whilly_workspaces/repos/<repo>/<plan>/<task>`, branch `whilly/<plan>/<task>`); tasks without one run in the process cwd. Workspace-prep failure → exit code 4. `whilly/worktree_runner.py::WorktreeManager` (`.whilly_worktrees/{task_id}` create→`merge_back` cherry-pick→`cleanup`) exists but is **not wired into the live run path**.
7. **Control plane / transport.** `whilly/adapters/transport/server.py::create_app` exposes `/health`, `/workers/register` (bootstrap-token gated, mints a per-worker bearer; no plaintext token stored), `/workers/{id}/heartbeat`, long-polled `/tasks/claim`, `/tasks/{id}/complete|fail` (optimistic-locking → 200/409). Remote workers use `adapters/transport/client.py::RemoteWorkerClient` (httpx, retry/backoff, typed `AuthError`/`VersionConflictError`/`ServerError`). Web dashboard + SSE in `whilly/api/`; auth supports sessions/magic-link, optional OIDC, optional TOTP/WebAuthn.
8. **PRD pipeline.** `prd_generator` (non-interactive) and `prd_wizard` + `prd_launcher` (interactive via Claude CLI) create `PRD-*.md`, then task generation produces a plan; `decomposer` can split oversized pending tasks every `DECOMPOSE_EVERY` iterations.

### Plan / task model

Two `Task` types coexist: the canonical **v4 frozen dataclass** in `whilly/core/models.py` (`TaskStatus` enum `PENDING|CLAIMED|IN_PROGRESS|DONE|FAILED|SKIPPED`; no-default fields `id`, `status`; then `dependencies`, `key_files`, `priority`, `description`, `acceptance_criteria`, `test_steps`, `prd_requirement`, `version`, `repo_target_id`), and the **legacy v3 dataclass** in `whilly/task_manager.py` (no-default `id, phase, category, priority, description, status`; `VALID_STATUSES` is lowercase and adds `blocked`, `human_loop`) kept for JSON plan I/O. Plan I/O: `whilly/adapters/filesystem/plan_io.py` (`parse_plan`/`serialize_plan`; envelope requires `project` + `tasks[]`; extensions `PlanOrigin`, `RepoTarget`, `VerificationCommand`). `Task.from_dict` filters to known dataclass fields (extra on-disk keys dropped on round-trip). `whilly/cli/__init__.py::validate_schema` is the narrow legacy shim — it validates **every** task's `id` against `whilly/core/task_id.validate_task_id` (no "first N" limit); the v4 import path applies the same validator to every task.

### Prompt conventions

Agent prompts are built in `whilly/core/prompts.py::build_task_prompt`. They pin the agent to a single task, state acceptance criteria, and expect `<promise>COMPLETE</promise>` on success — `result_parser` keys task completion off that exact marker. Reuse `build_task_prompt` for new dispatch paths so the completion signal stays consistent.

## When editing

- **Behavior changes REQUIRE an opsx spec delta.** Any change to `whilly/` behavior MUST ship with an `opsx` change proposal (propose → apply → archive) that updates the relevant OpenSpec capability spec at `openspec/specs/<slug>/spec.md` — the change is not complete until that delta is applied and archived. See `openspec/FORWARD-PROCESS.md` for the full workflow and `openspec/AUTHORING.md` for how to write the delta. Don't let the spec and code drift (this file itself drifted from v3→v4; that's the failure mode the specs exist to prevent). Pure docs/test/refactor with no behavior change is exempt.
- **Postgres is the source of truth**, not files. Task state moves through `TaskRepository` (claim/start/complete/fail/release) with an optimistic-locking `version`; handle `VersionConflictError` by abandoning the row, never by force-writing. Every transition writes an `events` audit row.
- **Don't gate behavior on removed no-op env vars** (`WHILLY_WORKTREE`, `WHILLY_USE_WORKSPACE`, `WHILLY_USE_TMUX`, `WHILLY_STATE_FILE`). They parse but do nothing.
- **Two `Task` classes exist** — use `whilly/core/models.py` for the v4 domain/DB model; `whilly/task_manager.py` is legacy JSON-plan I/O only. Don't conflate their status vocabularies (UPPERCASE enum vs lowercase `VALID_STATUSES`).
- **Exit codes:** `0` ok, `1` validation error, `2` environment failure (no DSN / unknown plan / bad flags), `3` timeout (legacy), `4` workspace-prep failure.
- **Docs to keep current:** `README.md`, `docs/Whilly-Usage.md` (CLI + env var reference), `docs/Whilly-Interfaces-and-Tasks.md` (module contracts, task schema), and the relevant `openspec/specs/` capability.
