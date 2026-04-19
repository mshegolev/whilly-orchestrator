# Whilly Orchestrator

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Python implementation of the **Ralph Wiggum technique** — continuous AI agent loops for autonomous software development. Drive a coding agent (Claude CLI) around a task board until the work is done, with a Rich TUI dashboard, task decomposition, TRIZ analysis and PRD generation.

> "I'm helping!" — Ralph Wiggum

## What it does

Whilly runs a loop: pick a pending task → hand it to an LLM agent → verify result → commit → next. It keeps running until the task board is empty, a budget is exhausted, or you stop it. Parallel mode dispatches multiple agents in tmux panes or git worktrees.

Originally described in [Ghuntley's post on the Ralph Wiggum technique](https://ghuntley.com/ralph/) and widely adopted across the Claude Code community. This is a batteries-included orchestrator with a dashboard and task lifecycle around that loop.

## Features

- **Continuous agent loop** — pull tasks from a simple `tasks.log` file, run Claude CLI on each, retry on transient errors
- **Rich TUI dashboard** — live progress, token usage, cost totals, per-task status; hotkeys for pause/reset/skip
- **Parallel execution** — tmux panes or git worktrees, up to N concurrent agents with budget/deadlock guards
- **Task decomposer** — LLM-based breakdown of oversized tasks into subtasks
- **PRD wizard** — interactive Product Requirements Document generation, then auto-derive tasks from the PRD
- **TRIZ analyzer** — surface contradictions and inventive principles for ambiguous tasks
- **State store** — persistent task state across restarts, per-task per-iteration logs
- **Notifications** — budget warnings, deadlock detection, auth/API error alerts

## Install

```bash
pip install whilly-orchestrator
```

Or from source:

```bash
git clone https://github.com/mshegolev/whilly-orchestrator
cd whilly-orchestrator
pip install -e .
```

Requires [Claude CLI](https://docs.claude.com/en/docs/claude-code) on PATH (or set `CLAUDE_BIN`).

## Quick start

1. Create a `tasks.log` with one task per line:

   ```
   TASK-001 Add a /health endpoint returning {"status":"ok"}
   TASK-002 Write a pytest covering the new endpoint
   TASK-003 Update README with the new endpoint
   ```

2. Run Whilly:

   ```bash
   whilly --tasks tasks.log --parallel 2
   # or without install:
   python -m whilly --tasks tasks.log --parallel 2
   ```

3. Watch the dashboard. Press `q` to quit, `p` to pause, `r` to reset a failed task.

## Modules

| Module | Purpose |
|---|---|
| `orchestrator.py` | Main loop, batch planning, interface agreement between agents |
| `agent_runner.py` | Claude CLI wrapper, JSON output parsing, usage accounting |
| `tmux_runner.py` | Parallel agents in tmux panes |
| `worktree_runner.py` | Parallel agents in isolated git worktrees |
| `dashboard.py` | Rich TUI dashboard with hotkeys |
| `task_manager.py` | Task lifecycle (pending → in_progress → done/failed) |
| `state_store.py` | Persistent state across restarts |
| `decomposer.py` | LLM-based task breakdown |
| `prd_generator.py`, `prd_wizard.py`, `prd_launcher.py` | PRD generation and task derivation |
| `triz_analyzer.py` | TRIZ contradiction analysis |
| `reporter.py` | Per-iteration reports, cost totals, summary markdown |
| `verifier.py`, `notifications.py`, `history.py`, `config.py` | Infrastructure |

## Configuration

Pass flags to `whilly` or set environment variables:

- `CLAUDE_BIN` — path to Claude CLI binary
- `--model` — Claude model id (default: `claude-opus-4-6[1m]`)
- `--parallel N` — concurrent agents (default 1)
- `--budget-usd` — hard cap on spend
- `--tasks <file>` — task list file
- `--worktree` — use git worktrees instead of tmux

See `docs/Whilly-Usage.md` for the full CLI reference.

## Documentation

- [Whilly-Usage.md](docs/Whilly-Usage.md) — CLI reference and flag catalog
- [Whilly-Interfaces-and-Tasks.md](docs/Whilly-Interfaces-and-Tasks.md) — task file format, state store schema, agent output contract

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check whilly/ tests/
ruff format whilly/ tests/
```

## Credits

- Technique attribution: [Ghuntley — the Ralph Wiggum technique](https://ghuntley.com/ralph/)
- Spirit of the technique — a Simpsons character whose "I'm helping!" captures the essence of an agent that just keeps going, no matter what

## Related work

- [`ralph-orchestrator`](https://pypi.org/project/ralph-orchestrator/) by [@mikeyobrien](https://github.com/mikeyobrien/ralph-orchestrator) — another implementation of the same technique. Whilly differentiates with a Rich TUI dashboard, TRIZ analyzer, PRD wizard, and tmux/git-worktree parallel execution.

## Workshop kit

Ready to run a team hackathon with Whilly? The **HackSprint1** workshop kit
has everything you need — facilitator guides, gap-pack exercises, and
decision-gate templates.

👉 [docs/workshop/INDEX.md](docs/workshop/INDEX.md)

## License

MIT — see [LICENSE](LICENSE).
