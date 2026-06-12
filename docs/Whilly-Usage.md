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
whilly jira import ABC-123 --run                 # single Jira ticket
whilly jira intake ABC-123                       # interactive Jira + repo choice
```

## Task sources

| Flag | Source | Notes |
|------|--------|-------|
| `--from-github <label>` | GitHub issues by label | `all`/`*`/`-` = no filter |
| `--from-issue <ref>` | one GitHub issue | `owner/repo/N`, `owner/repo#N`, or URL |
| `jira import <key>` | one Jira ticket | `ABC-123` or browse URL; auth via `[jira]`; legacy `--from-jira <key> --go` still works |
| `jira intake <key>` | one Jira ticket + repo choice | interactive same/new/other repo selection, then PRD/context, plan preflight, autonomous run, or save-only |
| `--from-project <url>` | GitHub Projects v2 board | full board items |
| `--from-issues-project <url> --repo o/r` | Projects board filtered by issue repo | |

Every source writes to an idempotent plan file (`tasks-…json`); re-running refreshes description/priority/labels without losing status.

For Jira, `whilly jira import` validates auth before fetching. If `JIRA_SERVER_URL`,
`JIRA_USERNAME`, or `JIRA_API_TOKEN` is missing, an interactive terminal prompts
for it. Missing PAT opens the Jira Cloud API token page; non-interactive runs
print the exact variables and the same URL. Use `--no-interactive-config` to
force instructions-only behavior.

`whilly jira intake` is the safer daily driver when Jira does not imply a repo.
It writes a top-level `repo_targets` block into the generated plan and sets each
Jira task's `repo_target_id`. `--action plan` runs strict apply plus TRIZ
preflight; `--action run` runs strict apply before starting a worker. For
GitLab, pass or paste the clone URL:

```bash
whilly jira intake ABC-123 --repo-url git@gitlab.example:group/project.git --action prd
```

Every intake also writes a `jira_work` block into the plan JSON. It records the
classified work kind (`feature`, `bug`, `task`, `devops`), `normal`/`hotfix`
urgency, context hashes for later Jira refresh checks, and supported Jira
comment commands such as `/whilly plan` and `/whilly run`.

Before an autonomous run, point Whilly at a local checkout when you want a
read-only code/test readiness gate:

```bash
whilly jira intake ABC-123 --repo-kind same --readiness-repo-path . --action run
```

The gate detects common test commands (`pytest`, `npm test`, `go test`,
`mvn test`, `gradle test`, `cargo test`) and unit-test files. If the verdict is
`needs_test_plan`, `needs_repo_choice`, `needs_human_context`, or `blocked`,
`action=run` stops before importing/running unless you pass
`--allow-unready-run`.

To refresh an already-known Jira issue without starting work, run one poll
cycle:

```bash
whilly jira poll ABC-123
whilly jira poll ABC-123 --persist --plan-id jira-abc-123
```

`poll` rereads the issue, comments, changelog, linked issues, remote links, and
GitLab/GitHub repo hints. With `--persist` it writes the snapshot into the
Postgres `jira_work_sessions` / `jira_work_events` history tables. A long-running
watcher can wrap this command in a scheduler or loop.

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
export PLAN_FILE=tasks.json                    # placeholder — your plan json path
whilly --config show                           # merged config, secrets redacted
whilly --config path                           # OS-native user config location
whilly --config migrate                        # legacy .env → whilly.toml + keyring
whilly --ensure-board-statuses                 # create missing Projects v2 columns
whilly --post-merge "$PLAN_FILE"               # after an out-of-band merge: flush cards/tickets to Done
```

### Remote-worker setup

A standalone worker box runs the orchestrator's worker loop against the central
control plane. The recommended sequence is:

```bash
# First-time setup (H22) — prompts for the control-plane URL + bootstrap token
# OR reads them from env vars; writes ~/.config/whilly/worker.json.
whilly worker bootstrap demo-plan                                    # interactive
WHILLY_SERVER_URL=http://control.lan:8000 \
WHILLY_WORKER_BOOTSTRAP_TOKEN=$BOOT_TK \
  whilly worker bootstrap demo-plan --non-interactive                # CI-friendly

# Subsequent runs — `launch` resolves the saved creds and starts the loop.
whilly worker launch demo-plan                                       # uses saved defaults
whilly worker launch demo-plan --model claude-opus-4-7               # H21: --model
                                                                     #      overrides
                                                                     #      cached default
whilly worker launch demo-plan --connect http://different-host:8000  # H21: --connect
                                                                     #      override

# Audit / cleanup
whilly worker list                                                   # tabular cache
whilly worker list --json                                            # raw JSON dump
whilly worker remove demo-plan                                       # drop cached entry
whilly worker remove demo-plan --connect http://control.lan:8000     # disambiguate
                                                                     # multi-server
whilly worker remove --all                                           # wipe everything
```

The `--model` and `--connect` flags on `whilly worker launch` always overwrite
the cached defaults when supplied (H21 / PR #291) — passing them is the
documented way to switch a worker box between models or control planes without
hand-editing `~/.config/whilly/worker.json`. Omitting them preserves the cached
defaults so a bare `whilly worker launch <plan>` is the steady-state command.

The optional `--tags` plumbing for worker-tag routing (F18b register-side) is
deferred to a focused follow-up PR; the `<@` SQL filter is already live (PR
#294) but every worker advertises `tags=[]` until the register-side plumbing
lands, so the filter is currently a no-op.

## Inspecting task logs

Every plan run writes per-task artifacts under `whilly_logs/`:

| File                                  | Content                                                  |
|---------------------------------------|----------------------------------------------------------|
| `whilly_logs/{task_id}.log`           | Full stdout of the Claude CLI subprocess (or tmux pipe). |
| `whilly_logs/{task_id}_prompt.txt`    | Final prompt sent to the agent.                          |
| `whilly_logs/tasks/{task_id}.events.jsonl` | Per-task structured timeline (start, retries, complete, skip). |
| `whilly_logs/whilly_events.jsonl`     | Global timeline (all tasks + plan-level events).         |
| `whilly_logs/tasks/http_trace.jsonl`  | HTTP body capture (only with `--trace`).                 |
| `whilly.log` (rotated 10 MB × 5)      | The orchestrator's own logger.                           |

Three viewer subcommands sit in front of these files — no Rich, no extra deps:

```bash
export TASK_ID=TASK-001               # placeholder — your task id
whilly logs --list                    # table: task_id, status, duration, cost, last event
whilly logs "$TASK_ID"                # prompt + events timeline + stdout for one task
whilly logs --tail "$TASK_ID"         # live follow (also -f); Ctrl-C to exit
```

`whilly logs` is a read-only viewer — it does not run the startup banner, does not
load a plan, and is safe to run while another Whilly is mid-flight in the same
directory.

### Verbose modes

By default Whilly only captures the Claude CLI's stdout (the final JSON block).
To see the HTTP traffic between Claude CLI and the Anthropic API, escalate:

```bash
whilly --verbose --tasks tasks.json   # ANTHROPIC_LOG=info — request lines, no bodies
whilly --trace   --tasks tasks.json   # ANTHROPIC_LOG=debug + http_trace.jsonl (full bodies)
```

`--trace` is loud: bodies grow logs by ~10–50× and may contain API keys and full
prompts. Use it for one-off debugging, not for routine runs. Whilly prints a red
warning banner whenever `--trace` is on and tags the event in
`whilly_events.jsonl` so you can audit the file's lineage later.

### Cleanup

`run_plan` runs an age-based cleanup at startup. Files older than
`WHILLY_LOG_TTL_DAYS` (default `14`) are deleted from `whilly_logs/` and
`whilly_logs/tasks/`. The global `whilly_events.jsonl` and the rotating
`whilly.log*` are spared — they have their own retention policies.

```bash
WHILLY_LOG_TTL_DAYS=0 whilly --tasks tasks.json   # disable cleanup entirely
WHILLY_LOG_TTL_DAYS=3 whilly --tasks tasks.json   # aggressive: keep last 3 days
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
| `WHILLY_LOG_TTL_DAYS`             | `14`                   | Delete agent logs older than N days at run start (`0` = disabled) |
| `WHILLY_VERBOSE`                  | `0`                    | Same as `--verbose`/`-v`: sets `ANTHROPIC_LOG=info` (HTTP request lines) |
| `WHILLY_TRACE_HTTP`               | `0`                    | Same as `--trace`: `ANTHROPIC_LOG=debug` + `tasks/http_trace.jsonl` (full bodies) |
| `WHILLY_ORCHESTRATOR`             | `file`                 | `file` (key-files collisions) or `llm` (LLM batching) |
| `WHILLY_VOICE`                    | `1`                    | macOS voice notifications |
| `SLACK_ACCESS_TOKEN`              | *(unset)*              | Slack bot/user token; `whilly run` posts a summary to `WHILLY_SLACK_CHANNEL` when set (also accepts `WHILLY_SLACK_ACCESS_TOKEN`) |
| `WHILLY_SLACK_CHANNEL`            | *(unset)* / demo default `C0B1WT58EBE` | Target channel id, e.g. `C0B1WT58EBE` — must be a channel the token's app/user is a member of |
| `WHILLY_SLACK_ENABLED`            | `1`                    | Kill switch; setting to `0` skips Slack even when token + channel are set |
| `WHILLY_SLACK_API_BASE_URL`       | `https://slack.com/api` | Override the API root (test stubs / on-prem proxies) |
| `WHILLY_SLACK_TIMEOUT_S`          | `5.0`                  | HTTP timeout for `chat.postMessage` |
| `WHILLY_SLACK_MESSAGE_TEMPLATE`   | *(see `whilly/config.py`)* | `str.format`-style template; placeholders match `RunCompletedEvent` fields plus `completed_at_iso` |
| `WHILLY_SLACK_WEBHOOK_URL`        | *(unset)*              | Optional Incoming Webhook for per-task demo messages from workers; otherwise demo workers can use `SLACK_ACCESS_TOKEN` |
| `WHILLY_SLACK_NOTIFY_EVENTS`      | `terminal`             | Demo webhook events: `terminal`, `started`, `all`, or `none` |
| `WHILLY_PUBLIC_BASE_URL`          | `http://127.0.0.1:8000` | Base URL used in Slack links to `/llm-ops` |
| `WHILLY_HEADLESS`                 | `0`                    | JSON stdout, no TUI (auto when stdout is not a TTY) |
| `WHILLY_DECOMPOSE_EVERY`          | `5`                    | Re-plan oversized pending tasks every N iterations |
| `WHILLY_AUTO_MERGE`               | `ask`                  | `ask` / `yes` / `claude` / `no` on plan completion |
| `WHILLY_GH_TOKEN`                 | *(unset)*              | Whilly-only GitHub token (overrides ambient) |
| `WHILLY_GH_PREFER_KEYRING`        | `0`                    | Force `gh` keyring auth even when `GITHUB_TOKEN` is set |
| `WHILLY_SUPPRESS_DOTENV_WARNING`  | `0`                    | Silence the legacy `.env` deprecation warning |

Every `WHILLY_*` variable corresponds to an equivalent `whilly.toml` field (same name, any case).
See `whilly.example.toml` for the complete template.

### Slack run-completed notifications

`whilly run` posts one Slack message per invocation, summarising the
plan id, worker id, hostname, and `WorkerStats` counters. The hook is
in :mod:`whilly.cli.run`; the adapter is :mod:`whilly.adapters.notifications.slack`.

Operator setup (one-time):

1. Create a Slack app, add the `chat:write` scope, install it into the
   target workspace.
2. Either invite the bot to channel `C0B1WT58EBE` (or your own) or use
   a user token whose owner is a member of that channel.
3. Export the env vars:

   ```bash
   export SLACK_ACCESS_TOKEN=xoxb-...        # or xoxe.xoxp-... (rotated user)
   export WHILLY_SLACK_CHANNEL=C0B1WT58EBE
   ```

The feature stays off when `SLACK_ACCESS_TOKEN` is empty
(:func:`whilly.adapters.notifications.factory.make_notifier` returns a
no-op `NullNotifier`). Slack outages and `chat.postMessage`
`{"ok": false}` responses are logged at WARNING but never change the
CLI exit code.

### Slack per-task demo notifications

Distributed demo workers can also post task-level messages. This path is
intended for `workshop-demo.sh` and supports either an Incoming Webhook or
a bot token:

```bash
export WHILLY_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
export WHILLY_SLACK_NOTIFY_EVENTS=all
export WHILLY_PUBLIC_BASE_URL=http://127.0.0.1:8000
bash workshop-demo.sh --cli opencode --workers 2 --keep-running
```

Each message includes task id, plan id, worker id, model, terminal status,
and a `/llm-ops?task_id=...` link. Webhook failures are logged and ignored.
Bot-token mode defaults to channel `C0B1WT58EBE`; override with
`WHILLY_SLACK_CHANNEL` when needed.

The summary text is a `str.format` template; override
`WHILLY_SLACK_MESSAGE_TEMPLATE` to customise the message without
touching code. Available placeholders match the
:class:`whilly.core.notifications.RunCompletedEvent` fields plus
`{completed_at_iso}`.

## Authentication Configuration

The Whilly control plane mints HMAC-signed session tokens for operator
dashboard requests. The signing key comes from the
`WHILLY_DASHBOARD_TOKEN_SECRET` environment variable.

**What it does.** Every dashboard token (the bearer the WUI/TUI presents on
HTTP requests to the control plane) is signed and verified with this secret.
A token signed by one secret cannot be verified by a different one — so the
secret directly controls who can log in and stay logged in.

**When to set it.** Always in production, and in any multi-replica deployment.
When the env var is unset or empty, `create_app` generates a per-process
random secret on startup and logs a warning. That fallback is fine for local
development (single process, restarts often) but breaks two things in prod:
(1) every restart invalidates every active session, kicking all operators
back to the login screen; (2) a multi-replica control plane mints tokens one
replica cannot verify, so requests sticky-routed to a different replica
return 401.

**How to generate a fresh value.** A 32-byte URL-safe random string is
sufficient:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

Store the output in your secret manager and inject it into the control-plane
process environment (Kubernetes Secret, systemd `EnvironmentFile=`, Docker
Compose `env_file:`, or your secret-of-choice).

**Rotation.** Rotate by overwriting the env value and restarting the control
plane. The restart is required — secrets are loaded once at `create_app`
time. All existing dashboard tokens are immediately invalidated by the
rotation; operators see a single login prompt. Pair the rotation with a
forced re-login if your incident playbook says session compromise is
suspected.

**Security posture.** Treat `WHILLY_DASHBOARD_TOKEN_SECRET` exactly like a
signing key: never commit it, never paste it into chat, never log it. Anyone
who learns the value can forge dashboard tokens for any operator email
without going through the login form. Companion knob
`WHILLY_DASHBOARD_TOKEN_TTL_SECONDS` controls token lifetime (default 24
hours); lowering it shortens the window of damage from a leaked token at
the cost of more frequent re-logins.

### SMTP magic-link delivery: `WHILLY_SMTP_*`

By default Whilly writes magic-link sign-in URLs to
`whilly_logs/whilly_events.jsonl` as `auth.magic_link.sent` events — the dev /
loopback path that operators copy from. Production deployments enable SMTP by
setting `WHILLY_SMTP_HOST`:

```
WHILLY_SMTP_HOST=smtp.example.com
WHILLY_SMTP_PORT=587                   # default; STARTTLS submission
WHILLY_SMTP_USER=...                   # optional
WHILLY_SMTP_PASSWORD=...               # optional
WHILLY_SMTP_FROM=whilly@example.com    # default whilly@<hostname>
```

The transport is async (`aiosmtplib`, hard dependency of the `server`
extras). On any SMTP error — connection refused, auth failure, bad
From, timeout — the `Mailer` fails open onto the event-log path so the
auth flow still completes; operators can recover the link from the audit
trail even when delivery is broken. The fallback event includes
`fallback_reason: "smtp_error"` so monitoring can alert on the
distinction between deliberate dev-mode and broken-prod-SMTP.

### Cluster-aware rate limiting: `WHILLY_NUM_WORKERS` and `WHILLY_REDIS_URL`

The default in-process IP rate limiter is correct for a single-process
deployment but **silently under-counts** when uvicorn spawns multiple worker
processes — each process owns its own counter. Two environment variables let
the control plane detect this and choose the right strategy at startup:

- `WHILLY_NUM_WORKERS` — integer, default `1`. The number of worker processes
  uvicorn was started with (typically passed via `--workers`).
- `WHILLY_REDIS_URL` — connection string for a shared Redis instance
  (`redis://host:port/db`). Required when `WHILLY_NUM_WORKERS > 1` if you
  want cluster-wide counting.

Decision matrix applied at `create_app` startup:

| `WHILLY_NUM_WORKERS` | `WHILLY_REDIS_URL` | Limiter | Behaviour |
|---|---|---|---|
| `1` (default) | either | `IPRateLimiter` | In-process sliding window |
| `> 1` | unset | `NullRateLimiter` + **WARNING** | **Fail-open**, always allows |
| `> 1` | set | `RedisRateLimiter` | `INCR`/`EXPIRE` counter |

The fail-open branch is deliberate: bricking the entire auth surface on a
misconfigured cluster is worse than missing some rate-limit counting. The
WARNING is loud in startup logs; production deployments should treat it as
configuration drift and either reduce `WHILLY_NUM_WORKERS` to `1` or supply
`WHILLY_REDIS_URL`. `redis-py>=5` must be installed when `WHILLY_REDIS_URL`
is set; if missing, `create_app` raises `RuntimeError` early rather than
deferring the failure to the first login attempt.

## Operator Dashboard Parity

The active browser WUI and browserless TUI expose the same canonical operator
surfaces: Overview, Compliance, Plans/Tasks, Workers, and Events. Shared surface
copy is `1-5=switch`.

Active worker controls use `/api/v1/admin/workers/*`; active review decisions
use `/api/v1/tasks/*/human-review`. `_logs.html` remains a routeable
noncanonical fragment behind `?fragment=logs`, but it is not visible navigation
until TUI parity expands. `_admin.html` and `_prd.html` are quarantined inactive
fragments because their old `/admin/*` and `/prd/*` controls are not supported
active WUI routes.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| q | Quit the TUI / stop WUI live updates |
| r | Refresh |
| R | Resume workers |
| 1-5 | Switch Overview, Compliance, Plans/Tasks, Workers, Events |
| / | Focus filter |
| p | Pause workers |
| j / k | Select next/previous review gap on Compliance |
| a / x / c | Approve, reject, or request changes for the selected review gap |

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
  core/notifications.py     RunCompletedEvent + NotificationPort (pure)
  adapters/notifications/   Slack + Null impls; `make_notifier(cfg)` factory
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

## Live smoke

Live smoke commands run a series of **read-only** integration checks against
real credentials and live infrastructure. They are safe to run against
production systems — no comments are posted, no transitions are made, no
writes occur.

Each run exits with a structured summary and writes a redacted JSON report so
you have evidence of what was checked and when.

### Jira smoke

**Required env vars**

| Variable | Description |
|----------|-------------|
| `JIRA_SERVER_URL` | Full base URL, e.g. `https://company.atlassian.net` |
| `JIRA_USERNAME` | Basic-auth email address (Cloud basic auth only) |
| `JIRA_API_TOKEN` | Jira Cloud API token (or PAT for Server/DC) |

**Jira Server / Data Center:** set `JIRA_AUTH_SCHEME=bearer` (PAT auth, no
username needed) and `JIRA_API_VERSION=2` — Server/DC instances serve
`/rest/api/2/` and answer `/rest/api/3/` requests with an HTML login page.

**Command**

```bash
export JIRA_SERVER_URL=https://company.atlassian.net
export JIRA_USERNAME=you@example.com
export JIRA_API_TOKEN=$JIRA_API_TOKEN_VALUE

whilly jira smoke --issue PROJECT-123
```

**What it checks:** auth (whoami), issue fetch, comments, changelog, remote
links, and classify. All six checks run even when an earlier one fails, so
you get a full picture on each invocation. The comments/changelog/remote-links
checks verify the data was fetched without error and has the expected shape;
the report records what was actually verified (e.g. `fetched 2 comments`) —
empty collections on a quiet issue still pass.

**Optional flags**

| Flag | Description |
|------|-------------|
| `--timeout N` | Per-request timeout in seconds (default 15) |
| `--persist` | Append a best-effort DB audit event when `WHILLY_DATABASE_URL` is set (persist problems never change the exit code) |
| `--json` | Print full report JSON to stdout instead of the human summary |

### GitLab smoke

**Required env vars**

| Variable | Description |
|----------|-------------|
| `GITLAB_URL` | GitLab base URL, e.g. `https://gitlab.example.com` |
| `GITLAB_TOKEN` | Personal access token with `read_api` scope (highest priority) |

Token resolution order: `GITLAB_TOKEN` → `GITLAB_API_TOKEN` →
`WHILLY_GITLAB_API_TOKEN` → `glab config get token` CLI fallback.

**Command**

```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=$GITLAB_TOKEN_VALUE

whilly gitlab smoke --repo-url https://gitlab.example.com/group/project.git
```

**What it checks:** auth (`/api/v4/user`), project access
(`/api/v4/projects/{path}`), and repo-hint validation (confirming the
project's recorded path matches the requested URL). `--repo-url` must be
the `https://` clone URL — SSH-style `git@host:path` values are rejected.

**Optional flags**

| Flag | Description |
|------|-------------|
| `--timeout N` | Per-request timeout in seconds (default 15) |
| `--json` | Print full report JSON to stdout instead of the human summary |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All checks passed |
| `1` | One or more checks failed |
| `2` | Configuration missing (env vars not set) |

### Report location

Every run writes a redacted JSON report to:

```
whilly_logs/smoke/jira-smoke-{timestamp}.json   # Jira
whilly_logs/smoke/gitlab-smoke-{timestamp}.json  # GitLab
```

Reports contain per-check pass/fail results, durations, and a redacted target
(hostname only — no tokens, DSNs, or full URLs with credentials).

**DB audit events (`whilly jira smoke --persist`):** A best-effort audit
event is appended to the database only when `WHILLY_DATABASE_URL` is set.
The report file is always written regardless of database availability.
`whilly gitlab smoke` does not support `--persist`.

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
