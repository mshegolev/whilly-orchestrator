# Whilly Orchestrator

> 🚀 **v4.1 — release-ready.** Whilly is a distributed orchestrator for AI coding agents:
> Postgres-backed task queue, FastAPI control plane, remote workers over HTTP, and an
> append-only `events` audit log. The legacy v3.x single-process loop has been retired —
> it lives at tag [`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final)
> for teams that still need it. There is **no backwards compatibility** with v3.x runtime
> state — see [`docs/Whilly-v4-Migration-from-v3.md`](docs/Whilly-v4-Migration-from-v3.md).

[![PyPI version](https://img.shields.io/pypi/v/whilly-orchestrator.svg)](https://pypi.org/project/whilly-orchestrator/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

🇷🇺 [Краткое описание на русском](README-RU.md)

> "I'm helping — and I've read TRIZ." — Whilly Wiggum

## What's new in v4.1

The v4.1 cleanup mission closed seven backlog items and brought the system to a
release-ready state. Headline changes (full detail in [`CHANGELOG.md`](CHANGELOG.md)):

- **Pure decision gate** (`whilly/core/gates.py`) + `whilly plan apply --strict` —
  REJECT verdicts are skipped via `repo.skip_task` and emit `task.skipped` events
  scoped to the current `plan_id` only (cross-plan collisions are refused).
- **Per-task TRIZ analyzer** (`whilly/core/triz.py`) — replaces the v3 plan-level
  analyzer. Subprocess to `claude` with a hard 25 s timeout (under the 30 s claim
  visibility window), fail-open on missing CLI / timeout / malformed JSON, and an
  `events.detail` jsonb column for findings. Opt in with `WHILLY_TRIZ_ENABLED=1`.
- **Per-worker bearer auth** (migration `004_per_worker_bearer`) — `workers.token_hash`
  is now nullable with a partial UNIQUE on non-null values. The `whilly worker register`
  CLI mints plaintext bearers. Bearer identity is bound to the request `worker_id`
  (403 on mismatch). Register stays bootstrap-gated by `WHILLY_WORKER_BOOTSTRAP_TOKEN`.
  `WHILLY_WORKER_TOKEN` is **deprecated** — one-shot warning, suppress with
  `WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`.
- **Plan budget guard** (migration `005_plan_budget`) — `plans.budget_usd` /
  `plans.spent_usd`; `events.plan_id NOT NULL` and `events.task_id` nullable to
  support the plan-level sentinel. `whilly plan create --budget USD` caps spend
  strictly (claim gate uses `<`); `plan.budget_exceeded` is emitted exactly once
  per crossing with `{plan_id, budget_usd, spent_usd, crossing_task_id, reason,
  threshold_pct: 100}`. Concurrent claims serialise on `FOR UPDATE OF t SKIP LOCKED`.
- **Lifespan event flusher** (`whilly/api/event_flusher.py`) — bounded asyncio.Queue
  flushed on a 100 ms timer **or** 500-row threshold (whichever fires first via
  `asyncio.Event`-driven wake), `tempfile + os.replace` checkpoint, graceful drain
  on `SIGTERM`/`SIGINT`. TRIZ events route through the flusher when
  `TaskRepository(event_flusher=...)` is wired; local-worker callers fall back to
  direct `INSERT`.
- **Forge intake** (migrations `006_plan_github_ref`, `007_plan_prd_file`) —
  `whilly forge intake owner/repo/N` shells out to `gh issue view`, normalises the
  issue into a Whilly plan via the PRD-wizard pipeline, persists it with
  `plans.github_issue_ref` + `plans.prd_file`, then flips the issue label
  `whilly-pending → whilly-in-progress`. Idempotent re-runs are enforced by a
  partial UNIQUE; concurrent intake stays at-most-once on the GitHub side via a
  creator-vs-loser flag. `plan.created` event emitted in the same transaction as
  the inserts. `GET /api/v1/plans/{id}` exposes `github_issue_ref` and `prd_file`.
- **Cleanup** — `whilly/cli_legacy.py` deleted; `WHILLY_WORKTREE` and
  `WHILLY_USE_WORKSPACE` are silent no-ops on v4. `task.created` events emitted per
  inserted task row; `plan.applied` events emitted once per `whilly plan apply`
  invocation with `{tasks_count, skipped_count, warned_count, strict}`. The full
  Flow A fingerprint (gate verdict → state-machine transition → audit row) is
  observable within 200 ms.

## Architecture

Three boxes / containers / VMs talk to each other:

```
┌─────────────────────────┐         ┌────────────────────────┐         ┌────────────────────────┐
│  Postgres 15+           │ ◄────── │  Control plane         │ ◄────── │  whilly-worker         │
│  plans / tasks /        │ asyncpg │  FastAPI + asyncpg     │  HTTP   │  httpx → claim → run   │
│  workers / events       │         │  + lifespan flusher    │  TLS    │  Claude CLI subprocess │
└─────────────────────────┘         └────────────────────────┘         └────────────────────────┘
```

Postgres is the single source of truth — operational tables (`plans`, `tasks`,
`workers`) plus an append-only `events` audit log. The state machine
(`whilly/core/state_machine.py`) gates every transition server-side. Workers are
stateless pollers; the control plane is a transaction shaper. See
[`docs/Whilly-v4-Architecture.md`](docs/Whilly-v4-Architecture.md) for the
hexagonal layout, the `core` / `adapters` split, scheduling, and lock semantics.

## Migration chain

```
001_initial_schema
 └→ 002_workers_status
     └→ 003_events_detail        (events.detail jsonb NULL — TASK-104b)
         └→ 004_per_worker_bearer   (workers.token_hash nullable + partial UNIQUE — TASK-101)
             └→ 005_plan_budget       (plans.budget_usd / spent_usd; events.plan_id NOT NULL — TASK-102)
                 └→ 006_plan_github_ref   (plans.github_issue_ref + partial UNIQUE — TASK-108a)
                     └→ 007_plan_prd_file     (plans.prd_file — TASK-108a)
```

Every migration is round-trippable via `alembic upgrade head → downgrade base
→ upgrade head` with byte-equal final schema.

## Quick start

```bash
# 0. Set placeholders so the block below is copy-paste-runnable as-is.
export PLAN_FILE=examples/demo/tasks.json
export WHILLY_CONTROL_URL=https://control.example.com:8000
export PLAN_ID=demo                   # placeholder convention — your plan id

# 1. Postgres on the control-plane box (or any reachable host)
docker compose up -d                  # boots postgres:15-alpine via docker-compose.yml
export WHILLY_DATABASE_URL=postgresql://whilly:whilly@localhost:5432/whilly

# 2. Install + apply migrations
pip install -e '.[server,dev]'        # control-plane install closure
alembic upgrade head                  # applies every revision through 007

# 3a. Have an idea, not a plan? PRD wizard generates both.
whilly init "build a CLI tool for monitoring API endpoints" --slug api-monitor
#   → docs/PRD-api-monitor.md saved + plan 'api-monitor' imported into Postgres

# 3b. Already have tasks.json? Import directly. Add --strict to skip
#     decision-gate REJECTs as they're imported.
whilly plan import "$PLAN_FILE"
whilly plan apply "$PLAN_FILE" --strict
whilly plan show "$PLAN_ID"           # ASCII DAG of the imported plan

# 3c. Cap spend up-front (per-plan budget guard).
whilly plan create --id my-plan --name "My plan" --budget 5.00

# 3d. Pull a GitHub issue into a plan via Forge.
whilly forge intake mshegolev/whilly-orchestrator/123

# 4a. All-in-one local mode — control plane embedded in the worker process.
whilly run --plan "$PLAN_ID"

# 4b. Distributed mode — control plane + remote worker on different hosts.
#     a) on the control-plane box, mint the cluster bootstrap secret:
export WHILLY_WORKER_BOOTSTRAP_TOKEN=$(openssl rand -hex 32)

#     b) on the worker box (only needs httpx — pull the slim install):
pip install whilly-orchestrator[worker]
WORKER_TOKEN=$(whilly worker register \
    --connect "$WHILLY_CONTROL_URL" \
    --bootstrap-token "$WHILLY_WORKER_BOOTSTRAP_TOKEN" \
    --plan "$PLAN_ID")
whilly-worker \
    --connect "$WHILLY_CONTROL_URL" \
    --token "$WORKER_TOKEN" \
    --plan "$PLAN_ID"

# 5. Watch progress live
whilly dashboard --plan "$PLAN_ID"    # Rich Live TUI over the tasks table
```

```bash
# Run in a second terminal — long-running:
uvicorn 'whilly.adapters.transport.server:create_app' --factory --port 8000
```

A complete reproducible single-host demo (Postgres + control plane + remote
worker, all on loopback) lives in [`docs/demo-remote-worker.sh`](docs/demo-remote-worker.sh).

### Distributed (multi-host) deployment — v4.4 / M1

For split-host deployments — control-plane on a VPS, workers on laptops —
two new compose files (additive; the single-host
[`docker-compose.demo.yml`](docker-compose.demo.yml) is unchanged):

```bash
# Set the public IP of the control-plane VPS so the example below is
# copy-paste-runnable as-is.
export VPS_IP=203.0.113.99

# VPS (control-plane only)
docker-compose -f docker-compose.control-plane.yml up -d

# Laptop (one-line bootstrap; stores per-worker bearer in OS keychain)
whilly worker connect http://$VPS_IP:8000 \
    --bootstrap-token "$WHILLY_WORKER_BOOTSTRAP_TOKEN" \
    --plan demo \
    --insecure   # dev-only: opts out of the loopback-only HTTP guard
```

> ⚠️ `--insecure` here is a **dev-only loopback-bypass**: the
> `whilly-worker` URL-scheme guard otherwise rejects plain HTTP to a
> non-loopback host. For production, use **M2 (v4.5)**'s
> localhost.run sidecar — it publishes a real
> `https://<random>.lhr.life` URL with an upstream Let's Encrypt
> cert at the edge, no `--insecure` needed. Full walkthrough +
> staging-vs-prod decision matrix:
> [`docs/Deploy-M2.md`](docs/Deploy-M2.md). Adjacent runbooks:
> [`docs/Cert-Renewal.md`](docs/Cert-Renewal.md) (TLS / cert
> lifecycle) and [`docs/Token-Rotation.md`](docs/Token-Rotation.md)
> (per-user vs admin token-leak playbooks).

Full walkthrough — including `WHILLY_BIND_HOST` / `WHILLY_USE_CONNECT_FLOW`
options, the laptop-side Docker variant via
[`docker-compose.worker.yml`](docker-compose.worker.yml), audit-log
verification, and the M4 workspace topology design — lives in
[`docs/Distributed-Setup.md`](docs/Distributed-Setup.md) and
[`docs/Workspace-Topology.md`](docs/Workspace-Topology.md). For the
v4.5 / M2 public-internet-exposure path (localhost.run sidecar +
per-operator bootstrap CLI + admin token rotation runbooks), see
[`docs/Deploy-M2.md`](docs/Deploy-M2.md),
[`docs/Cert-Renewal.md`](docs/Cert-Renewal.md), and
[`docs/Token-Rotation.md`](docs/Token-Rotation.md).

## CLI surface

`whilly <command>` dispatches to a sub-CLI; `whilly --help` prints the routing
block.

| Command | Purpose |
|---|---|
| `whilly plan import <file>` | Validate + persist a plan JSON to Postgres (idempotent on `plan_id`). |
| `whilly plan apply <file> [--strict]` | Import + run the decision gate. With `--strict`, REJECTs are skipped via `repo.skip_task` and emit `task.skipped` events. |
| `whilly plan create --id <id> --name <name> [--budget USD]` | Mint an empty plan with an optional spend cap. |
| `whilly plan export <plan_id>` | Round-trip canonical JSON to stdout. |
| `whilly plan show <plan_id>` | ASCII dependency-graph render with status badges. |
| `whilly plan reset <plan_id>` | Reset task statuses to `pending` (soft) or wipe rows (`--hard`). |
| `whilly init "<idea>" --slug <slug>` | PRD wizard → plan import in one step. |
| `whilly run --plan <id>` | All-in-one local worker (asyncpg-direct). |
| `whilly dashboard --plan <id>` | Rich Live TUI over the tasks table. |
| `whilly worker register --connect <url> --bootstrap-token <tok>` | Mint a per-worker bearer (TASK-101). |
| `whilly-worker --connect <url> --token <bearer> --plan <id>` | Standalone remote-worker entry (httpx-only closure). |
| `whilly forge intake owner/repo/N` | GitHub Issue → Whilly plan + label transition. |

## Configuration

Almost everything is controlled by env vars (see [`whilly/config.py`](whilly/config.py)
for the full set; `whilly --help` per subcommand for flag details).

| Variable | Purpose |
|---|---|
| `WHILLY_DATABASE_URL` | asyncpg DSN — required for the control plane and `whilly run`. |
| `WHILLY_WORKER_BOOTSTRAP_TOKEN` | Required for `POST /workers/register` and `whilly worker register`. |
| `WHILLY_WORKER_TOKEN` | **Deprecated** legacy shared bearer; one-shot warning. Suppress with `WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`. Use per-worker bearers instead. |
| `WHILLY_TRIZ_ENABLED` | Opt-in to the per-task TRIZ analyzer hook on FAIL transitions. |
| `WHILLY_RUN_LIVE_LLM` | Gate live `claude` CLI smoke tests (skipped by default). |
| `WHILLY_BUDGET_USD` | Legacy global cap; per-plan budgets via `whilly plan create --budget` are the v4.1 way. |
| `WHILLY_CLAUDE_PROXY_URL` | Inject `HTTPS_PROXY`/`NO_PROXY` into the spawned `claude` env only. See [`docs/Whilly-Claude-Proxy-Guide.md`](docs/Whilly-Claude-Proxy-Guide.md). |
| `CLAUDE_BIN` | Path to the `claude` CLI binary (default: `claude` on `PATH`). |

`WHILLY_WORKTREE` and `WHILLY_USE_WORKSPACE` are silent no-ops — the v3
worktree / workspace machinery was removed in TASK-107.

## Install

Two install closures, picked by deployment shape:

```bash
# Control-plane box (FastAPI + asyncpg + alembic + sqlalchemy)
pip install 'whilly-orchestrator[server]'

# Remote-worker box (httpx-only — no asyncpg / FastAPI)
pip install 'whilly-orchestrator[worker]'

# Both, e.g. for a single-host demo or CI
pip install 'whilly-orchestrator[all]'

# Contributor / dev (editable, with ruff / mypy / pytest / testcontainers)
pip install -e '.[dev]'
```

The `.importlinter` `core-purity` contract enforces the split: `whilly.core`
cannot import asyncpg, fastapi, httpx, subprocess, uvicorn, or alembic — CI
fails on regression. A worker VM that installs `[worker]` will never pull
`asyncpg` / `fastapi` by accident.

Both server and worker shapes need [Claude CLI](https://docs.claude.com/en/docs/claude-code)
on `PATH` (or `CLAUDE_BIN`) to actually run agents.

## Development

```bash
pip install -e '.[dev]'
ruff check whilly tests              # lint (CI-equivalent)
ruff format --check whilly tests     # format check
mypy --strict whilly/core/           # strict types on the pure domain layer
lint-imports                          # core-purity contract
pytest -q                             # unit + integration (≈1530+ tests)
alembic upgrade head                  # apply the migration chain on a dev DB
```

Test layout:

- `tests/unit/` — pure, no DB.
- `tests/integration/` — testcontainers + asyncpg; per-test ephemeral Postgres.

Live tests gated by `WHILLY_RUN_LIVE_LLM=1` (live `claude` smoke for the TRIZ
analyzer) are skipped by default.

Conventions in [`CLAUDE.md`](CLAUDE.md): line length 120, target `py312`, ruff
is the source of truth, structured logging via `logging.getLogger(__name__)`,
deprecation via `log.warning(...)` + env-flag suppression (not Python's
`DeprecationWarning`).

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — coding conventions and architecture pointers.
- [`docs/Whilly-v4-Architecture.md`](docs/Whilly-v4-Architecture.md) — hexagonal layout, scheduling, locks.
- [`docs/Whilly-v4-Worker-Protocol.md`](docs/Whilly-v4-Worker-Protocol.md) — HTTP wire protocol, auth, long-polling.
- [`docs/Distributed-Setup.md`](docs/Distributed-Setup.md) — v4.4 multi-host deployment (VPS control-plane + laptop workers).
- [`docs/Deploy-M2.md`](docs/Deploy-M2.md) — v4.5 (M2) public-internet exposure via the localhost.run sidecar (staging vs prod decision matrix, both topologies, env-var reference).
- [`docs/Cert-Renewal.md`](docs/Cert-Renewal.md) — v4.5 (M2) TLS / cert renewal runbook (file paths, force-renew, migration off localhost.run).
- [`docs/Token-Rotation.md`](docs/Token-Rotation.md) — v4.5 (M2) admin-token rotation runbook (per-user-leak vs admin-leak playbooks + forensic checklist).
- [`docs/Workspace-Topology.md`](docs/Workspace-Topology.md) — design-only spec for the M4 per-worker editing workspace.
- [`docs/Whilly-v4-Migration-from-v3.md`](docs/Whilly-v4-Migration-from-v3.md) — env-var mapping and breaking changes.
- [`docs/Whilly-Init-Guide.md`](docs/Whilly-Init-Guide.md) — `whilly init` PRD-wizard flow.
- [`docs/Whilly-Claude-Proxy-Guide.md`](docs/Whilly-Claude-Proxy-Guide.md) — Claude CLI through HTTPS proxy / SSH tunnel.
- [`CHANGELOG.md`](CHANGELOG.md) — full release notes.

## Credits

- Technique lineage: [Ghuntley's original Ralph Wiggum loop post](https://ghuntley.com/ralph/) — the
  pattern Whilly descends from.
- "I'm helping!" stamina, plus a Decision Gate, per-task TRIZ, and a PRD wizard
  on top — Ralph's smarter brother.

## License

MIT — see [LICENSE](LICENSE).
