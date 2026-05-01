# Extension Surfaces — M1+M2+M3 Distributed Mission

> All file:line refs anchor to commit `1093009` (v4.3.1).
> Backwards-compat constraint: existing `docker-compose.demo.yml` +
> `workshop-demo.sh` + `mshegolev/whilly:4.3.1` image MUST continue
> to work identically. Every change below is additive.

---

## 1. FastAPI app factory & lifespan

### Composition root

`whilly/adapters/transport/server.py:create_app(...)` is the single
factory. The signature today is:

```python
create_app(
    pool: asyncpg.Pool,
    *,
    worker_token: str | None = None,
    bootstrap_token: str | None = None,
    claim_long_poll_timeout: float = ...,
    claim_poll_interval: float = ...,
    visibility_timeout_seconds: int = ...,
    sweep_interval_seconds: float = ...,
    heartbeat_timeout_seconds: int = ...,
    offline_worker_sweep_interval_seconds: float = ...,
    event_flush_interval_seconds: float = ...,
    event_batch_limit: int = ...,
    event_drain_timeout_seconds: float = ...,
    event_checkpoint_dir: str | None = None,
) -> FastAPI
```
(server.py:567-606 — kwargs are validated `>= 0` / `> 0` at the top
of the function, server.py:617-657.)

Lifespan is an `@asynccontextmanager async def lifespan(app)` declared
inline at server.py:707-863. It does:

1. Stash `pool / repo / bearer_dep / bootstrap_dep / sweep_stop /
   event_flusher / event_queue / event_flusher_task` on `app.state`
   (server.py:721-749).
2. Open one `async with asyncio.TaskGroup() as tg:` block
   (server.py:781-813) that supervises three coroutines, each named:
   - `whilly-visibility-sweep` (`_visibility_sweep_loop`,
     server.py:329-403).
   - `whilly-offline-worker-sweep` (`_offline_worker_sweep_loop`,
     server.py:406-465).
   - `whilly-event-flusher` (`EventFlusher.run`,
     `whilly/api/event_flusher.py:202-353`).
3. `yield` to the app, then `sweep_stop.set()` + `await flusher.drain()`
   on teardown (server.py:830-862).

**Production launcher** that owns the pool lifecycle is
`docker/control_plane.py:_serve()` (no kwargs are forwarded to
`create_app` today — host/port/log come from `WHILLY_HOST` /
`WHILLY_PORT` / `WHILLY_LOG_LEVEL`). `WHILLY_HOST` defaults to
`0.0.0.0` (control_plane.py:52).

The lifespan is a single closure inside `create_app`; there are
**no `APIRouter` modules** anywhere in the repo (Grep
`include_router|APIRouter` → no matches). All routes are registered
inline as decorators on the local `app` variable (server.py:874-1462).

### How to add a new endpoint / dependency / sweep

* **New endpoint.** Append a new `@app.<verb>(...)` decorator inside
  `create_app` after the existing `/api/v1/plans/{plan_id}` block at
  server.py:1421-1462. They all close over `pool`, `repo`,
  `bearer_dep`, `bootstrap_dep`, the long-poll knobs and any timing
  defaults — you can rely on closure capture, no `request.app.state`
  read needed.
* **New sweep / background task.** Mimic the existing pattern:
  define an `async def _<name>_loop(repo, *, …, stop: asyncio.Event)`
  module-level coroutine that loops `await asyncio.wait_for(stop.wait(),
  timeout=interval)` (server.py:329-403 is the canonical template,
  catches `Exception` per tick), then add a single
  `tg.create_task(_<name>_loop(...), name="whilly-<name>")` line
  inside the lifespan TaskGroup at server.py:782-813. The shared
  `sweep_stop` event is reused by every sweep — no need for a new
  one. The lifespan teardown already calls `sweep_stop.set()` and
  the TaskGroup drains all children automatically.
* **New dependency.** Build it inside `create_app` *after*
  `bearer_dep` / `bootstrap_dep` at server.py:702-705 (**before**
  the `lifespan` coroutine is defined, so the closure picks it up),
  then declare it on a route via `dependencies=[Depends(my_dep)]`.

### Adding HTMX dashboard (M3)

* **Endpoint.** New `@app.get("/", response_class=HTMLResponse)` at
  the bottom of `create_app` (just before `return app` at
  server.py:1462). Fetch rows reusing the same SQL the TUI uses —
  copy `_SELECT_DASHBOARD_ROWS_SQL` from
  `whilly/cli/dashboard.py:233-251` into a server-side module
  constant, run it via `pool.acquire() / conn.fetch(...)`. The TUI's
  `DashboardRow` projection (`whilly/cli/dashboard.py:265-291`) is
  the natural read-model — promote to a shared helper if both
  surfaces are kept.
* **Templates.** **No Jinja2 templating is mounted today.**
  (`Grep jinja2|HTMX|Jinja|Templates` → no matches in `whilly/`.)
  Natural place to add: a new `whilly/api/templates/` directory plus
  a single `from fastapi.templating import Jinja2Templates;
  templates = Jinja2Templates(directory=...)` near the top of
  `create_app`. `jinja2` is a transitive dep of FastAPI itself but
  is **not** in the `[server]` extras list — add it to
  `pyproject.toml`'s `[server]` extra in the same M3 commit. HTMX
  is one `<script>` tag, no Python dep.
* **Static assets.** `app.mount("/static",
  StaticFiles(directory=...))` if needed; today nothing is mounted.

### Adding SSE endpoint (M3)

* **Endpoint shape.** `@app.get("/events/stream")` returning a
  `fastapi.responses.StreamingResponse(media_type="text/event-stream")`.
  Auth-gated via `dependencies=[Depends(bearer_dep)]` for now (admin
  auth may come later — see §1.5 below).
* **Backing primitive.** asyncpg LISTEN/NOTIFY — see §2 below for
  detail. The async generator inside the `StreamingResponse` will
  acquire **one dedicated** connection from the pool (a `LISTEN`
  binds the channel to the connection's lifetime, so this connection
  cannot return to the pool while the SSE stream is open). The
  pattern is `conn = await pool.acquire(); await
  conn.add_listener(channel, callback)` and the stream yields each
  `payload` as `f"data: {payload}\n\n"`.
* **Lifespan integration.** Optional but recommended: add a
  `_notify_listener_loop` coroutine to the lifespan TaskGroup that
  owns one *shared* asyncpg connection with a single `LISTEN
  whilly_events;`, drains payloads into an in-memory
  `asyncio.Queue`, and lets per-client SSE handlers fan-out from
  that queue. This avoids holding N pool connections for N clients.
  (Same `sweep_stop` rendezvous closes it on shutdown.)

### Adding Prometheus `/metrics` (M3)

* **Endpoint.** `@app.get("/metrics", include_in_schema=False)` at
  the same bottom-of-`create_app` location as the HTMX dashboard.
  Returns
  `Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)`.
* **Dep.** Add `prometheus-client>=0.20` to the `[server]` extras
  in `pyproject.toml`. **No conflict** — repo currently has no
  Prometheus / OTel / structlog code at all (Grep `prometheus|otel`
  → no matches).
* **Registration site.** A new `whilly/api/metrics.py` is the
  natural home — define module-level `Counter` / `Gauge`
  instances (`tasks_claimed_total`, `tasks_completed_total`,
  `worker_status_gauge`, `claim_queue_depth_gauge`,
  `plan_spent_usd_gauge`, etc.) and import them at the
  call-sites (route handlers update counters, lifespan
  background task refreshes gauges from SQL on a 10-30s tick).
  The same lifespan TaskGroup pattern hosts a
  `_metrics_refresh_loop` coroutine.

### Adding admin auth (M2)

`make_db_bearer_auth` and `make_bootstrap_auth` are the existing
factories at `whilly/adapters/transport/auth.py:421-486` and 600-630.
The patterns to copy:

* **Identity-binding shape** (writes to
  `request.state.authenticated_worker_id`) — copy
  `make_db_bearer_auth`. For admin we'd write a new
  `request.state.authenticated_admin = True/False` (or carry the
  `bootstrap_tokens.owner_email`).
* **Plain gate-keeper shape** — copy `make_bootstrap_auth`
  (auth.py:600-630). Same RFC 6750 401-with-`WWW-Authenticate`
  envelope.

A new `make_admin_auth(repo)` would:
1. Extract bearer (call `_extract_bearer`, auth.py:194-225).
2. SHA-256 hash via `hash_bearer_token` (auth.py:228-256).
3. Look up against a new `bootstrap_tokens` table (see §2 below) —
   add a single `repo.get_bootstrap_token_owner(token_hash)` method
   alongside `get_worker_id_by_token_hash` (`whilly/adapters/db/repository.py:1845-1888`).
4. On miss: 401. On hit with `revoked_at IS NOT NULL` or
   `expires_at < NOW()`: 401.
5. On hit, optionally check `is_admin` flag → return None (allow);
   else 403.

The new `make_admin_auth` is wired in inside `create_app` next to
the existing dep construction (server.py:702-705) and bound to
admin-only routes via `dependencies=[Depends(admin_dep)]`. No other
auth surface needs to change — workers continue using
`make_db_bearer_auth` exactly as today.

The **bootstrap token** itself can be reused for admin until M2's
per-user tokens land — i.e. the same `make_bootstrap_auth` dep
guards `POST /admin/bootstrap-tokens/mint` initially. Then mint's
output becomes the per-user admin tokens that
`make_admin_auth` validates.

---

## 2. Database schema, migrations, repository

### Migration tool

**Alembic, async-mode.** `alembic.ini` at repo root points
`script_location = whilly/adapters/db/migrations`; `env.py`
(`whilly/adapters/db/migrations/env.py`) coerces
`WHILLY_DATABASE_URL` → `postgresql+asyncpg://...` and runs migrations
through `async_engine_from_config(...) + run_sync(do_run_migrations)`.
`target_metadata = None` — no SQLAlchemy ORM models — so
`--autogenerate` is intentionally a no-op; every migration is hand-
written.

Existing chain (head = 007):
- `001_initial_schema.py` — workers / plans / tasks / events tables.
- `002_workers_status.py` — `workers.status` + offline-detection index.
- `003_events_detail.py` — adds `events.detail jsonb`.
- `004_per_worker_bearer.py` — relaxes `workers.token_hash` to
  nullable + partial UNIQUE index `ix_workers_token_hash_unique`.
- `005_plan_budget.py` — `plans.budget_usd / spent_usd`.
- `006_plan_github_ref.py` — `plans.github_issue_ref` + partial
  UNIQUE.
- `007_plan_prd_file.py` — `plans.prd_file`.

`schema.sql` is **reference / docs only** — keep it in sync by hand
(`whilly/adapters/db/schema.sql:1-18`). CI does not auto-diff yet.
Production image runs `alembic upgrade head` from
`docker/entrypoint.sh:60`; production alembic config is
`docker/alembic.prod.ini`.

### Adding `workers.owner_email` column (M2)

New migration `008_workers_owner_email.py`. Copy `004` or `006` as
template — both are short single-column adds with a partial index.

`upgrade()`:
```python
op.add_column(
    "workers",
    sa.Column("owner_email", sa.Text(), nullable=True, server_default=None),
)
op.create_index("ix_workers_owner_email", "workers", ["owner_email"],
                postgresql_where="owner_email IS NOT NULL")
```

`downgrade()` — drop index, drop column.

Touchpoints elsewhere:
* `_INSERT_WORKER_SQL` at
  `whilly/adapters/db/repository.py:604-607` — extend to insert
  `owner_email` (4th param, nullable).
* `register_worker(...)` repo method at `repository.py:1804-1843`
  — add `owner_email: str | None = None` kwarg.
* The `POST /workers/register` handler at `server.py:884-988` —
  read `owner_email` either from the bootstrap-token record (M2's
  `bootstrap_tokens` row carries `owner_email`) or from the
  `RegisterRequest` (`whilly/adapters/transport/schemas.py`).
* Update reference `schema.sql:20-44`.
* The events emitted on register / heartbeat / claim already write
  `payload jsonb`; tagging events with `owner_email` is an
  in-process projection — populate it in
  `_INSERT_WORKER_SQL` and pull it back via a JOIN inside
  `_RELEASE_OFFLINE_WORKERS_SQL` (`repository.py:670-698`) so the
  audit row carries `owner_email` for free.

### Adding `bootstrap_tokens` table (M2)

New migration `009_bootstrap_tokens.py`:
```python
op.create_table(
    "bootstrap_tokens",
    sa.Column("token_hash", sa.Text(), primary_key=True),
    sa.Column("owner_email", sa.Text(), nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True),
              server_default=sa.text("NOW()"), nullable=False),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("is_admin", sa.Boolean(), server_default=sa.false(),
              nullable=False),
)
op.create_index("ix_bootstrap_tokens_owner_email_active",
                "bootstrap_tokens", ["owner_email"],
                postgresql_where="revoked_at IS NULL")
```

Repo additions next to the existing
`_LOOKUP_WORKER_BY_TOKEN_HASH_SQL` block at
`repository.py:617-629`:
* `_INSERT_BOOTSTRAP_TOKEN_SQL`, `_REVOKE_BOOTSTRAP_TOKEN_SQL`,
  `_LOOKUP_BOOTSTRAP_TOKEN_OWNER_SQL`,
  `_LIST_ACTIVE_BOOTSTRAP_TOKENS_SQL`.
* Methods: `mint_bootstrap_token(plaintext, owner_email,
  expires_at, is_admin)`, `revoke_bootstrap_token(token_hash)`,
  `get_bootstrap_token_owner(token_hash) -> (owner_email,
  is_admin) | None`, `list_bootstrap_tokens() -> list[...]`.

Upgrade `make_bootstrap_auth` at `auth.py:600-630`: instead of
constant-time comparing against a single env-var string, accept a
`repo: TaskRepository` and dispatch to
`repo.get_bootstrap_token_owner(token_hash)`. Stash
`(owner_email, is_admin)` on `request.state` so the
`POST /workers/register` handler can propagate `owner_email` into
`workers.owner_email` without an extra round-trip. Keep the env-var
value as a **legacy fallback** (one minor version), exactly the
shape `make_db_bearer_auth` already follows for `WHILLY_WORKER_TOKEN`
(auth.py:421-498) — VAL-AUTH-030/031/034 contract is the proven
template.

### asyncpg LISTEN/NOTIFY current state

**Not used today.** Grep
`add_listener|LISTEN|NOTIFY` → only one substring hit in
`whilly/cli/dashboard.py` (POSIX termios listener — unrelated). The
schema has no triggers (`grep -i 'CREATE FUNCTION\|CREATE TRIGGER'`
in `schema.sql` → none).

To wire SSE / dashboard streaming:

1. **Migration 010_events_notify_trigger.py** — single
   `CREATE FUNCTION whilly_notify_event() RETURNS trigger AS $$
   BEGIN PERFORM pg_notify('whilly_events',
       json_build_object('event_id', NEW.id, 'event_type',
       NEW.event_type, 'task_id', NEW.task_id, 'plan_id',
       NEW.plan_id)::text); RETURN NEW; END; $$ LANGUAGE plpgsql;`
   plus `CREATE TRIGGER tr_events_notify AFTER INSERT ON events
   FOR EACH ROW EXECUTE FUNCTION whilly_notify_event();`.
   Cheap (~10 µs per insert); the existing 100 ms event-flusher
   batch (`event_flusher.py:_INSERT_PREFIX`) issues one multi-row
   INSERT, so the trigger fires once per row inside that statement
   — zero impact on throughput.
2. **Listener coroutine in lifespan** — new
   `_event_notify_listener_loop` coroutine inside the same
   TaskGroup at server.py:782-813. Acquires one dedicated
   connection from the pool *outside* the pool's normal rotation
   (`conn = await asyncpg.connect(dsn)`), `await conn.add_listener
   ('whilly_events', cb)`, where `cb` enqueues onto an
   `asyncio.Queue` stashed on `app.state.event_notify_queue`. The
   SSE handler (§1) consumes from that queue.
3. **Pool sizing.** Current production pool min/max defaults are
   asyncpg defaults; the dedicated listener connection lives
   outside the pool, so no resizing needed. Tests open
   `min_size=2, max_size=20` (`tests/conftest.py:380`).

---

## 3. Test infrastructure

### Layout

* `tests/unit/` (35 files) — pure-Python tests, no Postgres. Has
  its own `tests/unit/conftest.py`. Examples:
  `test_transport_server.py`, `test_transport_auth.py`,
  `test_bearer_dep.py`, `test_cli_worker.py`, `test_cli_init.py`.
* `tests/integration/` (44 files) — Postgres-backed via
  `testcontainers` (`postgres:15-alpine`). Examples:
  `test_phase5_remote.py` (full e2e over real HTTP socket),
  `test_per_worker_auth.py`, `test_event_flusher.py`,
  `test_alembic_*.py` (one per migration).
* `tests/` (root, ~30 files) — legacy v3-era tests for the in-process
  loop (`test_whilly_dashboard.py`, `test_classifier.py`,
  `test_github_*.py`, etc.). Not relevant to M1+M2+M3.
* `tests/fixtures/` — `fake_claude.sh`, `fake_claude_demo.sh`
  (used by Phase 4/5 e2e gates and the demo compose).
* `tests/conftest.py` (398 lines) is the source of truth for new
  Postgres-backed integration tests. Module-level
  `pytestmark = DOCKER_REQUIRED` skips when Docker is missing.

### Postgres in tests

* **Session-scoped** `postgres_dsn` fixture
  (`tests/conftest.py:280-340`) — boots one
  `PostgresContainer("postgres:15-alpine")` for the whole pytest
  session, runs `alembic upgrade head` once, returns a DSN. Wraps
  start + alembic in a 3-attempt exponential-backoff retry to ride
  out colima port-forwarding flake (conftest.py:30-100).
* **Per-test** `db_pool` fixture (conftest.py:343-378) — fresh
  asyncpg pool per test, `TRUNCATE events, tasks, plans, workers
  RESTART IDENTITY CASCADE` at setup time (not teardown — so a
  failing test leaves diagnostic state).
* **Per-test** `task_repo` fixture (conftest.py:382-398) — wraps
  `db_pool` in `TaskRepository`. **Use this** for repository unit
  tests of the new `bootstrap_tokens` SQL.

`pyproject.toml` already has `pytest-asyncio>=0.23` and
`testcontainers>=4.0` in `[dev]`. `asyncio_mode = "auto"` is set
at `tool.pytest.ini_options` so test funcs can simply be `async
def`.

### Test count (approximate)

By Glob: 79 test files total
(`tests/test_*.py` ≈ 30, `tests/unit/` 35, `tests/integration/`
44 — dedupe — ~109 collected files; pytest count not run per
instructions).

### Existing FastAPI route test patterns

Two coexisting patterns; both are already in use:

1. **In-process ASGI via httpx**:
   `tests/integration/test_forge_intake.py:47-50, 615` —
   `from httpx import ASGITransport, AsyncClient; transport =
   ASGITransport(app=app); async with AsyncClient(transport=transport,
   base_url="http://test") as ac: ...`. **Use this for new
   `GET /` HTMX, `GET /metrics`, `GET /api/v1/tasks` tests.**
   Cheaper than a real socket; the lifespan still fires (so the
   event flusher / sweeps still run if invoked).
2. **Real uvicorn TCP socket** (only when proving the wire works):
   `tests/integration/test_phase5_remote.py:73-220` — picks a free
   port, starts `uvicorn.Server(uvicorn.Config(create_app(pool),
   host="127.0.0.1", port=port))`, spawns `whilly-worker` as a
   real subprocess against `http://127.0.0.1:<port>`. Reserved for
   the M1 cross-host smoke test.

### Where new tests live

* **HTMX dashboard** —
  `tests/integration/test_dashboard_web.py`. ASGITransport client +
  seed plans/tasks via the `task_repo` fixture, assert HTML body
  contains row for each task id.
* **SSE stream** —
  `tests/integration/test_events_stream.py`. ASGITransport with
  `client.stream("GET", "/events/stream", ...)` then iterate
  `response.aiter_lines()`. Seed an event by calling a
  state-transition endpoint or by running the existing FAIL/COMPLETE
  flow.
* **Prometheus metrics** —
  `tests/integration/test_metrics.py`. ASGITransport,
  `await client.get("/metrics")`, assert `# TYPE
  tasks_claimed_total counter` lines + counter increments after
  exercising routes.
* **Bootstrap-tokens SQL** —
  `tests/integration/test_bootstrap_tokens.py`. Uses `task_repo`
  fixture; exercises `mint_bootstrap_token / revoke / list`.
* **Admin CLI** —
  `tests/unit/test_cli_admin.py` (parser argv assertions, dry-run)
  and `tests/integration/test_cli_admin_e2e.py` (against
  `task_repo`).
* **Per-user bootstrap auth path** —
  `tests/integration/test_per_user_bootstrap_auth.py`. Mirror
  `test_per_worker_auth.py` (which is the proven template for
  the legacy-fallback semantics — VAL-AUTH-030/031/034).
* **Worker connect (one-line bootstrap CLI)** —
  `tests/unit/test_cli_worker_connect.py` (parser + env
  resolution + keychain shim). For an end-to-end keychain test,
  monkeypatch `keyring` per the existing pattern in
  `whilly/secrets.py:54-66`.
* **Migration `008` / `009` / `010`** —
  `tests/integration/test_alembic_008.py`, `_009.py`, `_010.py`
  (templates: existing `test_alembic_004.py` / `_005.py` /
  `_006.py` / `_007.py`).

---

## 4. Docker compose structure

### Files today

* `docker-compose.demo.yml` (root, 191 lines) — three services:
  `postgres` (loopback `127.0.0.1:5432:5432`), `control-plane`
  (loopback `127.0.0.1:8000:8000`, built from `Dockerfile.demo`),
  `worker` (no container_name so `--scale worker=N` works). Plus
  optional `seed` profile (one-shot plan import).
* `docker-compose.yml` (root, smaller) — older minimal compose
  (kept for back-compat).
* `Dockerfile` (production, 220 lines) — multi-stage, builds
  `mshegolev/whilly:4.3.1` for amd64+arm64, ships
  `claude-code/gemini-cli/opencode/codex` agentic CLIs in image,
  copies `docker/entrypoint.sh` to `/usr/local/bin/whilly-entrypoint`,
  default CMD `["control-plane"]`, exposes 8000, healthcheck on
  `127.0.0.1:8000/health`.
* `Dockerfile.demo` — demo flavour (includes test fixtures).
* `docker/entrypoint.sh` — role dispatcher (`control-plane | worker
  | migrate | shell | <other>`).
* `docker/control_plane.py` — production launcher (opens pool,
  calls `create_app`, runs `uvicorn.Server`).
* `docker/cli_adapter.py`, `docker/llm_shim.py`,
  `docker/llm_resource_picker.py` — agentic CLI plumbing inside
  the worker container.

### Forking docker-compose.demo.yml safely (M1)

**Backwards-compat strategy.** `docker-compose.demo.yml` stays
**unchanged** (one-host demo + workshop). New files added
side-by-side:

* `docker-compose.control-plane.yml` (new) — keep `postgres` and
  `control-plane` services. Drop the `worker` and `seed` services.
  Default `ports: ["127.0.0.1:8000:8000"]` (LAN demo); document
  override via `WHILLY_BIND_HOST=0.0.0.0` env (read by the
  `control-plane` service through new env passthrough). The
  Caddy reverse-proxy is **a `profiles: ["caddy"]` service** in
  the same file (M2) so it ships disabled by default.
* `docker-compose.worker.yml` (new) — single `worker` service.
  Reads `WHILLY_CONTROL_URL` from `.env.worker`, otherwise
  identical to today's `worker` block (env vars / bootstrap-token
  flow). Mounts a host workspace dir (planned for M4 — leave a
  comment placeholder).

The demo file already binds postgres+control-plane to
`127.0.0.1` (`docker-compose.demo.yml:42, 76`). The new
`docker-compose.control-plane.yml` should keep that as the safe
default and rely on `WHILLY_BIND_HOST` to opt into `0.0.0.0` —
see §6 below.

### Worker connect flow (M1)

Today's auto-registration in `docker/entrypoint.sh:75-138`:

1. `wait_for_db` is skipped (worker doesn't talk DB).
2. Curl-loop until `$WHILLY_CONTROL_URL/health` is 200.
3. If `WHILLY_WORKER_TOKEN` unset → call
   `whilly worker register --connect $URL --bootstrap-token $T
   --hostname $(hostname)`, awk-parse the two
   `key: value` lines, export `WHILLY_WORKER_ID` /
   `WHILLY_WORKER_TOKEN`.
4. `exec whilly-worker --connect ... --token ... --plan ...`.

For `whilly worker connect <url>` (one-line bootstrap CLI):

* The shell flow above is the contract. The new CLI should produce
  identical results when run *outside* a container — bash logic
  becomes Python.
* Skeleton: a `whilly worker connect` subcommand that
  - Prompts (or reads `--bootstrap-token` flag / env) for the
    cluster-join secret.
  - POST `/workers/register` via `RemoteWorkerClient.register`
    (existing — `whilly/cli/worker.py:_async_register` is the
    template).
  - Stores `worker_id` + plaintext bearer in OS keychain via
    `keyring.set_password("whilly", control_url,
    json.dumps({...}))` — `keyring>=24.0` is **already** in base
    deps (`pyproject.toml:31-33`). The `whilly.secrets` module's
    `_resolve_keyring` (`whilly/secrets.py:54-66`) is the existing
    consumer pattern; `connect` mirrors with `keyring.set_password`.
  - Exec `whilly-worker --connect <url> --token <bearer>` (replace
    current process via `os.execvp` so the operator's terminal
    becomes the worker's foreground).

Wire-in in entrypoint: when `WHILLY_USE_CONNECT_FLOW=1` (or
default), entrypoint just `exec whilly worker connect` instead of
the bash awk-parse. Backwards-compat: keep the old block under an
env switch so existing `docker-compose.demo.yml` invocations don't
shift behaviour.

---

## 5. CLI dispatch

### Dispatcher pattern

`whilly/cli/__init__.py:55-125` — `main(argv=None)` reads the first
positional, branches on string equality, lazy-imports the matching
sub-CLI's `run_<x>_command(rest)` function. Lazy imports are
**required** by `.importlinter` contract (worker-only install must
not pull in fastapi / asyncpg).

Existing branches:
- `plan`, `run`, `dashboard`, `init`, `forge`, `worker` (with
  `worker register` sub-dispatch at lines 107-125).

`whilly-worker` is the standalone console script
(`whilly/cli/worker.py:main`, `pyproject.toml:64`). Its `main()`
(worker.py:541-557) likewise sub-dispatches `register` first.

### Adding `whilly admin <cmd>` (M2)

1. New module `whilly/cli/admin.py`. Mirror
   `whilly/cli/worker.py:build_register_parser` /
   `run_register_command` — the canonical small-CLI shape: an
   argparse parser builder, a `run_<>_command(argv)` handler that
   returns int exit code, an `_async_<>` helper that opens a pool
   (or http client) and does the work.
2. Sub-dispatch: in `whilly/cli/__init__.py`, after the `worker`
   branch (line 125), add:
   ```python
   if cmd == "admin":
       from whilly.cli.admin import run_admin_command
       return run_admin_command(rest)
   ```
3. Inside `run_admin_command(argv)` use a sub-sub-parser pattern
   (`argparse` subparsers): `admin bootstrap mint`,
   `admin bootstrap revoke`, `admin bootstrap list`,
   `admin worker revoke <id>`. Each leaf is an `async def
   _async_admin_<verb>(repo, **kwargs)` that opens a pool via
   `whilly.adapters.db.create_pool(dsn)` (where `dsn =
   os.environ["WHILLY_DATABASE_URL"]`), constructs
   `TaskRepository(pool)`, calls the new repo methods (§2 above).
4. Output: same `key: value` shape as
   `whilly worker register`'s stdout (worker.py:506-510). Tests
   `grep '^token:'` it. **No JSON output by default** — that's the
   established convention.

### Adding `whilly worker connect <url>`

A new subcommand inside `whilly/cli/worker.py` next to `register`:

* `build_connect_parser()` — flags: `--bootstrap-token`
  (env: `WHILLY_WORKER_BOOTSTRAP_TOKEN`), `--plan`, `--hostname`,
  `--keychain-service` (default `whilly`).
* `run_connect_command(argv)` — calls register
  (already `_async_register` exists at worker.py:511-540), then
  `keyring.set_password(...)`, then `os.execvp` into
  `whilly-worker --connect ... --token ... --plan ...`.
* Sub-dispatch in `whilly/cli/worker.py:main` and in
  `whilly/cli/__init__.py:108-118` (add `connect` alongside
  `register`).

### Interactive-CLI patterns

* **No global "interactive" wrapper.** PRD wizard
  (`whilly/prd_wizard.py`) and `whilly init` use raw `input()` /
  `getpass.getpass()` plus subprocess-execv into `claude`.
* **Confirmations** — none built-in; `--yes` flags or env opt-outs
  are the project's convention. `admin worker revoke` should
  follow that (a `--yes` flag for non-interactive use).

### Keychain integration

* `keyring>=24.0` in base deps (pyproject.toml:33).
* Existing consumer: `whilly/secrets.py:54-66`
  (`_resolve_keyring` reads via `keyring.get_password(service,
  user)`, default user `"default"`). `whilly worker connect`
  writes via the symmetric `keyring.set_password`. Use
  `service="whilly"` and `user=<control-url>` so multiple
  control planes coexist.
* Failure handling: today `_resolve_keyring` swallows ImportError
  / OSError and warns. `connect` should do the opposite —
  hard-fail if `keyring.set_password` raises **unless**
  `--no-keychain` is passed (operator can echo the bearer to
  stdout for manual `.env` setup).

---

## 6. Config / env vars

### `WhillyConfig.from_env()`

`whilly/config.py:71-176` (loaded from
`load_layered(...)` — defaults < user TOML < repo TOML < .env <
shell env < CLI flags). Keys are explicit dataclass fields with
`WHILLY_` prefix. **The control-plane process does not read this**
— it goes through `whilly/adapters/transport/server.py`'s
factory kwargs / env (`WHILLY_WORKER_TOKEN`,
`WHILLY_WORKER_BOOTSTRAP_TOKEN`, `WHILLY_HOST`, `WHILLY_PORT`,
`WHILLY_LOG_LEVEL`, etc.). `WhillyConfig` is the *legacy in-process*
loop's config; the v4 distributed shape uses bare env vars.

### Adding `WHILLY_BIND_HOST`

* **Read site:** `docker/control_plane.py:52`
  (`host = os.environ.get("WHILLY_HOST", "0.0.0.0")`). Today
  `WHILLY_HOST` defaults to `0.0.0.0` — that's the *production*
  default and we keep it (image runs in containers; binding to
  `0.0.0.0` inside a container is correct, port exposure is
  controlled by compose).
* **New env var `WHILLY_BIND_HOST`** is for the **compose-level**
  port mapping in `docker-compose.control-plane.yml`. Default
  `127.0.0.1` for safety. Plumbing:
  ```yaml
  ports:
    - "${WHILLY_BIND_HOST:-127.0.0.1}:8000:8000"
  ```
  No Python code change needed for that part.
* If we *also* want the in-container uvicorn to honour it (e.g.
  if someone runs control_plane outside Docker on a host where
  `0.0.0.0` is too aggressive), add a tiny bridge:
  `host = os.environ.get("WHILLY_BIND_HOST",
  os.environ.get("WHILLY_HOST", "0.0.0.0"))` — `WHILLY_BIND_HOST`
  wins when set.
* Documentation site: update `.env.example` (already 9 KB, lots
  of `WHILLY_*` examples — append a stanza explaining LAN-only
  vs cluster-exposed).
* No `WhillyConfig` dataclass change required; the distributed
  shape doesn't go through it.

### Fail-safe defaults pattern

Two coexisting patterns in the codebase:

* **`_resolve_token` / `_resolve_optional_token` style**
  (`server.py:283-330`) — explicit kwarg > env > raise (or `None`).
  Strips whitespace; rejects empty-after-strip as misconfiguration.
* **`os.environ.get("X", default)` inline** — used by
  `docker/control_plane.py` for non-secret config knobs.

The `_resolve_token` pattern is the right template for any new
config that **must** be non-empty when set (e.g. a future
`WHILLY_ADMIN_TOKEN` legacy fallback).

---

## 7. Observability hooks

### Logging setup

* **Single bootstrap point** for the control plane:
  `docker/control_plane.py:80-87` — plain
  `logging.basicConfig(level=os.environ.get(
  "WHILLY_LOG_LEVEL", "INFO").upper(), format="%(asctime)s
  %(levelname)-7s %(name)s %(message)s")`.
* **No structured JSON logger.** No `structlog`, no
  `python-json-logger`. Routes log via
  `logger = logging.getLogger(__name__)` (server.py:218,
  auth.py:79) using f-string-with-args style for arg formatting.
* Worker side: `whilly-worker` inherits `WHILLY_LOG_LEVEL` and
  goes through the same `logging.basicConfig`.

### Prometheus

* `prometheus-client` is **not** present in `pyproject.toml`'s
  base / `[server]` / `[worker]` extras. **No conflict** to add it
  — a single line in the `[server]` extras list:
  `"prometheus-client>=0.20"`.
* **No existing metrics module.** Natural place: new
  `whilly/api/metrics.py` next to `event_flusher.py` and
  `main.py` in `whilly/api/`. Define `Counter` / `Gauge` /
  `Histogram` instances at module level (default REGISTRY is
  fine). Import them from route handlers
  (`server.py:1034-1346`'s claim/complete/fail/release blocks)
  to `.inc()` on successful transitions, and from the new
  `_metrics_refresh_loop` lifespan coroutine to refresh
  `worker_status_gauge` / `claim_queue_depth_gauge` /
  `plan_spent_usd_gauge` from a single `SELECT … GROUP BY status`
  every 10-30 s.
* The pattern is **single point of registration** (one module),
  scattered `.inc()` calls. Keeps the metric inventory greppable.

### SSE NOTIFY trigger

See §2 above — the schema has no triggers today, no functions, no
LISTEN/NOTIFY usage. The single-trigger migration sketched there
plus a single dedicated listener connection in the lifespan
TaskGroup is the smallest possible delta.

The lifespan-owned **`EventFlusher`** is the canonical async event
emit pattern (`whilly/api/event_flusher.py:170-353`). It does
multi-row `INSERT INTO events ... VALUES (...)` once per 100 ms
or 500-row batch. Adding the trigger means *every* row flushed
through this batcher fires `pg_notify` once on commit — no
additional Python plumbing needed on the *publish* side. The
listener side is a separate concern (one connection,
`add_listener`, fan-out queue) wired into the lifespan TaskGroup.

### Worker logs

* Stdout/stderr only. No log shipping. `WHILLY_LOG_LEVEL` is the
  only knob.
* `whilly worker register`'s grep-able stdout shape
  (worker.py:506-510) is the established
  "machine-readable-output" pattern; extend for any new
  one-shot CLI commands rather than introducing JSON.

---

## Summary: extension-friendliness assessment

Rank each new endpoint/feature on a 1-5 scale of "how invasively
does it touch existing code" (1 = drop-in addition; 5 = touches
many existing files / contracts):

| Feature                                               | Score | Touches |
|--|--|--|
| `GET /metrics` (Prometheus) endpoint (M3)             | **1** | One new module `whilly/api/metrics.py`, single line in `[server]` extras, one new endpoint inside `create_app`, sprinkle `.inc()` on existing routes. No schema, no auth change, no contract churn. |
| `GET /api/v1/tasks` listing (auth-bearer) (M3)        | **1** | One new endpoint inside `create_app`, reuses existing `bearer_dep`. New SQL constant in `repository.py` next to `_SELECT_DASHBOARD_ROWS_SQL`. |
| `WHILLY_BIND_HOST` env var (M1)                       | **1** | Compose YAML only; optional 1-line change in `docker/control_plane.py:52`. |
| Splitting compose into control-plane + worker (M1)    | **1** | Pure additive — new YAMLs, demo file unchanged. |
| HTMX dashboard `GET /` (M3)                           | **2** | Requires `jinja2` in `[server]` extras + `whilly/api/templates/` directory. Reuses existing dashboard SQL. New endpoint, no new models. |
| `whilly worker connect <url>` CLI + keychain (M1)     | **2** | New subcommand module pattern matches `register`. Keyring already a dep. `os.execvp` re-uses existing worker entry. Update entrypoint.sh under env switch. |
| Caddy reverse-proxy compose profile (M2)              | **2** | Pure compose addition (`profiles: ["caddy"]` block in `docker-compose.control-plane.yml`). No Python change. |
| `whilly admin bootstrap` CLI commands (M2)            | **2** | New `whilly/cli/admin.py` module, dispatcher branch, repo methods. Mirrors existing `worker register` shape. |
| `workers.owner_email` column (M2)                     | **2** | One migration; one column add to `_INSERT_WORKER_SQL`; small param plumbed through `register_worker(...)` and `RegisterRequest` schema. Reference `schema.sql` text update. |
| `bootstrap_tokens` table + `make_admin_auth` (M2)     | **3** | New migration + new table + 4-5 new SQL constants + 4-5 new repo methods + new `make_admin_auth` factory in `auth.py` + lifespan wiring (one new dep var on `app.state`) + retrofit `make_bootstrap_auth` to consult the table (with legacy env fallback for one minor version, mirroring the proven VAL-AUTH-030/031/034 contract). Plus tests for each surface. |
| `GET /events/stream` SSE via LISTEN/NOTIFY (M3)       | **3** | New migration (one trigger + one function). New lifespan coroutine owning a dedicated asyncpg connection (outside the pool). New endpoint with `StreamingResponse`. Fan-out queue on `app.state`. Test pattern (`client.stream(...).aiter_lines()`) is new but well-known. |

### Top 5 risks / gotchas

* **Lifespan TaskGroup is a single supervision boundary** — any new
  coroutine added there must catch `Exception` per tick (not
  `BaseException`) and use the shared `sweep_stop` event. A coroutine
  that raises uncaught will crash the *whole* app on shutdown via
  `ExceptionGroup`. Template at server.py:329-403.
* **`create_app` is a single 750-line function** with closures over
  ~20 kwargs. New endpoints add to that closure. There are
  no `APIRouter` modules — converting to routers would be a
  refactor, **not** required for M1+M2+M3 but worth flagging if
  the endpoint count goes >5.
* **Schema drift between `schema.sql` and migrations is a
  maintainer responsibility** (CI drift-check is TASK-029 and not
  yet wired). Every migration in M2/M3 must hand-update
  `whilly/adapters/db/schema.sql` in the same commit.
* **`.importlinter` contract** restricts the worker import path
  to `httpx + pydantic + whilly.core + whilly.adapters.transport.client`.
  M2's `bootstrap_tokens` SQL methods land in `repository.py` —
  worker-only installs must never import that, which is already
  enforced. Just don't accidentally import `repository.py` from
  `whilly/adapters/transport/client.py`.
* **Tests use `testcontainers` Postgres**; CI gate is
  `DOCKER_REQUIRED`. `pytest --collect-only` is fine offline,
  full integration suite needs Docker. Unit tests for argparse /
  dataclass shapes are the cheap path; SQL/repo/route tests need
  the live container.
