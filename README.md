# Whilly Orchestrator

Whilly Orchestrator is a control plane for AI-assisted engineering workflows.

It turns structured engineering work from JSON plans, GitHub Issues, GitHub
Projects, Jira, and PRD/Forge intake into a deterministic, observable, and
auditable task execution pipeline.

Whilly does not position AI agents as fully autonomous developers. Instead, it
wraps agent execution in a controlled orchestration layer: task validation,
dependency checks, decision gates, Postgres-backed queueing, worker claiming,
guarded prompt construction, runner execution, state transitions, audit events,
metrics, dashboards, and human review points.

The system is optimized for issue-driven coding tasks such as bug fixes,
features, refactoring, tests, and documentation updates. It provides the
foundation for safely scaling AI-assisted development from local
single-repository workflows toward configurable multi-domain engineering
pipelines.

The long-term goal is to make Whilly a configurable project-aware orchestrator
where each project type can define its own sources, pipeline stages, quality
gates, verification steps, runners, sinks, and human-in-the-loop checkpoints.

Whilly's core value is not unrestricted autonomy, but controlled acceleration:
enabling AI agents to perform useful engineering work while preserving
traceability, reviewability, safety, and operational control.

> **Current baseline.** Whilly ships a Postgres-backed task queue, FastAPI
> control plane, local and remote workers over HTTP, append-only `events` audit
> log, web dashboard, browserless operator surfaces, SSE stream, Prometheus
> metrics, health endpoints, worker heartbeat, repo-target metadata, and project
> config plan generation. Configured verification commands can block `DONE`,
> project-config tasks emit pipeline stage audit events, and human-review
> checkpoint event models exist. Approval enforcement and configured sinks are
> still being aligned with the target documentation in
> [`docs/target/`](docs/target/). Legacy v3.x remains historical at tag
> [`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final).

[![PyPI version](https://img.shields.io/pypi/v/whilly-orchestrator.svg)](https://pypi.org/project/whilly-orchestrator/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

🇷🇺 [Краткое описание на русском](README-RU.md)

> "I'm helping — and I've read TRIZ." — Whilly Wiggum

## What Whilly Does

- Accepts work from JSON plans, GitHub Issues, GitHub Projects, Jira, and
  Forge/PRD intake.
- Normalizes each task into one model: description, dependencies, priority,
  acceptance criteria, test steps, key files, budget, and `plan_id`.
- Validates tasks before execution: vague work can be rejected or skipped,
  dependency cycles are refused, and decision gates can run in strict mode.
- Orchestrates execution from a Postgres queue using dependency readiness,
  priority, budget checks, row locking, worker claiming, and a deterministic
  state machine.
- Hands prepared prompts to runners/backends without letting agents freely pick
  tasks or rewrite the project plan.
- Records outcomes through task states, append-only events, JSONL mirrors,
  dashboard views, SSE, Prometheus metrics, health checks, and worker heartbeat.
- Supports human-in-the-loop through PR review, handoff backends, dashboards,
  issue/Jira comments, and explicit checkpoint evidence. `BLOCKED` and
  `HUMAN_LOOP` are target checkpoint concepts, not current core task statuses.

## Current Scope And Boundaries

Whilly orchestrates agents; it does not magically make agent output correct.

Current Whilly is best described as an issue-driven AI task orchestrator for one
working repository or workspace, with a Postgres-backed queue, deterministic
state transitions, worker execution, runner abstraction, audit events, and
baseline safety gates.

It already fits bug fixes, feature tasks, refactoring, test generation,
documentation updates, structured task plans, and controlled local/remote worker
execution.

The core worker loop does **not** claim all of the following as complete product
guarantees: full multi-repo execution, automatic PR review feedback loops,
mandatory CI/lint verification unless verification commands are configured,
full sandbox or VM isolation, semantic long-term memory, reliable git rollback,
or autonomous production release without human review.

## What's new in v4.6.1 (M3 of Whilly Distributed v5.0)

The M3 milestone closes the live-observability surface and ships the headline
two-host demo. Headline changes (full detail in [`CHANGELOG.md`](CHANGELOG.md);
M2 / v4.5 release notes follow below):

- **HTMX dashboard at `GET /`** — server-rendered Jinja2 page (Pico.css from CDN
  for `prefers-color-scheme` dark / light, mobile-responsive at 375 × 812),
  HTMX live-swaps tasks / workers rows on `sse:*` events from `/events/stream`,
  and falls back to a 5 s `hx-trigger="every 5s"` poll if the SSE socket fails.
  `?fragment=workers|tasks` returns just the partial table for the polling
  fallback. Zero client-side build pipeline — `htmx@1.9.12` and
  `htmx-ext-sse@2.2.4` are loaded from CDN.
- **`GET /events/stream` SSE endpoint** — `sse-starlette` `EventSourceResponse`
  wired onto a per-subscriber broker. Authenticates via per-worker / bootstrap
  / legacy-env bearer; honours `Last-Event-ID` for catch-up replay (capped at
  1000 rows with a synthetic `replay_truncated` frame on overflow); slow
  subscribers are dropped with WS close-code 1015 surfaced as an SSE `error`
  frame. A worker disconnected for ≤ 60 s catches up via `Last-Event-ID`
  without losing any committed event.
- **`GET /api/v1/tasks` JSON listing** — cursor-paginated read-only endpoint
  returning `{tasks: [...], next_cursor: ...}`. Sort: `PRIORITY_ORDER`
  (`critical=0` / `high=1` / `medium=2` / `low=3`) ASC then `id` ASC; supports
  `status` filter, `limit` 1..500 (default 100), and an opaque base64url
  cursor encoding `(priority_rank, id)` so iteration is deterministic across
  mid-flight inserts. Bearer auth required.
- **Bearer-gated Prometheus `/metrics` + extended health probes** —
  `prometheus-fastapi-instrumentator` exposes `whilly_claims_total`,
  `whilly_completes_total`, `whilly_fails_total{reason=...}`,
  `whilly_workers_online`, `whilly_claims_pending`,
  `whilly_plan_budget_remaining_usd`, and
  `whilly_claim_long_poll_duration_seconds`. `/metrics` requires
  `WHILLY_METRICS_TOKEN` (fail-closed when unset). `/health` body grows
  `db_reachable` / `listener_connected` / `queue_depth`; sibling probes
  `GET /health/live` (always 200) and `GET /health/ready` (503 when the
  listener task has exited) round out the triplet.
- **`pg_notify`-driven event flusher** — migration **011** adds the
  `whilly_notify_event()` PL/pgSQL function and `tr_events_notify` AFTER
  INSERT trigger on `events`, so every newly inserted row also fires
  `pg_notify('whilly_events', …)`. The `whilly-event-notify-listener`
  lifespan task owns a dedicated `LISTEN whilly_events` connection
  *outside* the asyncpg pool with exponential reconnect backoff (1/2/4/8/30 s)
  and feeds the per-subscriber broker that drives the SSE fan-out.
- **Two-host demo via `localhost.run` sidecar** — the headline demo runs the
  control-plane on one host (laptop or VPS) and remote workers on the other
  side of the public internet, connected via the rotating
  `https://<random>.lhr.life` URL published by the funnel sidecar. The
  sidecar publishes the URL into the `funnel_url` Postgres singleton table
  AND `/funnel/url.txt`; workers re-discover the URL via
  `WHILLY_FUNNEL_URL_SOURCE=postgres|file` and re-register idempotently with
  the same `worker_id` on URL rotation. Replaces the previously-planned
  Caddy + ACME + Tailscale Funnel stack — Tailscale is **removed** from the
  architecture (2026-05-02 pivot).

For forward-looking scope deferred to the next mission
(`whilly-v6.0-hardening`: prompt-injection guard, dangerous-command
deny-list, optional sandboxing, rollback / backup-tag tooling), see
[`library/deferred-v6-hardening.md`](library/deferred-v6-hardening.md).

## What's new in v4.1

The v4.1 cleanup mission closed seven backlog items and brought the system to a
release-ready state. Headline changes (full detail in [`CHANGELOG.md`](CHANGELOG.md)):

- **Pure decision gate** (`whilly/core/gates.py`) + `whilly plan apply --strict` —
  REJECT verdicts are skipped via `repo.skip_task` and emit `task.skipped` events
  scoped to the current `plan_id` only (cross-plan collisions are refused).
- **TRIZ analyzer** (`whilly/core/triz.py`) — `whilly plan triz <plan_id>` restores
  the useful v3 plan-level challenge as a deterministic v4 preflight over the
  imported DAG. The per-task fail-open hook still subprocesses `claude` on
  `repo.fail_task(...)` with a hard 25 s timeout and stores findings in
  `events.detail`. Opt in with `WHILLY_TRIZ_ENABLED=1`.
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

Current schema head is `013_work_intents_repo_targets`. The active Alembic
chain is:

```
001_initial_schema
002_workers_status
003_events_detail
004_per_worker_bearer
005_plan_budget
006_plan_github_ref
007_plan_prd_file
008_workers_owner_email
009_bootstrap_tokens
010_funnel_url
011_events_notify_trigger
012_pull_requests_and_pr_events
013_work_intents_repo_targets
```

## Quick start

> **⚠️ Python 3.12+ required.** `pip install whilly-orchestrator==4.6.1`
> (and every release since v4.4.0) will fail on Python 3.10 / 3.11 with
> `Could not find a version that satisfies the requirement
> whilly-orchestrator==4.6.1`. Install on a 3.12+ interpreter
> instead — e.g.
> `python3.12 -m pip install whilly-orchestrator==4.6.1`, or pin via
> `pyenv install 3.12 && pyenv local 3.12` before running `pip install`.
>
> **🐳 Docker users:** the multi-arch image is published as
> `mshegolev/whilly:4.6.1` (linux/amd64 + linux/arm64). Pull with
> `docker pull mshegolev/whilly:4.6.1` — this is the tag wired into
> [`docker-compose.control-plane.yml`](docker-compose.control-plane.yml)
> and [`docker-compose.worker.yml`](docker-compose.worker.yml) by default.

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
whilly plan triz "$PLAN_ID"           # deterministic TRIZ/challenge preflight

# 3c. Cap spend up-front (per-plan budget guard).
whilly plan create --id my-plan --name "My plan" --budget 5.00

# 3d. Pull a GitHub issue into a plan via Forge.
whilly forge intake mshegolev/whilly-orchestrator/123

# 4a. All-in-one local mode — control plane embedded in the worker process.
whilly run --plan "$PLAN_ID"

# 4a-bonus. Optional: post a "run finished" message to Slack on completion.
#           Token + channel are the only required vars; everything else has
#           sensible defaults in whilly/config.py. See docs/Whilly-Usage.md
#           for the full env-var list and message-template override.
export SLACK_ACCESS_TOKEN=xoxb-...        # or xoxe.xoxp-... (rotated user)
export WHILLY_SLACK_CHANNEL=C0B1WT58EBE

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

### Distributed (multi-host) deployment — v4.4 / M1, hardened in v4.5–v4.6.1

> The default image tag in [`docker-compose.control-plane.yml`](docker-compose.control-plane.yml)
> and [`docker-compose.worker.yml`](docker-compose.worker.yml) is
> `mshegolev/whilly:4.6.1` (multi-arch: linux/amd64 + linux/arm64). Override
> with `WHILLY_IMAGE=mshegolev/whilly:<other-tag>` if you need to pin to a
> different release.

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
| `whilly plan triz <plan_id> [--json] [--strict]` | Deterministic v4 TRIZ/challenge preflight for imported plans. |
| `whilly plan reset <plan_id>` | Reset task statuses to `pending` (soft) or wipe rows (`--hard`). |
| `whilly init "<idea>" --slug <slug>` | PRD wizard → plan import in one step. |
| `whilly run --plan <id> [--verify-command NAME=COMMAND]` | All-in-one local worker (asyncpg-direct); required verification commands block `DONE` on failure. |
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
- [`docs/Distributed-Setup.md`](docs/Distributed-Setup.md) — v4.4 multi-host deployment (VPS control-plane + laptop workers); the `localhost.run` funnel-sidecar exposure path for two-host demos is documented under the "Two-host via localhost.run" section.
- [`docs/Deploy-M2.md`](docs/Deploy-M2.md) — v4.5 (M2) public-internet exposure via the localhost.run sidecar (staging vs prod decision matrix, both topologies, env-var reference).
- [`docs/Cert-Renewal.md`](docs/Cert-Renewal.md) — v4.5 (M2) TLS / cert renewal runbook (file paths, force-renew, migration off localhost.run).
- [`docs/Token-Rotation.md`](docs/Token-Rotation.md) — v4.5 (M2) admin-token rotation runbook (per-user-leak vs admin-leak playbooks + forensic checklist).
- [`docs/Workspace-Topology.md`](docs/Workspace-Topology.md) — design-only spec for the M4 per-worker editing workspace.
- [`library/deferred-v6-hardening.md`](library/deferred-v6-hardening.md) — forward-looking scope deferred to the next mission (`whilly-v6.0-hardening`): security & isolation hardening (block A) plus rollback / safety-net tooling (block D).
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
