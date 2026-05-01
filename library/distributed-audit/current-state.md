# Whilly v4.3.1 — Distributed-Orchestrator Audit (Current State)

> Audit performed against `main` @ `1093009` (v4.3.1). All file:line refs
> are anchored to that commit.

## 1. Worker Lifecycle & Registration

The remote-worker bootstrap and steady-state are **fully implemented** and
already split into the two-token model expected by a multi-tenant cluster.

### Two distinct token surfaces
- **Cluster-join (bootstrap) secret** — `WHILLY_WORKER_BOOTSTRAP_TOKEN`
  authenticates **only** `POST /workers/register`.
  Defined: `whilly/adapters/transport/auth.py:103` (`BOOTSTRAP_TOKEN_ENV`).
  Validated: `make_bootstrap_auth(...)` factory, `auth.py:474-491`.
- **Per-worker bearer** — `WHILLY_WORKER_TOKEN` carries every steady-state
  RPC (claim/complete/fail/heartbeat/release).
  Validated against `workers.token_hash` (SHA-256), `auth.py:329-449`.
- A legacy "shared bearer" fallback exists for one minor version
  (`auth.py:84-99`); when the env var is set, all RPCs accept that
  cluster-wide token with a one-shot deprecation warning. This means
  cross-worker isolation is opt-in (`_require_token_owner`,
  `whilly/adapters/transport/server.py:1394-1429` rejects only when the DB
  resolved a real worker id).

### Registration RPC
`POST /workers/register` (`server.py:940-988`):
1. Server mints `worker_id = "w-<urlsafe(8)>"` (`server.py:265-274`).
2. Server mints plaintext bearer = `secrets.token_urlsafe(32)`
   (`server.py:255-261, 970`).
3. Persists `(worker_id, hostname, sha256(token))` via
   `TaskRepository.register_worker` — schema `whilly/adapters/db/schema.sql:23-44`,
   SQL `whilly/adapters/db/repository.py:608-612` (`_INSERT_WORKER_SQL`).
4. Plaintext returned **once** in the 201 body (`RegisterResponse`,
   `whilly/adapters/transport/schemas.py`).

### CLI surface
- Bootstrap one-shot: `whilly worker register --connect URL --bootstrap-token X --hostname H`
  (`whilly/cli/worker.py:436-528`). Prints two `key: value` lines to stdout
  (`worker_id: ...`, `token: ...`) for `grep`/`awk` extraction.
- Steady-state loop: standalone `whilly-worker` console script
  (registered in `pyproject.toml`) **or** `whilly worker ...` via the dispatcher
  (`whilly/cli/__init__.py:107-125`).

### Env vars / flags configured for cross-host use
| Env var | Flag | Semantics |
|---|---|---|
| `WHILLY_CONTROL_URL` | `--connect` | Control-plane base URL incl. scheme+port (`whilly/cli/worker.py:200, 297`). |
| `WHILLY_WORKER_TOKEN` | `--token` | Per-worker bearer (`worker.py:201, 305`). |
| `WHILLY_PLAN_ID` | `--plan` | Plan whose PENDING rows this worker drains (`worker.py:202, 313`). |
| `WHILLY_WORKER_ID` | `--worker-id` | Optional override; defaults to `<hostname>-<8-hex>` (`worker.py:203, 374-385`). |
| `WHILLY_WORKER_BOOTSTRAP_TOKEN` | `--bootstrap-token` | Used **only** by `register` (`worker.py:210`). |

### Worker identity stability
`_resolve_worker_id` (`whilly/cli/worker.py:374-385`):
```
if cli_override: return cli_override
if env_override: return env_override
return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
```
Auto-generated ids are **NOT stable across restarts** — a fresh `uuid4` is
minted each run. Operators that want stable ids must pin
`WHILLY_WORKER_ID`. The registration handler issues server-side ids
(`w-<urlsafe>`); the CLI auto-id is only used when the operator routes
around register and supplies the bearer manually.

The Docker entrypoint (`docker/entrypoint.sh:108-130`) auto-registers a
worker when `WHILLY_WORKER_TOKEN` is missing, parses out the freshly
minted `worker_id` + token, exports them, and execs `whilly-worker`. This
works for `docker compose up --scale worker=N` — each replica gets a
unique server-issued id and per-worker bearer.

---

## 2. Task Claim Semantics

Claim is **already** built for distributed contention; the SQL primitive is
production-grade.

### SQL — `FOR UPDATE OF t SKIP LOCKED`
`_CLAIM_SQL` at `whilly/adapters/db/repository.py:239-265`:
```sql
WITH picked AS (
    SELECT t.id FROM tasks t
    JOIN plans p ON p.id = t.plan_id
    WHERE t.plan_id = $1 AND t.status = 'PENDING'
      AND (p.budget_usd IS NULL OR p.spent_usd < p.budget_usd)
    ORDER BY <priority_rank>, t.id
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
)
UPDATE tasks
SET status = 'CLAIMED', claimed_by = $2, claimed_at = NOW(),
    version = tasks.version + 1, updated_at = NOW()
FROM picked
WHERE tasks.id = picked.id
RETURNING ...
```
- `FOR UPDATE OF t` deliberately locks `tasks` only — not the joined `plans`
  row — so 100-way contention does not starve on the per-plan budget row
  (rationale: `repository.py:253-264`).
- A CLAIM event row is inserted in the **same transaction** (`repository.py:993-998`),
  so the audit log can never disagree with the tasks table.

### Lease / liveness
There is **no separate lease table**. Liveness is enforced by two layered
sweeps running inside the control-plane lifespan TaskGroup:

1. **Visibility-timeout sweep** (`server.py:324-403`,
   `repository.py:1634-1700`): every `SWEEP_INTERVAL_DEFAULT_SECONDS` (60s
   default) it flips `CLAIMED|IN_PROGRESS` rows whose `claimed_at` predates
   `NOW() - VISIBILITY_TIMEOUT_DEFAULT_SECONDS` (15min default) back to
   `PENDING` and emits a `RELEASE` event with `payload.reason =
   'visibility_timeout'`. Slow fallback for stuck claims.
2. **Offline-worker sweep** (`server.py:406-465`,
   `repository.py:1704-1800`): every
   `OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS` (30s default) it flips
   `workers.status='online' → 'offline'` for rows whose `last_heartbeat`
   predates `NOW() - HEARTBEAT_TIMEOUT_DEFAULT_SECONDS` (120s default) and
   releases all that worker's CLAIMED/IN_PROGRESS tasks
   (`payload.reason = 'worker_offline'`). This is the **fast SC-2 recovery
   path** — peer worker re-claims within ~ heartbeat+sweep ≈ 2.5 min worst
   case.

### Worker dies mid-claim
- The worker heartbeats `POST /workers/{id}/heartbeat` every 30s
  (`whilly/worker/remote.py:115`, `DEFAULT_HEARTBEAT_INTERVAL`). On graceful
  SIGTERM/SIGINT the worker calls `POST /tasks/{id}/release` (server route
  `server.py:1271-1346`, repo SQL `_RELEASE_SQL` at `repository.py:498-521`)
  → row goes back to `PENDING` immediately, peers re-claim within one poll
  cycle.
- On hard kill the row stays `CLAIMED` until the offline-worker sweep
  reclaims it (≤ ~150s default).
- **Optimistic locking** on `tasks.version` provides last-line defence: every
  `complete`/`fail`/`release` filters `WHERE version = $2 AND status IN
  (...)`, so two writers (worker vs. sweep) lose-race cleanly with a 409
  surfacing `expected/actual_version + actual_status`
  (`server.py:1432-1457`, `client.py:287-334`).

### Claim contention beyond DB row level
`POST /tasks/claim` (`server.py:1034-1107`) is **server-side long-polled**:
30s budget, 1.5s repository poll cadence. Worker re-polls a 204 immediately
without client-side sleep (`whilly/worker/remote.py:32-46`). Multiple
remote workers all hit the same endpoint and contend purely through
`SKIP LOCKED`.

---

## 3. Transport & API Surface

### Endpoints (FastAPI routes in `whilly/adapters/transport/server.py`)

| Method | Path | Auth dep | Purpose |
|---|---|---|---|
| GET  | `/health` | none | Pings pool with `SELECT 1`; 200/503. (`server.py:893-938`) |
| POST | `/workers/register` | bootstrap | Mint `(worker_id, plaintext_token)`; 201. (`server.py:940-988`) |
| POST | `/workers/{id}/heartbeat` | per-worker | Refresh `last_heartbeat`, flip status→online. (`server.py:990-1032`) |
| POST | `/tasks/claim` | per-worker | Long-poll; 200 ClaimResponse / 204. (`server.py:1034-1107`) |
| POST | `/tasks/{id}/complete` | per-worker | IN_PROGRESS→DONE; 200/409. (`server.py:1125-1211`) |
| POST | `/tasks/{id}/fail` | per-worker | CLAIMED\|IN_PROGRESS→FAILED. (`server.py:1213-1269`) |
| POST | `/tasks/{id}/release` | per-worker | CLAIMED\|IN_PROGRESS→PENDING (graceful shutdown). (`server.py:1271-1346`) |
| GET  | `/api/v1/plans/{id}` | **none** | Read-only metadata `{id,name,github_issue_ref,prd_file}`. (`server.py:1349-1389`) |
| GET  | `/docs`, `/openapi.json` | none | Default FastAPI |

There is **no listing surface** (`GET /tasks`, `GET /workers`,
`GET /events`, `GET /plans`) and **no events streaming** (no `/events/stream`,
no SSE/WS).

### Bearer validation
- Per-request via FastAPI `Depends(make_db_bearer_auth(repo, legacy_token=…))`.
  The dep is **bound once at app build time** (`server.py:730-742`); the
  closure captures the repo and runs `SELECT worker_id FROM workers WHERE
  token_hash = sha256(presented)` (`_LOOKUP_WORKER_BY_TOKEN_HASH_SQL`,
  `repository.py:617-629`). On hit it stashes `worker_id` on
  `request.state.authenticated_worker_id`.
- Every state-mutating route additionally calls `_require_token_owner`
  (`server.py:1394-1429`) — worker A's bearer cannot act as worker B
  (returns 403). Skipped for the legacy fallback path (identity unknown).
- Constant-time compare (`secrets.compare_digest`); 401 returns
  `WWW-Authenticate: Bearer realm="whilly"` (RFC 6750).

### Long-polling vs short-polling
- `/tasks/claim` is **server-side long-polled** with deadline tracking
  (`server.py:1095-1107`). Default 30s budget + 1.5s repo retry interval.
- Client side: `RemoteWorkerClient` (`whilly/adapters/transport/client.py:354+`)
  is a single long-lived `httpx.AsyncClient` per worker process.
  - `DEFAULT_TIMEOUT_SECONDS = 60.0` (`client.py:236`) — 2× the server
    long-poll budget.
  - `DEFAULT_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0)` (`client.py:242`).
  - Retries on `httpx.ConnectError`, `httpx.TimeoutException`, HTTP 5xx;
    fail-fast on 4xx (with typed mapping: `AuthError` for 401/403,
    `VersionConflictError` for 409, `ServerError` for exhausted 5xx).
- No reconnect logic beyond httpx's connection-pool keepalive — TCP pool
  is reused across RPCs for the lifetime of the `async with` block.
- **No transport-level rate-limiting / circuit breaker** beyond the retry
  ladder.

### TLS
The control plane runs **plain HTTP over uvicorn** by default
(`docker/control_plane.py:55-72`); there is no TLS terminator wired in.
The architecture doc explicitly notes "Mitigations: TLS terminator in front
of the control plane" (`docs/Whilly-v4-Worker-Protocol.md:52`) — i.e.
ingress TLS is left to the operator (kube ingress / nginx / Caddy).

---

## 4. Workspace Assumptions

**This is the biggest single-host assumption baked in today.**

### Task execution side
The remote runner shells out to the agent CLI on the worker host:
`asyncio.create_subprocess_exec(*cmd, ..., env=child_env)`
(`whilly/adapters/runner/claude_cli.py:181-185`). No `cwd=` argument — the
agent inherits the worker process's `os.getcwd()`. **There is no patch
generation, no diff capture, no upload-back step.** Whatever the agent
writes to disk lands on the worker's local filesystem and stays there.

### Domain payload
`Task` carries `key_files: list[str]` (`whilly/adapters/db/schema.sql:99`,
domain model `whilly/core/models.py`). These are filesystem paths
referenced by the agent prompt (`whilly/core/prompts.py`). They are
**plain string paths** with no association with a content-addressable
store, no git commit, no archive — just bare paths the agent expects to
exist on the local FS where the worker runs.

### Server side
The control plane is **purely a state machine**: no file storage, no patch
review surface, no working-tree management. The plan import (`whilly plan
apply tasks.json`) only persists task metadata into `tasks` and `plans`
tables.

### Net effect
- A single worker process completes tasks against its local checkout.
- Two workers on **different hosts** working the same plan would each have
  their own checkout. There is no mechanism to merge their results, hand
  one worker's edits to another, or even detect that they touched
  conflicting `key_files`. The legacy v3 `worktree_runner.py` /
  `.whilly_worktrees/{task_id}` (still in `whilly/worktree_runner.py`) is
  **only used by the legacy in-process runner** (`whilly/cli/run_plan`
  path) — the v4 distributed worker does not consult it.
- The demo (`docker-compose.demo.yml`) hides this by running both worker
  replicas inside containers that share the same baked-in
  `/opt/whilly/examples/...` from `Dockerfile.demo` and using a fake
  `claude_demo.sh` that does no real file mutation.

---

## 5. Existing Multi-Host Hooks

Searched for: `tls`, `https`, `certbot`, `tunnel`, `ngrok`, `tailscale`,
`wireguard`, `cloudflared`, `vps`, `whilly-share`, `cross-host`,
`distributed`.

### What exists
- **Documentation** describing how to onboard a second machine:
  - `docs/Whilly-Workstation-Bootstrap.md` (lines 99-181) — three modes:
    standalone-Docker (no shared plan), SSH reverse-tunnel to primary's
    Postgres, Tailscale/WireGuard mesh.
  - `docs/Continuing-On-Another-Machine.md:70` — TL;DR cheat-sheet.
  - `docs/PRD-v41-claude-proxy.md` — TASK-109 lands the *worker → Anthropic*
    proxy / SSH-tunnel pattern (different concern: outbound LLM access).
- **One end-to-end SC-3 demo script** for cross-host *behaviour* on a
  single host: `docs/demo-remote-worker.sh`. Spawns Postgres + uvicorn +
  one `whilly-worker --once` against `127.0.0.1:8000`.
- **Production Dockerfile** (`Dockerfile`) ships a `worker` role
  (`docker/entrypoint.sh:75-138`) that:
  - waits for control-plane `/health` to flip green;
  - auto-registers via bootstrap token;
  - execs `whilly-worker --connect $WHILLY_CONTROL_URL --token … --plan …`.

### What is **planned but not implemented**
- `scripts/whilly-share.sh` — TASK-111 in `.planning/v4-1_tasks.json:873-905`.
  Designed as a one-command primary-side tunnel using
  `ssh -R 80:localhost:$PORT nokey@localhost.run` (anonymous, throwaway)
  with a `TUNNEL=cloudflared` opt-in for production. Referenced from
  `docs/Whilly-Workstation-Bootstrap.md:171-181` and
  `docs/Continuing-On-Another-Machine.md:70` as a future capability.
  **Currently the file `scripts/whilly-share.sh` does not exist** in the
  repo (verified via `LS scripts/`).
- TLS termination — the architecture doc enumerates TLS as the operator's
  responsibility (`docs/Whilly-v4-Worker-Protocol.md:52`). No bundled
  config, no certbot helper, no nginx sidecar.

### What does **not** exist
- No `cloudflared` binary or wrapper script.
- No `tailscale` integration in `Dockerfile` or compose stack.
- No `wireguard` config. No `certbot` helper.
- No public-endpoint scaffolding in `examples/`. (Confirmed via grep.)

---

## 6. Observability Surfaces

### Dashboard
`whilly/cli/dashboard.py` is a Rich Live TUI (q/r/p hotkeys), but it is
**Postgres-direct**, not control-plane-direct:

```python
from whilly.adapters.db import close_pool, create_pool   # dashboard.py:133
pool = await create_pool(dsn)                            # dashboard.py:488
await conn.fetch(_SELECT_DASHBOARD_ROWS_SQL, plan_id)    # dashboard.py:626
```

**Implication:** the dashboard cannot drive a remote control plane over HTTP
today — it requires direct asyncpg reachability to the same Postgres the
control plane uses. To run it from a second laptop you must SSH-tunnel /
Tailscale the Postgres port (per
`docs/Whilly-Workstation-Bootstrap.md` §4.2/§4.3) or expose Postgres
publicly. There is **no `GET /tasks` / `GET /events`** HTTP listing
surface for it to consume.

### Metrics / OTel / Prometheus
Confirmed via grep across `whilly/`:
- **No `prometheus_client`, no OpenTelemetry import, no `/metrics`
  endpoint.**
- The single hit for "metrics" is a docstring reference, not code
  (`whilly/worker/local.py:114`).
- No `structlog`. Standard library `logging` only, configured via
  `logging.basicConfig(level=WHILLY_LOG_LEVEL)` in
  `docker/control_plane.py:84-87`. No JSON formatter in production paths.

### Audit log = `events` table
The append-only `events` table (`schema.sql:120-150`) is the canonical audit
trail and the only "structured" observability primitive:
- One row per state transition (`CLAIM`, `START`, `COMPLETE`, `FAIL`,
  `RELEASE`, `SKIP`, `triz.contradiction`, `plan.budget_exceeded`, ...).
- Carries `event_type`, `payload jsonb` (state-machine bookkeeping) and
  `detail jsonb` (free-form diagnostics, TASK-104b).
- Persisted via the lifespan `EventFlusher` (`whilly/api/event_flusher.py`)
  — bulk INSERT with checkpoint on shutdown
  (`server.py:751-845`).
- **Read access is SQL-only** — there is no API for downstream consumers
  to subscribe.

### Worker logs
- Remote worker logs to **stdout/stderr only** — no per-worker file sink,
  no log shipping. `WHILLY_LOG_LEVEL` controls verbosity
  (`Dockerfile:79`, `docker-compose.demo.yml:72,155`).
- Final summary line is written to stderr at exit
  (`whilly/cli/worker.py:339-346`).
- Failure reasons are truncated to ≤500 chars and stored in
  `events.payload.reason` (`whilly/worker/remote.py:127`,
  `_FAIL_REASON_OUTPUT_CAP`).
- Container deployments inherit Docker's stdout-collected log driver; no
  built-in fluent-bit / loki / journald shim.

---

## Summary: What's Already Distribution-Ready

- **HTTP transport surface is real and contract-stable.** Worker→control-plane
  RPCs are versioned (`/api/v1/plans/{id}`-style is starting), bearer-auth'd,
  schema-validated (pydantic), and have OpenAPI docs at `/docs`.
- **Two-token security split** (`bootstrap` for registration,
  per-worker bearer for steady-state RPCs) supports adding/revoking
  workers without restarting the cluster, and `_require_token_owner`
  prevents cross-worker bearer use (403). Suitable for adversarial
  multi-tenant deployments once TLS is layered in.
- **Concurrency primitives are production-grade.**
  `FOR UPDATE OF t SKIP LOCKED` claim + optimistic-locking
  `version` counter + visibility-timeout sweep + offline-worker sweep
  collectively handle the "100 workers, 1 plan, 1 dies" matrix without
  losing or duplicating work. Validated by SC-2 (kill -9 a worker, peer
  re-claims).
- **Containerized worker role** (`docker/entrypoint.sh`) auto-registers
  via bootstrap token on cold boot — `docker compose up --scale worker=N`
  works *today* on one host because each replica gets a unique
  server-issued `worker_id`. Same image works on any host that can reach
  the control plane URL.
- **Graceful shutdown semantics work over HTTP** —
  `POST /tasks/{id}/release` with `payload.reason="shutdown"` puts an
  in-flight task back to `PENDING` so a peer reclaims it within one poll
  cycle (no waiting for the visibility-timeout sweep).

## Summary: What's Demo-Bound (single-host only)

- **Workspace / file-state has no multi-host story.** The agent runs
  `claude` (or shim) in the worker process's local `cwd` and writes to
  the local FS. There is no patch capture, no shared blob store, no git
  worktree integration in the v4 path, and no `key_files`-aware conflict
  detection. Two real workers on different hosts editing the same plan
  would silently diverge.
- **Control plane runs plain HTTP** (`docker/control_plane.py:55-72`).
  Cross-internet exposure requires the operator to put a TLS terminator
  in front; there is no bundled certbot/nginx/caddy config.
- **No public-endpoint scaffolding ships.** `scripts/whilly-share.sh`
  (TASK-111: `ssh -R …localhost.run` + cloudflared) is documented as a
  future capability but the file does not exist. Onboarding a remote
  worker today requires the operator to roll their own SSH tunnel,
  Tailscale, or VPS setup (procedures live in
  `docs/Whilly-Workstation-Bootstrap.md` §4.2-4.4).
- **Dashboard is asyncpg-direct, not HTTP-direct** (`dashboard.py:133,488`).
  Watching a remote plan today means tunnelling Postgres, not just the
  control-plane URL — there is no `GET /tasks` / `/events` HTTP surface
  for a thin remote dashboard to consume.
- **Observability is stdout + Postgres `events` only.** No Prometheus
  endpoint, no OTel exporter, no structured-JSON log shipper, no
  per-worker log sink. Cross-host operators rely on `docker logs` /
  `kubectl logs` plus direct SQL queries against `events`.
- **Auto-generated worker ids are not stable across restarts**
  (`whilly/cli/worker.py:374-385`) — operators that need pinned identity
  for log correlation across crashes must set `WHILLY_WORKER_ID`
  explicitly.
