# Whilly v3 → v4 Migration Guide

> ⚠️ **no backwards compat.** v4 is a wholesale rewrite. (Per PRD Appendix B.) There is no
> in-place upgrade. v3.x state files (`.whilly_state.json`,
> `.whilly_workspaces/`, `whilly_logs/`) are not read by v4. The plan
> JSON schema is mostly compatible (see "Plan format" below), but every
> other interface — env vars, CLI flags, deployment shape, on-disk paths
> — has changed.
>
> This doc walks through the migration path: what to keep, what to throw
> away, and what to re-derive.

## Scope

| Category | v3.x | v4.0 |
|---|---|---|
| Runtime topology | Single process driving Claude CLI in tmux/worktree panes | Postgres + FastAPI control plane + N remote workers over HTTP |
| Task storage | `tasks.json` is the source of truth | Postgres `tasks` table; `tasks.json` is import format only |
| State persistence | `.whilly_state.json` (resume support) | `tasks` table + `events` audit log |
| Concurrency | tmux panes / git worktrees | optimistic locking + `SKIP LOCKED` claim + visibility-timeout sweep |
| Failure recovery | manual `--reset` / re-run | sweep flips stale claims back to PENDING; peer worker re-claims |
| Python | 3.10+ | **3.12+** (TaskGroup, @override) |
| Install | `pip install whilly-orchestrator` (everything) | `[worker]` / `[server]` / `[all]` extras split |
| Logs | `whilly_logs/whilly_events.jsonl` | `events` table (audit log), `whilly dashboard` (live) |

## Decision: stay on v3 or move to v4?

**Stay on v3 (tag `v3-final`)** if any of:

* You run a single laptop / single-VM workflow and don't need distributed
  workers.
* Your Postgres ops budget is zero — v4 needs an actual database to talk to.
* You depend on the v3 CLI surface (tmux panes, interactive menu, plan-level
  workspace, `--prd-wizard`, `--init`, etc.) — none of this ships on v4.
* You're inside a Python 3.10/3.11 deployment that you can't bump.

The v3.x line stays on PyPI; bugfix releases will be tagged `3.3.x`.

**Move to v4** if any of:

* You want to run more than one worker against the same task plan (SC-1).
* You want SIGKILL'd workers to recover automatically (SC-2).
* You want a control plane on one VM and workers on another (SC-3).
* You want a static type-strict + 100%-coverage core domain layer (SC-5/SC-6).
* You're starting a fresh deployment and want the long-term-supported shape.

## Step-by-step migration

### 1. Decommission v3.x state

Stop any running `whilly` / `whilly --resume` processes. Then preserve and
remove the v3.x runtime artefacts:

```bash
# Optional: archive v3 state in case you need to look at it later
tar czf whilly-v3-archive.tar.gz \
    .whilly_state.json \
    .whilly_workspaces/ \
    .whilly_worktrees/ \
    whilly_logs/

# Remove (v4 will not read any of these)
rm -rf .whilly_state.json .whilly_workspaces/ .whilly_worktrees/ whilly_logs/
```

`tasks.json` (or `.planning/*tasks*.json`) — **keep**. It's the import
format on v4.

### 2. Bump Python

v4 requires Python ≥3.12. Check:

```bash
python --version  # must be 3.12+
```

If not, install via pyenv / asdf / your distro:

```bash
pyenv install 3.12.7 && pyenv local 3.12.7
# or
asdf install python 3.12.7 && asdf local python 3.12.7
```

### 3. Install v4 with the right extras

Pick the install closure that matches the deployment role:

```bash
# All-in-one local box (developer laptop, demo VM)
pip install -e '.[all]'

# Control-plane VM (Postgres + FastAPI)
pip install -e '.[server]'

# Worker VM (httpx-only — no Postgres / FastAPI bloat)
pip install whilly-worker
# or, equivalently, pulled directly from extras:
pip install whilly-orchestrator[worker]
```

The slim `[worker]` install is what lets a worker box stay on a tiny base
image — no asyncpg, no FastAPI, no SQLAlchemy. See [`whilly_worker/README.md`](../whilly_worker/README.md)
for the full rationale.

### 4. Stand up Postgres + apply migrations

```bash
# Local: docker-compose ships a postgres:15-alpine + whilly DB / role
docker compose up -d
export WHILLY_DATABASE_URL=postgresql://whilly:whilly@localhost:5432/whilly

# Production: point at your Postgres cluster
export WHILLY_DATABASE_URL=postgresql://USER:PASS@HOST:5432/DBNAME

# Apply schema (creates plans, tasks, events, workers tables)
alembic upgrade head
```

### 5. Re-import your plan

```bash
# Old: whilly --tasks tasks.json
# New:
whilly plan import path/to/tasks.json

# Verify
whilly plan show <plan_id>     # ASCII DAG of tasks + dependencies
```

The v4 plan JSON schema is mostly the same as v3's. Required task fields:
`id`, `status`, `priority`, `description`. Optional but commonly used:
`dependencies`, `key_files`, `acceptance_criteria`, `test_steps`,
`prd_requirement`. Extra fields are tolerated and ignored. Invalid plans
fail at `plan import` with a structured error pointing at the offending
task id.

### 6. Run

```bash
# Local (control plane embedded in worker — single process)
whilly run --plan <plan_id>

# Distributed
# a) on the control-plane box
export WHILLY_WORKER_TOKEN=$(openssl rand -hex 32)
export WHILLY_WORKER_BOOTSTRAP_TOKEN=$(openssl rand -hex 32)
uvicorn 'whilly.adapters.transport.server:create_app' --factory --port 8000

# b) on each worker box
whilly-worker --connect https://control.example.com:8000 \
              --token "$WHILLY_WORKER_TOKEN" \
              --plan <plan_id>
```

### 7. Watch progress

```bash
whilly dashboard --plan <plan_id>     # Rich Live TUI
```

## Env-var mapping

Variables marked **removed** are no longer read by v4 — set them or not,
no effect.

| v3 var | v4 equivalent | Notes |
|---|---|---|
| `WHILLY_MAX_PARALLEL` | (removed) | Concurrency is per-worker; spawn N workers instead |
| `WHILLY_BUDGET_USD` | (deferred) | Budget guards land in v4.1 |
| `WHILLY_MODEL` | `WHILLY_CLAUDE_MODEL` | Same value, renamed for clarity |
| `WHILLY_USE_TMUX` | (removed) | tmux runner deleted |
| `WHILLY_USE_WORKSPACE` | (removed) | Plan-level workspace deleted |
| `WHILLY_HEADLESS` | (still works) | `whilly run` is non-interactive by default |
| `WHILLY_TIMEOUT` | (removed) | Use process supervisor's timeout |
| `WHILLY_STATE_FILE` | (removed) | State lives in Postgres |
| `WHILLY_LOG_DIR` | (deferred) | Audit lives in `events` table; file logs land in v4.1 |
| (new) | `WHILLY_DATABASE_URL` | Postgres DSN |
| (new) | `WHILLY_CONTROL_URL` | Control-plane base URL (worker side) |
| (new) | `WHILLY_WORKER_TOKEN` | Per-worker bearer token |
| (new) | `WHILLY_WORKER_BOOTSTRAP_TOKEN` | Cluster-join secret |
| (new) | `WHILLY_PLAN_ID` | Plan id worker draws from |
| (new) | `WHILLY_WORKER_ID` | Worker identity (default: `<host>-<8-hex>`) |
| `CLAUDE_BIN` | `CLAUDE_BIN` | Same name, same semantics |

## CLI surface mapping

| v3 invocation | v4 equivalent |
|---|---|
| `whilly --tasks tasks.json` | `whilly plan import tasks.json && whilly run --plan <id>` |
| `whilly --all` | spawn N `whilly-worker --plan <id>` processes |
| `whilly --resume` | (no longer needed; state survives in Postgres) |
| `whilly --reset PLAN.json` | (no v4 equivalent yet — DELETE FROM events/tasks WHERE plan_id=...) |
| `whilly --headless` | `whilly run` is headless by default |
| `whilly --init "desc"` | (deferred to v4.1; PRD wizard is v3-only) |
| `whilly --prd-wizard` | (deferred to v4.1) |
| `whilly --workspace` / `--worktree` | (removed) |
| (new) | `whilly plan show <plan_id>` — ASCII DAG |
| (new) | `whilly plan export <plan_id> > tasks.json` |
| (new) | `whilly dashboard --plan <plan_id>` — Rich Live TUI |
| (new) | `whilly-worker --connect URL --token X --plan <id>` |

## Breaking changes summary

1. **Python 3.12+** required (was 3.10+).
2. **Postgres** is a hard runtime dependency for the control plane (was none).
3. **State files (`.whilly_state.json` etc.) are not read.** Plans must be
   re-imported via `whilly plan import`.
4. **tmux runner / git worktree workspace removed.** Concurrency comes
   from spawning multiple workers.
5. **Single `pip install whilly-orchestrator` no longer installs everything.**
   Pick `[worker]` / `[server]` / `[all]` based on role.
6. **State machine: `(COMPLETE, CLAIMED) → DONE` is a valid edge.**
   `claimed → done` skipping IN_PROGRESS is now legal — required for the
   remote worker shape (no `/tasks/{id}/start` HTTP endpoint).
7. **Audit log lives in `events` table**, not `whilly_logs/whilly_events.jsonl`.
8. **Console scripts**: `whilly` is now a sub-command dispatcher (`plan`,
   `run`, `dashboard`); `whilly-worker` is a separate binary for the
   remote-worker entry point.
9. **Workers register out-of-band.** v3 had no concept of registration;
   v4 expects a worker row in the `workers` table before the first
   claim. The `/workers/register` endpoint mints one; or (for tests /
   demo) seed it via SQL — see `docs/demo-remote-worker.sh`.

## Pointers

* Architecture: [`Whilly-v4-Architecture.md`](Whilly-v4-Architecture.md)
* HTTP protocol: [`Whilly-v4-Worker-Protocol.md`](Whilly-v4-Worker-Protocol.md)
* Release checklist: [`v4.0-release-checklist.md`](v4.0-release-checklist.md)
* PRD (full design rationale): [`PRD-refactoring-1.md`](PRD-refactoring-1.md)
