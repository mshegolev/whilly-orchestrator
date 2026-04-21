# Whilly Orchestrator

[![PyPI version](https://img.shields.io/pypi/v/whilly-orchestrator.svg)](https://pypi.org/project/whilly-orchestrator/)
[![PyPI downloads](https://img.shields.io/pypi/dm/whilly-orchestrator.svg)](https://pypi.org/project/whilly-orchestrator/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Self-Healing](https://img.shields.io/badge/self--healing-enabled-green.svg)](docs/Self-Healing-Guide.md)
[![Workshop kit](https://img.shields.io/badge/workshop-HackSprint1-blue.svg)](docs/workshop/INDEX.md)

Python implementation of the **Whilly Wiggum loop** — Ralph Wiggum's smarter brother. Same family, same "I'm helping!" spirit, but with TRIZ contradiction analysis, a Decision Gate that refuses nonsense upfront, a PRD wizard, and a Rich TUI dashboard on top of the classic continuous agent loop.

🇷🇺 [Краткое описание на русском](README-RU.md) · 🎓 [Workshop kit (HackSprint1)](docs/workshop/INDEX.md)

> "I'm helping — and I've read TRIZ." — Whilly Wiggum

## What it does

Whilly runs a loop: pick a pending task → hand it to an LLM agent → verify result → commit → next. It keeps running until the task board is empty, a budget is exhausted, or you stop it. Parallel mode dispatches multiple agents in tmux panes or git worktrees.

The base technique was first described in [Ghuntley's post on the Ralph Wiggum loop](https://ghuntley.com/ralph/) and widely adopted across the Claude Code community. Whilly is the brainier sibling: all of Ralph's "pick task → try → repeat" stamina, plus a TRIZ analyzer for surfacing contradictions, a Decision Gate for refusing garbage tasks, and a PRD wizard for understanding the problem before swinging at it.

## Features

- **Continuous agent loop** — pull tasks from a JSON plan, run Claude CLI on each, retry on transient errors
- **Rich TUI dashboard** — live progress, token usage, cost totals, per-task status; hotkeys for pause/reset/skip
- **Parallel execution** — tmux panes or git worktrees, up to N concurrent agents with budget/deadlock guards
- **Self-healing system** 🛡️ — auto-detect crashes, fix common code errors (NameError, ImportError), restart pipeline
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

Requires [Claude CLI](https://docs.claude.com/en/docs/claude-code) on `PATH` (or set `CLAUDE_BIN`).

## Quick start

1. Create `tasks.json` describing the work:

   ```json
   {
     "project": "health-endpoint",
     "tasks": [
       {
         "id": "TASK-001",
         "phase": "Phase 1",
         "category": "functional",
         "priority": "high",
         "description": "Add a /health endpoint returning {\"status\":\"ok\"}",
         "status": "pending",
         "dependencies": [],
         "key_files": ["app/server.py"],
         "acceptance_criteria": ["GET /health returns 200 with {\"status\":\"ok\"}"],
         "test_steps": ["curl -s localhost:8000/health"]
       },
       {
         "id": "TASK-002",
         "phase": "Phase 1",
         "category": "test",
         "priority": "high",
         "description": "Write a pytest covering the new endpoint",
         "status": "pending",
         "dependencies": ["TASK-001"],
         "key_files": ["tests/test_health.py"],
         "acceptance_criteria": ["pytest tests/test_health.py passes"],
         "test_steps": ["pytest -q tests/test_health.py"]
       }
     ]
   }
   ```

2. Run Whilly (2 concurrent agents, $5 budget cap):

   ```bash
   WHILLY_MAX_PARALLEL=2 WHILLY_BUDGET_USD=5 whilly tasks.json
   # straight from a checkout, no install:
   ./whilly.py tasks.json
   # or as a module:
   python -m whilly tasks.json
   # or just `whilly` with no args for the interactive plan-picker
   ```

3. Watch the dashboard. Press `q` to quit, `d` for task detail, `l` for the live log of a running agent, `t` for the task overview.

## 🛡️ Self-Healing System

Whilly includes a built-in self-healing system that automatically detects, analyzes, and fixes code errors to ensure pipeline resilience:

```bash
# Standard whilly (no crash protection)
whilly tasks.json

# Self-healing whilly (auto-fix + restart on crashes)
python scripts/whilly_with_healing.py tasks.json
```

**Supported error types:**
- ✅ `NameError` — missing variables/parameters (auto-fix)
- ✅ `ImportError` — missing modules (auto pip install)  
- ✅ `TypeError` — function parameter mismatches (diagnosis)
- ⚠️ `AttributeError` — missing object attributes (suggestions)

**Features:**
- 🔍 **Smart error detection** via traceback pattern analysis
- 🔧 **Automated fixes** for common coding errors
- 🔄 **Auto-restart** with exponential backoff (max 3 retries)
- 📊 **Learning from patterns** in historical error logs
- 💡 **Recovery suggestions** for complex issues

See [Self-Healing Guide](docs/Self-Healing-Guide.md) for complete documentation.

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
| `self_healing.py` | 🛡️ Error detection, analysis, and automated fixing |
| `recovery.py` | Task status synchronization and consistency validation |
| `reporter.py` | Per-iteration reports, cost totals, summary markdown |
| `verifier.py`, `notifications.py`, `history.py`, `config.py` | Infrastructure |

## Configuration

Whilly resolves config through layers (last wins):

```
defaults → user TOML → ./whilly.toml → .env → shell env (WHILLY_*) → CLI flags
```

Start from the committed template (works cross-platform — macOS / Linux / Windows):

```bash
cp whilly.example.toml whilly.toml           # project-local
# or place settings at the OS-native user path:
whilly --config path                         # shows the location
whilly --config edit                         # opens it in $EDITOR

# legacy .env users: one-shot migration that moves tokens into the OS keyring
whilly --config migrate
```

See [docs/Whilly-Usage.md](docs/Whilly-Usage.md) for the full config guide (TOML fields, secret schemes, user-config paths per OS). The env-var table below is kept as a reference — every `WHILLY_*` variable has an equivalent TOML field of the same name.

| Variable | Default | Purpose |
|---|---|---|
| `WHILLY_MODEL` | `claude-opus-4-6[1m]` | Claude model id |
| `WHILLY_MAX_PARALLEL` | `3` | Concurrent agents (1 = sequential) |
| `WHILLY_MAX_ITERATIONS` | `0` | Max work cycles per plan (0 = unlimited) |
| `WHILLY_BUDGET_USD` | `0` | Hard cost cap; 80% triggers warning, 100% stops the run |
| `WHILLY_TIMEOUT` | `0` | Wall-clock cap in seconds (0 = unlimited) |
| `WHILLY_USE_TMUX` | `1` | Use tmux panes for parallel agents |
| `WHILLY_WORKTREE` | `0` | Per-task git worktree isolation (needs `MAX_PARALLEL>1`) |
| `WHILLY_LOG_DIR` | `whilly_logs` | Per-task log directory |
| `WHILLY_STATE_FILE` | `.whilly_state.json` | Crash-recovery state file (`--resume` reads it) |
| `WHILLY_HEADLESS` | auto | CI mode — JSON on stdout, exit codes |
| `CLAUDE_BIN` | `claude` | Path to Claude CLI binary |
| `WHILLY_AGENT_BACKEND` | `claude` | Active agent backend (`claude` or `opencode`) |
| `WHILLY_OPENCODE_BIN` | `opencode` | Path to the OpenCode CLI binary |
| `WHILLY_OPENCODE_SAFE` | `0` | `1` → drop `--dangerously-skip-permissions` for OpenCode |
| `WHILLY_OPENCODE_SERVER_URL` | _(unset)_ | Optional remote OpenCode server URL |

**New here?** Start with [`docs/Getting-Started.md`](docs/Getting-Started.md) — practical walkthroughs from install to first run.

Key CLI flags: `--all`, `--headless`, `--timeout N`, `--resume`, `--reset PLAN.json`, `--init "desc" [--plan] [--go]`, `--plan PRD.md`, `--prd-wizard`, `--no-worktree`, `--agent {claude,opencode,claude_handoff}`.

### Task sources (pull issues into plans)

```bash
whilly --from-github whilly:ready --go          # all open issues with label
whilly --from-github all --go                   # every open issue, no label filter
whilly --from-issue owner/repo/42 --go          # one GitHub issue (slash form — shell-safe)
whilly --from-issue 'owner/repo#42' --go        # same, with '#' (quote in zsh/bash)
whilly --from-jira ABC-123 --go                 # one Jira ticket
whilly --from-project <project-v2-url> --go     # GitHub Projects v2 board
whilly --from-issues-project <url> --repo owner/name   # board-filtered issues
```

### Lifecycle sync (live board updates as whilly works)

Enable in `whilly.toml`:
```toml
[project_board]                                  # GitHub Projects v2
url = "https://github.com/users/you/projects/4"
default_repo = "you/your-repo"

[jira]                                           # Jira transitions (complements auto-close)
server_url = "https://company.atlassian.net"
username   = "you@example.com"
token      = "keyring:whilly/jira"
enable_board_sync = true
```

Task status → column mapping is identical for both: `pending → Todo/To Do`, `in_progress → In Progress`, `done → In Review`, `merged → Done`, `failed → Failed`, `skipped → Refused/Cancelled`, `blocked → On Hold/Blocked`, `human_loop → Human Loop/Waiting for Customer`. Override per-section via `[project_board.status_mapping]` / `[jira.status_mapping]`.

One-off helpers:
```bash
whilly --ensure-board-statuses             # create any missing Status columns
whilly --post-merge <plan.json>            # after an external merge, flush cards to Done
python3 scripts/populate_board.py …        # bulk-add issues onto a Projects v2 board
```

### Human-in-the-loop backend (`claude_handoff`)

```bash
WHILLY_AGENT_BACKEND=claude_handoff whilly --from-issue alice/repo/42 --go
```

Each task's prompt lands in `.whilly/handoff/<task_id>/prompt.md`; whilly blocks until you write `result.json`. Three companion commands:

```bash
whilly --handoff-list                                  # see pending handoffs
whilly --handoff-show GH-42                            # read the prompt
whilly --handoff-complete GH-42 --status complete --message "done"
```

Accepted statuses: `complete` / `failed` / `blocked` / `human_loop` / `partial`. `blocked` and `human_loop` map to extra whilly statuses + board columns so tickets needing a decision land in a dedicated column instead of being misreported as failed.

Exit codes in headless mode: `0` success, `1` some tasks failed, `2` budget exceeded, `3` timeout.

See `docs/Whilly-Usage.md` for the full CLI reference.

## Documentation

- [Whilly-Usage.md](docs/Whilly-Usage.md) — CLI reference and flag catalog
- [Whilly-Interfaces-and-Tasks.md](docs/Whilly-Interfaces-and-Tasks.md) — task file format, state store schema, agent output contract
- [docs/workshop/INDEX.md](docs/workshop/INDEX.md) — Workshop kit (HackSprint1)

## Workshop kit

Whilly ships with a **HackSprint1 workshop kit** — a 90-minute hands-on tutorial that takes you from `pip install` to a running self-hosting bootstrap demo. Two tracks:

- **Track A (`tasks.json`)** — works without GitHub auth, 30 min.
- **Track B (GitHub Issues)** — full e2e with PR creation, 60 min.

Includes BRD, PRD, 12 ADRs, sample plans, and a roadmap. See [docs/workshop/INDEX.md](docs/workshop/INDEX.md) for the full guide. RU/EN bilingual.

## Backends

Whilly ships with pluggable agent backends behind a single `AgentBackend` Protocol (see `whilly/agents/`).

| Backend | Select | CLI wrapped | Notes |
|---|---|---|---|
| **Claude** (default) | `--agent claude` / `WHILLY_AGENT_BACKEND=claude` | `claude --output-format json -p "…"` | Requires [Claude CLI](https://docs.claude.com/en/docs/claude-code). Set `CLAUDE_BIN` to override path. |
| **OpenCode** | `--agent opencode` / `WHILLY_AGENT_BACKEND=opencode` | `opencode run --format json --model <provider/id> "…"` | Requires [sst/opencode](https://github.com/sst/opencode) on `PATH` (or `WHILLY_OPENCODE_BIN`). Set `WHILLY_OPENCODE_SAFE=1` to respect its per-tool permission policy. |

Model ids pass through normalization per backend — e.g. `claude-opus-4-6` automatically becomes `anthropic/claude-opus-4-6` for OpenCode. Completion is signalled identically (`<promise>COMPLETE</promise>`) so the main loop is backend-agnostic. Decision Gate, tmux runner, and the subprocess fallback all route through the active backend.

## Workflow boards

Whilly can sync a GitHub Projects v2 board as issues move through the pipeline (ready → picked_up → in_review → done / refused / failed). Board integration is Protocol-driven (`whilly/workflow/BoardSink`) — today one adapter ships (`GitHubProjectBoard` via `gh api graphql`); Jira/Linear/GitLab drop in as sibling implementations.

Before first use, run the analyzer to map whilly's six lifecycle events to your board columns:

```bash
whilly --workflow-analyze https://github.com/users/<you>/projects/<N>
```

The analyzer prints matched / missing / ambiguous columns and walks you through `[A]dd / [M]ap / [S]kip` decisions. Output goes to `.whilly/workflow.json` — a committable artefact so teams share one contract. Extra flags: `--apply` (auto-add all missing columns, CI-friendly) and `--report` (dry-run, no writes).

See [ADR-014](docs/workshop/adr/ADR-014-workflow-sink-protocol.md) for the design rationale and extension guide.

## Self-hosting pipelines

Two e2e scripts ship for "whilly processes its own GitHub issues end-to-end", differing in how much *thinking* they do before coding. Pick by issue complexity:

| Script | Stages | Use when |
|---|---|---|
| [`scripts/whilly_e2e_demo.py`](scripts/whilly_e2e_demo.py) | fetch → Decision Gate → execute → PR → review-fix loop | Issue is crisp, single-file, "just do it" scoped. Ralph-loop reference. |
| [`scripts/whilly_e2e_triz_prd.py`](scripts/whilly_e2e_triz_prd.py) | fetch → Gate → **TRIZ challenge** → **PRD** → **tasks decomp** → execute → quality gate → PR | Issue deserves decomposition. "Whilly Wiggum" smarter-brother variant. |

Both honour the workflow board integration (`WHILLY_PROJECT_URL=...` → cards move at every stage) and share the hard `WHILLY_BUDGET_USD` cap.

Typical invocation for the TRIZ+PRD pipeline:

```bash
unset GITHUB_TOKEN
WHILLY_REPO=mshegolev/whilly-orchestrator \
WHILLY_LABEL=whilly:ready \
WHILLY_BUDGET_USD=30 \
WHILLY_PROJECT_URL=https://github.com/users/mshegolev/projects/4 \
python scripts/whilly_e2e_triz_prd.py --limit 1
```

`--limit N` caps issues per run; `--dry-run` skips all LLM / PR / merge work for plan-only inspection; `--allow-auto-merge` is OFF by default — a pipeline that modifies whilly's own code always leaves PRs for human review. Details + design rationale in [ADR-015](docs/workshop/adr/ADR-015-e2e-triz-prd-pipeline.md).

## Troubleshooting / FAQ

| Issue | Fix |
|---|---|
| `gh auth status` returns 401 ("token invalid") | `unset GITHUB_TOKEN` (env-based token overrides keyring auth), then `gh auth login` if needed. |
| `claude: command not found` | Install Claude CLI from [docs.claude.com](https://docs.claude.com/en/docs/claude-code) or set `CLAUDE_BIN` to its path. |
| Dashboard rendering broken on narrow terminal (<100 cols) | `WHILLY_HEADLESS=1 whilly tasks.json` — disables TUI, streams JSON events on stdout. |
| Budget hits 0 unexpectedly | Set or raise `WHILLY_BUDGET_USD` (default unlimited; 0 also means unlimited). |
| `tmux ls` shows no sessions after dispatch | Either tmux isn't installed, or `WHILLY_USE_TMUX=0` — whilly silently falls back to subprocess mode. |
| Agent loops forever without marking done | Ensure prompt ends with the `<promise>COMPLETE</promise>` marker contract — `agent_runner.is_complete` checks that string. |

## Workshop kit

Running HackSprint1 or a self-paced walkthrough? The full workshop kit (BRD, PRD, ADRs, tutorial, roadmap) lives under [docs/workshop/INDEX.md](docs/workshop/INDEX.md).

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check whilly/ tests/
ruff format whilly/ tests/
```

## Credits

- Technique lineage: [Ghuntley's original Ralph Wiggum loop post](https://ghuntley.com/ralph/) — the pattern whilly descends from.
- Spirit of the family — Ralph's "I'm helping!" captures the essence of an agent that just keeps going, no matter what. Whilly is his smarter brother: same stamina, plus TRIZ, Decision Gate, PRD wizard.

## Related work

- Earlier Ralph-loop implementations exist across the Claude Code community. Whilly sets itself apart with a Rich TUI dashboard, TRIZ analyzer, Decision Gate pre-flight, PRD wizard, and tmux/git-worktree parallel execution — the "smarter brother" kit on top of the base loop.

## License

MIT — see [LICENSE](LICENSE).
