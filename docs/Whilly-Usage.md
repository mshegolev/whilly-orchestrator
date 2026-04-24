---
title: Full Usage Reference
nav_order: 3
---

# Whilly — Task Orchestrator

Python-based task orchestrator that runs Claude CLI agents to execute tasks from a JSON plan file.

## Quick Start

```bash
# Install — prod (default, released version, isolated CLI on macOS/Linux/Windows)
pipx install whilly-orchestrator
# Contributor install instead? See README "For contributors (dev)" or: make install-dev

# First run: let whilly tell you where your user config lives
whilly --config path

# Either migrate a legacy .env, or copy the template:
whilly --config migrate                           # if you have an old .env
cp whilly.example.toml whilly.toml                # or start fresh from template

# Run with a specific plan
whilly .planning/my_tasks.json

# Auto-discover plans in the current directory
whilly

# Run all discovered plans sequentially
whilly --all

# Pull open GitHub issues as tasks and start immediately
whilly --from-github whilly:ready --go

# One specific issue or Jira ticket
whilly --from-issue owner/repo/42 --go           # slash form — shell-safe
whilly --from-issue 'owner/repo#42' --go         # '#' form, quote in zsh/bash
whilly --from-jira ABC-123 --go                  # single Jira ticket
```

## Task sources

| Flag | Source | Notes |
|------|--------|-------|
| `--from-github <label>` | GitHub issues by label | `all`/`*`/`-` = no filter |
| `--from-issue <ref>` | one GitHub issue | `owner/repo/N`, `owner/repo#N`, or URL |
| `--from-jira <key>` | one Jira ticket | `ABC-123` or browse URL; auth via `[jira]` |
| `--from-project <url>` | GitHub Projects v2 board | full board items |
| `--from-issues-project <url> --repo o/r` | Projects board filtered by issue repo | |

Every source writes to an idempotent plan file (`tasks-…json`); re-running refreshes description/priority/labels without losing status.

## Lifecycle sync

Two integrations drive cards/tickets automatically as whilly task statuses change. Enable one or both in `whilly.toml`.

### GitHub Projects v2
```toml
[project_board]
url = "https://github.com/users/you/projects/4"
enabled = true
default_repo = "you/your-repo"

[project_board.status_mapping]    # optional
in_progress = "Doing"
```
Requires `gh auth refresh -s project` once.

### Jira
```toml
[jira]
server_url = "https://company.atlassian.net"
username   = "you@example.com"
token      = "keyring:whilly/jira"
enabled    = true
enable_board_sync = true

[jira.status_mapping]             # optional
in_progress = "Doing"
done        = "Review"
```
Drives Jira transitions via REST v3. Uses `urllib` stdlib — no extra deps.

### Status mapping (defaults)

| whilly | GitHub column | Jira transition |
|---|---|---|
| `pending` | Todo | To Do |
| `in_progress` | In Progress | In Progress |
| `done` (PR open) | In Review | In Review |
| `merged` | Done | Done |
| `failed` | Failed | Failed |
| `skipped` | Refused | Cancelled |
| `blocked` | On Hold | Blocked |
| `human_loop` | Human Loop | Waiting for Customer |

## Companion commands

```bash
whilly --config show                           # merged config, secrets redacted
whilly --config path                           # OS-native user config location
whilly --config migrate                        # legacy .env → whilly.toml + keyring
whilly --ensure-board-statuses                 # create missing Projects v2 columns
whilly --post-merge <plan.json>                # after an out-of-band merge: flush cards/tickets to Done
```

## Human-in-the-loop backend

`claude_handoff` pauses each task and waits for an external operator (or an interactive Claude session) to do the work:

```bash
WHILLY_AGENT_BACKEND=claude_handoff whilly --from-issue alice/repo/42 --go
# whilly writes .whilly/handoff/GH-42/prompt.md and blocks

whilly --handoff-list
whilly --handoff-show GH-42
whilly --handoff-complete GH-42 --status complete --message "done"
#                              ^^^^^^^^ complete / failed / blocked / human_loop / partial
```

`blocked` and `human_loop` signal "task can't finish without help" — they land in the corresponding board column without being misreported as failed.

## Configuration

Whilly reads its config through five layers, last wins:

```
defaults (dataclass)
  ↓
user TOML    — OS-native per-user config
  ↓
repo TOML    — ./whilly.toml  (project-local overrides)
  ↓
.env         — legacy, loads with a deprecation warning
  ↓
shell env    — WHILLY_* variables
  ↓
CLI flags    — highest precedence
```

### User config location

`whilly --config path` prints the OS-native location:

| OS       | Path                                                         |
|----------|--------------------------------------------------------------|
| macOS    | `~/Library/Application Support/whilly/config.toml`           |
| Linux    | `$XDG_CONFIG_HOME/whilly/config.toml` (default `~/.config/whilly/config.toml`) |
| Windows  | `%APPDATA%\whilly\config.toml`                               |

### Inspect the merged config

```bash
whilly --config show          # merged result, secrets redacted
whilly --config edit          # open user config in $EDITOR
```

### Example `whilly.toml`

```toml
# Core loop
MAX_PARALLEL = 1             # concurrent agents (1 = sequential)
MAX_ITERATIONS = 0           # 0 = unlimited
BUDGET_USD = 0               # 0 = unlimited; warns at 80 %, kills at 100 %

# Agent backend
AGENT_BACKEND = "claude"
MODEL = "claude-opus-4-7[1m]"

# Workspace + tmux
USE_WORKSPACE = false           # v3.3.0: off by default; set to true or pass --workspace to enable
USE_TMUX = false

# Logging
LOG_DIR = "whilly_logs"
VOICE = false

# External integrations
CLOSE_EXTERNAL_TASKS = true
GITHUB_AUTO_CLOSE = true

# GitHub auth — cross-platform secrets
# Schemes: env:NAME, keyring:service/user, file:/path, literal
[github]
token = "keyring:whilly/github"

# Jira (optional)
[jira]
# server_url = "https://jira.example.com"
# username   = "you@example.com"
# token      = "keyring:whilly/jira"
```

Store secrets once, per OS:

```bash
python3 -c "import keyring; keyring.set_password('whilly', 'github', 'ghp_xxx')"
```

### GitHub auth resolution order

Used by `whilly/gh_utils.py::gh_subprocess_env()` when invoking `gh`:

1. `WHILLY_GH_TOKEN`              → overrides everything, just for whilly's subprocesses
2. `WHILLY_GH_PREFER_KEYRING=1`   → strips env tokens, forces `gh` to use its own keyring
3. `[github].token` in `whilly.toml` → resolved via `env:` / `keyring:` / `file:` schemes
4. Ambient `GITHUB_TOKEN` / `GH_TOKEN` — passed through unchanged (cross-platform default)

### Full env var reference (back-compat)

| Variable                          | Default                | Description |
|-----------------------------------|------------------------|-------------|
| `WHILLY_MAX_ITERATIONS`           | `0` (unlimited)        | Max work iterations per plan |
| `WHILLY_MAX_PARALLEL`             | `3`                    | Concurrent agents (1 = sequential) |
| `WHILLY_MAX_TASK_RETRIES`         | `5`                    | Retries before a task is skipped/failed |
| `WHILLY_BUDGET_USD`               | `0`                    | `0` = unlimited; warns at 80 %, kills at 100 % |
| `WHILLY_TIMEOUT`                  | `0`                    | Wall-clock seconds per plan (`0` = unlimited) |
| `WHILLY_AGENT_BACKEND`            | `claude`               | `claude` or `opencode` |
| `WHILLY_MODEL`                    | `claude-opus-4-6[1m]`  | LLM model |
| `WHILLY_USE_TMUX`                 | `0`                    | Run each agent in its own tmux session |
| `WHILLY_USE_WORKSPACE`            | `0`                    | Plan-level git worktree workspace (off by default since v3.3.0; set `1` or use `--workspace` to enable) |
| `WHILLY_LOG_DIR`                  | `whilly_logs`          | Directory for per-task logs |
| `WHILLY_ORCHESTRATOR`             | `file`                 | `file` (key-files collisions) or `llm` (LLM batching) |
| `WHILLY_VOICE`                    | `1`                    | macOS voice notifications |
| `WHILLY_HEADLESS`                 | `0`                    | JSON stdout, no TUI (auto when stdout is not a TTY) |
| `WHILLY_DECOMPOSE_EVERY`          | `5`                    | Re-plan oversized pending tasks every N iterations |
| `WHILLY_AUTO_MERGE`               | `ask`                  | `ask` / `yes` / `claude` / `no` on plan completion |
| `WHILLY_GH_TOKEN`                 | *(unset)*              | Whilly-only GitHub token (overrides ambient) |
| `WHILLY_GH_PREFER_KEYRING`        | `0`                    | Force `gh` keyring auth even when `GITHUB_TOKEN` is set |
| `WHILLY_SUPPRESS_DOTENV_WARNING`  | `0`                    | Silence the legacy `.env` deprecation warning |

Every `WHILLY_*` variable corresponds to an equivalent `whilly.toml` field (same name, any case).
See `whilly.example.toml` for the complete template.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| q | Graceful shutdown |
| d | Task detail overlay |
| l | Log viewer (last 30 lines) |
| t | All tasks overview |
| h | Help screen |

(Windows: key listener is disabled; the dashboard itself still renders.)

## Task Plan JSON Format

```json
{
  "project": "My Project",
  "tasks": [
    {
      "id": "TASK-001",
      "phase": "Phase 1",
      "category": "functional",
      "priority": "critical",
      "description": "What to do",
      "status": "pending",
      "dependencies": [],
      "key_files": ["path/to/file.py"],
      "acceptance_criteria": ["AC1"],
      "test_steps": ["step1"]
    }
  ]
}
```

## Architecture

```
whilly.py                    Entry point + main loop
whilly/
  config.py                 Layered config (defaults → TOML → .env → env)
  secrets.py                env:/keyring:/file:/literal resolver
  gh_utils.py               Central gh CLI subprocess env resolution
  task_manager.py           JSON plan CRUD, dependency resolution
  agent_runner.py           Claude/OpenCode subprocess + JSON parsing
  tmux_runner.py            Tmux session isolation
  orchestrator.py           File-based + LLM task batching
  dashboard.py              Rich Live TUI + keyboard handler
  reporter.py               JSON + Markdown cost reports
  decomposer.py             Task decomposition via LLM
  notifications.py          macOS voice alerts
  sources/                  Input adapters (GitHub Issues, Project v2, unified)
  sinks/                    Output adapters (PR creation, etc.)
  agents/                   Pluggable backends (claude, opencode)
```

## Tmux Setup (optional)

When `USE_TMUX = true`, each agent runs in its own tmux session:

```bash
# View running agent sessions
tmux ls | grep whilly-

# Attach to a specific agent
tmux attach -t whilly-TASK-001

# Kill a session
tmux kill-session -t whilly-TASK-001
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Dashboard doesn't render | Check Rich works: `python3 -c "from rich import print; print('[bold]test[/]')"` |
| Agent auth errors (403) | Check Claude CLI: `claude --version` |
| `gh` returns 401 / not found | `unset GITHUB_TOKEN && gh auth status`; set `WHILLY_GH_PREFER_KEYRING=1` on macOS |
| Tmux not found | `brew install tmux` or set `USE_TMUX = false` |
| Tasks stuck in_progress | Whilly resets stale tasks on startup |
| Too many API errors | Whilly pauses 60 s after 5+ consecutive failures |
| Legacy `.env` warning on every run | Run `whilly --config migrate`, or silence with `WHILLY_SUPPRESS_DOTENV_WARNING=1` |
