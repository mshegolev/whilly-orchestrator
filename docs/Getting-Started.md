---
title: Getting Started
nav_order: 2
---

# Whilly — Getting Started

Step-by-step walkthroughs for the most common flows. If you want the full flag/config reference, see [`Whilly-Usage.md`](./Whilly-Usage.md).

---

## 0. Install (one-time)

```bash
# macOS / Linux / Windows (needs Python 3.10+)
pipx install whilly-orchestrator

# verify
whilly --help
whilly --config path           # prints the OS-native user config file location
```

Locations `--config path` prints:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/whilly/config.toml` |
| Linux | `$XDG_CONFIG_HOME/whilly/config.toml` |
| Windows | `%APPDATA%\whilly\config.toml` |

Secrets live in the OS keyring, never in repo files. To write one:

```bash
python3 -c "import keyring; keyring.set_password('whilly', 'github', 'ghp_xxx')"
python3 -c "import keyring; keyring.set_password('whilly', 'jira',  'jira_api_token')"
```

---

## 1. First-time GitHub setup

```bash
unset GITHUB_TOKEN                   # clear any stale shell token
gh auth login                        # browser flow, one-time
gh auth refresh -s project           # extra scope for Projects v2 automation
gh auth status                       # should list: gist, project, read:org, repo
```

If you had a legacy `.env`:

```bash
cd /path/to/your/repo
whilly --config migrate              # .env → whilly.toml + secrets → keyring
```

Minimal `whilly.toml` for a GitHub-backed repo:

```toml
MAX_PARALLEL = 1
MODEL = "claude-opus-4-7[1m]"
BUDGET_USD = 20                      # hard cap
AUTO_MERGE = "yes"                   # yes | claude | ask | no

[project_board]                      # optional but recommended
url = "https://github.com/users/<you>/projects/<N>"
default_repo = "<you>/<your-repo>"
```

Verify:

```bash
whilly --config show                 # merged config, tokens redacted
```

---

## 2. Scenario: «Run whilly on all GitHub issues with a label»

Most common flow. Whilly fetches every open issue with the given label, converts each into a task, and works through them.

```bash
# sanity: see the issues that would be picked up
gh issue list --label whilly:ready --state open

# one-time: make sure the Projects v2 board has the columns whilly needs
whilly --ensure-board-statuses

# one-time: put those issues on the board so you can watch them move
python3 /path/to/whilly-orchestrator/scripts/populate_board.py \
    --project <board-url> --repo <you>/<repo> --label whilly:ready

# now the main event
whilly --from-github whilly:ready --go
```

What you'll see:

1. Whilly fetches issues, writes `tasks-from-github.json`.
2. A Rich TUI dashboard opens showing active agents, queue, cost, time.
3. For every task: card on the board moves **Todo → In Progress** when the agent picks it up, **→ In Review** when the agent finishes.
4. After all tasks done: whilly pushes the branch; cards move **→ Done** per the `--post-merge` hook.

Dashboard hotkeys: `q` quit, `d` task detail, `l` logs, `t` task overview, `h` help.

---

## 3. Scenario: «Only one specific issue»

Three equivalent forms:

```bash
whilly --from-issue owner/repo/42          --go   # slash form — shell-safe
whilly --from-issue 'owner/repo#42'        --go   # '#' form, always quote in zsh/bash
whilly --from-issue https://github.com/owner/repo/issues/42 --go
```

Whilly writes `tasks-issue-owner-repo-42.json` and runs it. Idempotent — re-running the same issue ref refreshes description / acceptance criteria / priority without losing status.

---

## 4. Scenario: «Use Jira instead of (or alongside) GitHub»

Add `[jira]` to `whilly.toml`:

```toml
[jira]
server_url = "https://company.atlassian.net"
username   = "you@example.com"
token      = "keyring:whilly/jira"          # looks up OS keyring
enabled    = true                            # gates auto-close too
enable_board_sync = true                     # perform Jira transitions
```

Then:

```bash
whilly --from-jira ABC-123 --go
whilly --from-jira https://company.atlassian.net/browse/ABC-123 --go
```

While the task runs, whilly transitions the Jira ticket in step with its internal status — the mapping (overridable via `[jira.status_mapping]`):

| whilly status | Jira transition (default) |
|---|---|
| pending | To Do |
| in_progress | In Progress |
| done | In Review |
| merged | Done |
| blocked | Blocked |
| human_loop | Waiting for Customer |
| failed | Failed |
| skipped | Cancelled |

GitHub Projects v2 + Jira can be configured at the same time — whilly drives both on every status change.

---

## 5. Scenario: «I want to do the coding myself but let whilly orchestrate»

The `claude_handoff` backend turns each task into a file-based RPC:

```bash
WHILLY_AGENT_BACKEND=claude_handoff whilly --from-issue owner/repo/42 --go
```

Whilly writes `.whilly/handoff/GH-42/prompt.md` and **blocks**. In another shell (or in a Claude Code session you have open):

```bash
whilly --handoff-list                    # see pending
whilly --handoff-show GH-42              # read the brief
# ... do the work: edit code, run tests, create PR ...
whilly --handoff-complete GH-42 --status complete --message "PR #123 merged"
```

Accepted statuses:

| status | meaning | task lands as | card lands in |
|---|---|---|---|
| `complete` | done, ready to merge | done | In Review |
| `failed` | tried and couldn't finish | failed | Failed |
| `blocked` | waiting on external thing (CI / dep / decision) | blocked | On Hold |
| `human_loop` | needs a human decision | human_loop | Human Loop |
| `partial` | progress but not complete | done | In Review |

This is the visible-every-step flow — each transition on the board corresponds to a real filesystem event you can inspect.

---

## 6. Scenario: «I merge PRs through GitLab / GitHub web UI, not whilly»

Whilly's automated merge assumes it owns the git push. If you merge elsewhere, use the out-of-band hook:

```bash
whilly --post-merge tasks-from-github.json
```

Iterates every `done` task in the plan, moves its card to `Done`, and transitions its Jira ticket to `Done` (whichever is configured).

---

## 7. Common problems

| Symptom | Fix |
|---|---|
| `gh: Bad credentials (HTTP 401)` | `unset GITHUB_TOKEN && gh auth status` — if that's clean, set `WHILLY_GH_PREFER_KEYRING=1` in whilly.toml |
| `Legacy .env detected` warning each run | `whilly --config migrate` |
| Dashboard doesn't render | non-TTY stdout → auto-headless; run whilly in a real terminal, or pass `--no-worktree` to simplify |
| Card doesn't move | `gh auth refresh -s project` (Projects scope) + `whilly --ensure-board-statuses` |
| Windows: some hotkeys don't work | expected — `q/d/l/t/h` are POSIX-only; dashboard rendering still works |
| Task stuck "in_progress" across restarts | whilly resets stale in_progress tasks automatically on start |
| Budget exceeded | `WHILLY_BUDGET_USD=N` caps spend; whilly stops at 100 %, warns at 80 % |

---

## 8. Cost + safety knobs worth knowing

```toml
BUDGET_USD = 20              # hard cap ($0 = unlimited)
MAX_TASK_RETRIES = 5         # before a task is skipped
TIMEOUT = 0                  # per-plan wall clock, seconds ($0 = unlimited)
MAX_PARALLEL = 1             # 1 = sequential (recommended for handoff + demos)
RESOURCE_CHECK_ENABLED = true
MAX_CPU_PERCENT = 80
MAX_MEMORY_PERCENT = 75
```

Kill signals:

```bash
# any running whilly exits cleanly on SIGINT — just Ctrl+C
pkill -f whilly                        # nuclear option
tmux kill-session -t whilly-<TASK>     # single stuck agent (when USE_TMUX=1)
```

---

## 9. Full reference

- [`Whilly-Usage.md`](./Whilly-Usage.md) — every flag, env var, config field.
- [`whilly.example.toml`](../whilly.example.toml) — annotated config template.
- `whilly --help` — terse CLI help.
- `whilly --config show` — what values are *actually* in effect right now.
