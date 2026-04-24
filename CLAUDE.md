# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Whilly is a Python implementation of the **Whilly Wiggum loop** — Ralph Wiggum's smarter brother in Ghuntley's technique lineage. Ralph picks a task, tries, shouts "I'm helping!", repeats. Whilly does the same but with TRIZ contradiction analysis, a Decision Gate that refuses nonsense tasks upfront, and a PRD wizard that understands the problem first. Ships as the `whilly` console script (entry `whilly.cli:main`) plus a small `whilly` package. Version lives in `whilly/__init__.py` and `pyproject.toml` (keep them in sync).

Requires Python 3.10+. Most end-to-end paths shell out to the Claude CLI on `PATH` (override with `CLAUDE_BIN`); tests that need it skip when it isn't present.

## Common commands

```bash
# Dev install (editable, with dev deps)
pip install -e '.[dev]'

# Run from a source checkout — all three are equivalent
./whilly.py --tasks tasks.json
python -m whilly --tasks tasks.json
whilly --tasks tasks.json                    # after install

# Lint + format (same commands CI runs)
ruff check whilly/ tests/
ruff format --check whilly/ tests/
ruff format whilly/ tests/                   # apply formatting

# Tests (pytest matrix runs on 3.10 / 3.11 / 3.12 in CI)
pytest -q
pytest tests/test_whilly_dashboard.py        # single file
pytest tests/test_whilly_dashboard.py::test_name   # single test
pytest -k budget                             # by keyword
```

Line length is 120 (`[tool.ruff]` in `pyproject.toml`). Target `py310`.

## CLI surface worth knowing

- `whilly` with no args → interactive menu (discovers `tasks.json` + `.planning/*tasks*.json` + `PRD-*.md`).
- `whilly --all` runs every discovered plan sequentially.
- `whilly --headless` / non-TTY stdout → emits structured JSON on stdout, uses exit codes `0=ok, 1=some failed, 2=budget exceeded, 3=timeout`.
- `whilly --resume` restores from `.whilly_state.json` (plan file, iteration, cost, live tmux sessions).
- `whilly --reset PLAN.json` resets all tasks to `pending`.
- `whilly --init "desc" [--plan] [--go]` PRD → tasks → optional auto-exec pipeline.
- `whilly --prd-wizard [slug]` launches Claude CLI interactively with the PRD master prompt.
- Plan-level workspace in `.whilly_workspaces/{slug}/` is **off by default since v3.3.0**. Opt in with `whilly --workspace` (or `--worktree`) / `WHILLY_USE_WORKSPACE=1`. `--no-workspace` / `--no-worktree` are retained as no-ops for backward compatibility.

Config is almost entirely env vars read by `WhillyConfig.from_env()` (`whilly/config.py`) — prefix `WHILLY_`, e.g. `WHILLY_MAX_PARALLEL`, `WHILLY_BUDGET_USD`, `WHILLY_MODEL`, `WHILLY_USE_TMUX`, `WHILLY_HEADLESS`, `WHILLY_TIMEOUT`, `WHILLY_STATE_FILE`. Defaults: model `claude-opus-4-6[1m]`, max_parallel 3, log_dir `whilly_logs/`.

## Architecture big picture

The main loop lives in `whilly/cli.py::run_plan`. One plan execution flows like this:

1. **Workspace isolation.** If `USE_WORKSPACE` is on (**off by default since v3.3.0** — enable with `--workspace` or `WHILLY_USE_WORKSPACE=1`), `worktree_runner.create_plan_workspace` creates/reuses a git worktree at `.whilly_workspaces/{slug}/` and the loop `chdir`s into it. The plan file itself is resolved to an absolute path *before* chdir so agents still read/write the canonical JSON in the main repo.
2. **Task loading.** `task_manager.TaskManager` loads a JSON plan (`{project, prd_file, tasks: [...]}`) with atomic writes. Task statuses: `pending | in_progress | done | failed | skipped`. `get_ready_tasks()` respects `dependencies`, `PRIORITY_ORDER`, and resets stale `in_progress` tasks on startup.
3. **Batch planning.** If `MAX_PARALLEL > 1`, `orchestrator.plan_batches` groups ready tasks by non-overlapping `key_files` (or `plan_batches_llm` asks an LLM). Only the first batch of each iteration is dispatched — the loop re-evaluates after it completes so newly-unblocked tasks can join the next batch.
4. **Agent dispatch.** Two runners:
   - `tmux_runner` — each agent in `whilly-{task_id}` tmux session (preferred when tmux is available and `USE_TMUX=1`).
   - Subprocess fallback when tmux isn't available.
   Optional per-task isolation via `worktree_runner.WorktreeManager` when `WHILLY_WORKTREE=1` *and* `MAX_PARALLEL > 1` — creates `.whilly_worktrees/{task_id}` worktree per task, cherry-picks back on `done`, cleans up after.
5. **Result collection.** `agent_runner.collect_result[_from_file]` parses Claude CLI JSON output → `AgentResult(usage, exit_code, is_complete)`. `<promise>COMPLETE</promise>` in result text marks the task done. Exponential backoff on API errors (5/10/20/40/60s), `MAX_TASK_RETRIES` (default 5) before a task is marked `skipped` (deadlock) or `failed` (auth).
6. **Guards.** Budget check against `BUDGET_USD` (80% warning, 100% kills all tmux sessions and exits with code 2). Deadlock detection — a task stuck `in_progress` ≥ 3 iterations is marked `skipped`. Global: 5 consecutive iterations without `done_count` progress → pause 60s. Wall clock `TIMEOUT` → kill and exit 3.
7. **State + reporting.** `state_store.StateStore` persists iteration/cost/task status/tmux sessions for `--resume`. `reporter.Reporter` writes per-iteration JSON and an end-of-run Markdown summary (under `.planning/reports/` when multiple plans). `dashboard.Dashboard` is a Rich Live TUI with hotkeys (`q`=quit, `p`=pause, `d`=detail, `l`=logs, `t`=tasks, `h`=help); `NullDashboard` is substituted in headless mode. Every significant event also appends to `whilly_logs/whilly_events.jsonl`.
8. **PRD pipeline.** `prd_generator` (non-interactive) and `prd_wizard` + `prd_launcher` (interactive via Claude CLI) create `PRD-*.md`, then `generate_tasks` produces `tasks.json`. `decomposer` can split oversized pending tasks mid-run every `DECOMPOSE_EVERY` iterations.

### Plan JSON contract

Minimum task fields the orchestrator cares about: `id`, `status`, plus `dependencies`, `key_files`, `priority` (`critical|high|medium|low`), `description`, `acceptance_criteria`, `test_steps`. Use `Task.from_dict` / `Task.to_dict` (`task_manager.py`) — extra keys on disk are tolerated but dropped on round-trip. Schema is validated by `cli.validate_schema` before a plan is accepted.

### Prompt conventions

Agent prompts are built by `cli.build_task_prompt` (parallel / targeted) and `cli.build_sequential_prompt` (sequential). They reference `@tasks.json` and `@progress.txt`, pin the agent to a single task ID, require `make lint` / `make test` to pass, and expect `<promise>COMPLETE</promise>` on success. When adding new orchestration modes, reuse these builders so the completion signal stays consistent.

## When editing

- **Keep the startup banner intact.** `_show_startup_banner` prints version/SHA and sleeps 5s before any plan work — tests and ops rely on it.
- **Don't leak cwd changes.** `run_plan` chdirs into a workspace and must restore `_original_cwd` in all exit paths. Any new early return needs the same cleanup.
- **Status transitions go through `TaskManager.mark_status` / `save` / `reload`** — the file is the source of truth, re-read it after anything that might have written (agent run, auto-retry, deadlock skip).
- **`validate_schema` only checks the first 3 tasks.** If you add required fields, update it or it will silently accept partial plans.
- **Docs to keep current:** `README.md`, `docs/Whilly-Usage.md` (CLI + env var reference), `docs/Whilly-Interfaces-and-Tasks.md` (module contracts, task JSON schema).
