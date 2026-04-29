# Whilly v4.0 Worker HTTP Protocol

> Wire-level specification for the worker ↔ control-plane HTTP API
> (PRD FR-1.x). Anyone implementing a non-Python worker, or debugging a
> production deployment, should read this doc end-to-end. Python users
> get this for free via `whilly.adapters.transport.client.RemoteWorkerClient`.

## Versioning

This document describes **protocol version 1.0** — the API exposed by
`whilly-orchestrator==4.0.0`. There is no `/v1` URL prefix because v4 is
the first release with a stable HTTP surface; future incompatible
changes will introduce `/v2` etc. and ship under semver-major bumps.

Worker and control-plane versions **must match**. The dependency in the
`whilly-worker` meta-package is pinned (`whilly-orchestrator[worker]==X.Y.Z`)
precisely so this never drifts in production.

## Endpoints overview

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/health` | none | Liveness probe (Postgres SELECT 1) |
| `POST` | `/workers/register` | bootstrap | Mint a fresh `(worker_id, token)` |
| `POST` | `/workers/{worker_id}/heartbeat` | bearer | Refresh `last_heartbeat` |
| `POST` | `/tasks/claim` | bearer | Long-polled task acquisition |
| `POST` | `/tasks/{task_id}/complete` | bearer | Terminal: → DONE |
| `POST` | `/tasks/{task_id}/fail` | bearer | Terminal: → FAILED |
| `POST` | `/tasks/{task_id}/release` | bearer | Graceful shutdown: → PENDING |

All bodies are JSON. All endpoints return JSON (or empty 204 on a
documented no-content path). Errors use a structured envelope — see
"Errors" below.

## Authentication

Two token types:

* **Bootstrap token** (cluster-wide). One value across the cluster, used
  exactly once per worker — at `/workers/register`. The control plane
  reads it from `WHILLY_WORKER_BOOTSTRAP_TOKEN` at boot.
* **Bearer token** (cluster-shared in v4.0). All steady-state RPCs
  (`heartbeat`, `claim`, `complete`, `fail`, `release`) carry it as
  `Authorization: Bearer <token>`. The control plane reads it from
  `WHILLY_WORKER_TOKEN`.

> **v4.0 caveat — shared bearer.** v4.0 ships with a single cluster-
> shared bearer (one value for all workers). Per-worker bearer rotation
> (mint at registration, validated against `workers.token_hash`) lands
> in v4.1. For the v4.0 release this means: rotating the bearer
> requires bouncing all workers; cluster compromise ≈ all-worker
> compromise. Mitigations: TLS terminator in front of the control
> plane, network-level isolation. The PRD's NFR-3 ("plaintext tokens
> never persisted") is met because `workers.token_hash` is still SHA-256
> hashed even though the steady-state path doesn't currently consult it.

A missing or invalid token returns:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="whilly"
Content-Type: application/json

{"error_code": "missing_or_invalid_bearer", "detail": "...", "task_id": null}
```

403 means "I know who you are, but you can't do this" — currently
unused (v4.0 has no per-token permissions); future per-worker tokens
will surface 403 on cross-worker actions.

## `GET /health`

Liveness probe. The handler runs `SELECT 1` against the asyncpg pool to
prove the control plane can still reach Postgres.

```http
GET /health HTTP/1.1
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok"}
```

Returns `503 Service Unavailable` if Postgres is unreachable. No auth.
Suitable for Kubernetes liveness/readiness probes.

## `POST /workers/register`

Mint a fresh `(worker_id, token)` pair.

```http
POST /workers/register HTTP/1.1
Authorization: Bearer <bootstrap-token>
Content-Type: application/json

{"hostname": "worker-vm-01"}
```

```http
HTTP/1.1 201 Created
Content-Type: application/json

{"worker_id": "w-7c4f2a8b9d1e", "token": "<plaintext-per-worker-bearer>"}
```

* `worker_id` is `w-<urlsafe-12-chars>`, server-generated to avoid
  collisions.
* `token` is `secrets.token_urlsafe(32)` — plaintext, returned exactly
  once. The server stores only the SHA-256 hash in `workers.token_hash`.
  If the worker crashes before storing the token, it must re-register.
* On the rare entropy-collision path the server returns 500 rather than
  retrying with a fresh id; collisions are nearly impossible (64 bits
  of entropy) and a retry would paper over a broken entropy source.

> **v4.0 note**: although `register` returns a per-worker token, all
> downstream RPCs accept the cluster-shared `WHILLY_WORKER_TOKEN`
> instead. Per-worker bearer enforcement lands in v4.1.

## `POST /workers/{worker_id}/heartbeat`

Refresh `workers.last_heartbeat = NOW()`.

```http
POST /workers/w-7c4f2a8b9d1e/heartbeat HTTP/1.1
Authorization: Bearer <worker-token>
Content-Type: application/json

{"worker_id": "w-7c4f2a8b9d1e"}
```

The body's `worker_id` must match the path's — defence-in-depth against
a misrouted client. Mismatch returns `400 Bad Request`.

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"ok": true}
```

`ok=false` (with HTTP 200) means the worker is not (or no longer)
registered. The worker's correct response is to re-register; the
heartbeat loop logs a warning and keeps ticking so a transient
deregistration doesn't crash the worker.

Recommended cadence: 30s. The visibility-timeout sweep flips the
worker `offline` after `2 × heartbeat_interval` seconds without a tick.

## `POST /tasks/claim`

Long-polled task acquisition.

```http
POST /tasks/claim HTTP/1.1
Authorization: Bearer <worker-token>
Content-Type: application/json

{"worker_id": "w-7c4f2a8b9d1e", "plan_id": "plan-abc123"}
```

The handler tries `claim_task(worker_id, plan_id)` in a loop, sleeping
`claim_poll_interval` (default 1.5s) between attempts, until either:

* a task transitions PENDING → CLAIMED (200 + `ClaimResponse`), or
* the cumulative wait exceeds `claim_long_poll_timeout` (default 30s),
  in which case the server returns 204.

```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "task": {
    "id": "T-001",
    "plan_id": "plan-abc123",
    "status": "CLAIMED",
    "version": 2,
    "priority": "critical",
    "description": "...",
    "dependencies": [],
    "key_files": ["whilly/main.py"],
    "acceptance_criteria": ["entry point runs"],
    "test_steps": ["pytest -q"],
    "prd_requirement": "Day 4 deliverable"
  }
}
```

```http
HTTP/1.1 204 No Content
```

On 204 the worker's correct response is to immediately re-issue the
claim. The cumulative wait time across re-issues is bounded only by the
worker's own outer loop, not by this endpoint.

Server-side polling rather than client-side retry keeps the worker's
outer loop trivial (`while True: claim(); run(); complete()`) and
holds a single connection open instead of multiplying the request
rate against Postgres.

## `POST /tasks/{task_id}/complete`

Terminal-state RPC: status → DONE.

```http
POST /tasks/T-001/complete HTTP/1.1
Authorization: Bearer <worker-token>
Content-Type: application/json

{"worker_id": "w-7c4f2a8b9d1e", "version": 2}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "task": {
    "id": "T-001",
    "status": "DONE",
    "version": 3,
    ...
  }
}
```

The `version` in the body is the **expected** version (the one
returned by `claim`). The server's `_COMPLETE_SQL` filters by
`version = $2 AND status IN ('CLAIMED', 'IN_PROGRESS')` — both states
are valid because the remote-worker shape skips IN_PROGRESS (no
`/start` RPC; see [`Whilly-v4-Architecture.md`](Whilly-v4-Architecture.md#data-flow--remote-worker-shape-sc-3)).

On conflict (lost race, terminal state already, row gone):

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{
  "error_code": "version_conflict",
  "task_id": "T-001",
  "expected_version": 2,
  "actual_version": 3,
  "actual_status": "DONE",
  "detail": "..."
}
```

Field semantics for the worker's branch logic:

* `actual_status is None and actual_version is None` → row gone (FK
  cascade in tests, mis-routed worker).
* `actual_version != expected_version` → another writer advanced the
  counter (lost-update / re-claim).
* `actual_version == expected_version` and `actual_status` is `DONE` /
  `FAILED` / `SKIPPED` → idempotent retry — the worker treats it as
  success and moves on.

## `POST /tasks/{task_id}/fail`

Terminal-state RPC: status → FAILED.

```http
POST /tasks/T-001/fail HTTP/1.1
Authorization: Bearer <worker-token>
Content-Type: application/json

{"worker_id": "w-7c4f2a8b9d1e", "version": 2, "reason": "exit_code=1"}
```

Same shape as `/complete` plus a `reason` string that lands in the
`events.payload` of the FAIL audit row. `_FAIL_SQL` accepts both
CLAIMED and IN_PROGRESS — a worker that crashes between claim and run
can still emit a clean FAILED audit row.

`409` envelope identical to `/complete`.

## `POST /tasks/{task_id}/release`

Graceful shutdown — flip the task back to PENDING so a peer (or this
worker on restart) can re-claim it within one poll cycle. Used by the
worker's SIGTERM/SIGINT handler.

```http
POST /tasks/T-001/release HTTP/1.1
Authorization: Bearer <worker-token>
Content-Type: application/json

{"worker_id": "w-7c4f2a8b9d1e", "version": 2}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"task": {"id": "T-001", "status": "PENDING", "version": 3, ...}}
```

`409` envelope identical to `/complete`. Idempotent retry: a row that
already PENDING due to the visibility-timeout sweep returns 409 with
`actual_status="PENDING"` — the worker's signal handler treats this as
"someone got there first" and exits cleanly.

## Worker lifecycle (state diagram)

```
                    ┌────────────────────────────┐
                    │  process start             │
                    │  (or post-crash restart)   │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
              ┌───────────────────────────────────────┐
              │  POST /workers/register (bootstrap)   │
              │  ← 201 (worker_id, token)             │
              │  (skipped on shared-bearer v4.0 path) │
              └─────────────┬─────────────────────────┘
                            │
                            ▼
        ┌───────────────────────────────────────────┐
        │  parallel TaskGroup:                      │
        │   ┌──────────────────────────────────┐   │
        │   │ heartbeat loop (30s cadence):    │   │
        │   │   POST /workers/{id}/heartbeat   │   │
        │   └──────────────────────────────────┘   │
        │   ┌──────────────────────────────────┐   │
        │   │ main loop:                       │   │
        │   │   while not stop:                │   │
        │   │     POST /tasks/claim            │   │
        │   │       └ 204 → continue           │   │
        │   │       └ 200 → run + complete/    │   │
        │   │              fail                │   │
        │   └──────────────────────────────────┘   │
        └─────────────────────┬─────────────────────┘
                              │
                  SIGTERM/SIGINT received
                              │
                              ▼
              ┌───────────────────────────────────────┐
              │  if in-flight task:                   │
              │    POST /tasks/{id}/release           │
              │  cancel TaskGroup → process exit 0    │
              └───────────────────────────────────────┘
```

The worker never holds a database connection — all state lives in the
control plane's asyncpg pool. A worker crash leaves at most one task
in CLAIMED state; the visibility-timeout sweep flips it back to
PENDING after `WHILLY_VISIBILITY_TIMEOUT` seconds (default 60).

## Errors

All non-2xx responses share the envelope:

```json
{
  "error_code": "<machine-readable string>",
  "detail": "<human-readable message>",
  "task_id": "<id-or-null>"
}
```

`409 Conflict` adds `expected_version`, `actual_version`,
`actual_status` for `/complete` / `/fail` / `/release`.

Stable `error_code` values:

| Code | HTTP | Meaning |
|---|---|---|
| `missing_or_invalid_bearer` | 401 | Bearer token missing or doesn't match |
| `missing_or_invalid_bootstrap` | 401 | Bootstrap token missing or wrong (register only) |
| `version_conflict` | 409 | Optimistic-locking conflict — branch on extra fields |
| `worker_id_mismatch` | 400 | Path / body `worker_id` disagree (heartbeat) |
| `worker_id_collision` | 500 | Register entropy collision (≈ never) |

## Retry policy (recommended)

| Error class | Retry? | Strategy |
|---|---|---|
| Network timeout / 5xx | yes | Exponential back-off; cap at 60s; no upper limit on attempts (worker is long-running) |
| 401 / 403 | no | Crash — the supervisor restarts with fresh config |
| 409 on complete/fail | no | Log and skip; the row is owned by someone else now |
| 409 on release | no | Idempotent — exit cleanly |
| 204 on claim | yes | Immediately re-issue the claim |
| 200 with `ok=false` on heartbeat | yes (after re-register) | Heartbeat keeps ticking; re-register on next iteration |

The Python `RemoteWorkerClient` implements this policy in
`whilly/adapters/transport/client.py`. Workers in other languages
should mirror it.

## Pointers

* Wire schemas (pydantic): [`whilly/adapters/transport/schemas.py`](../whilly/adapters/transport/schemas.py)
* FastAPI handler implementation: [`whilly/adapters/transport/server.py`](../whilly/adapters/transport/server.py)
* Python client: [`whilly/adapters/transport/client.py`](../whilly/adapters/transport/client.py)
* Architecture: [`Whilly-v4-Architecture.md`](Whilly-v4-Architecture.md)
* Migration from v3: [`Whilly-v4-Migration-from-v3.md`](Whilly-v4-Migration-from-v3.md)
