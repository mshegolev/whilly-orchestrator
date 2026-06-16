## Purpose

The web-status-ui capability governs Whilly's HTTP control-plane surface: the
FastAPI app that distributed workers talk to over the network (registration,
task claim, terminal-state RPCs, heartbeat) plus the operator-facing read/write
surface layered on the same app (plans API, tasks API, Prometheus metrics, the
Server-Sent Events stream, the dashboard static mount) and the standalone
stdlib localhost status server. It is reverse-spec'd from
`whilly/adapters/transport/{server,client,auth,schemas}.py`,
`whilly/api/{main,plans_api,tasks_api,tasks_api_crud,metrics,sse,sse_endpoint,event_flusher,static_mount}.py`,
`whilly/cli/server.py`, and `whilly/web_status.py`.

This capability covers only the *transport-level* auth split — the
shared bootstrap token used to join the cluster versus the per-worker bearer
token used by every steady-state RPC — and the HTTP/SSE/status endpoints
themselves. The full operator authentication model (browser sessions, session
cookies, OIDC, WebAuthn, dashboard JWT minting) is owned by the `auth-security`
capability (Phase 26); this spec references that boundary and does not re-spec
it. The Rich Live terminal dashboard is owned by `dashboard-tui`; operator log
views are owned by `operator-views-logs`; this spec references them rather than
duplicating their rendering behavior.

## Requirements

### Requirement: Per-worker bearer auth on steady-state RPCs
The system SHALL require a valid per-worker bearer token, read from the
`WHILLY_WORKER_TOKEN` env var (legacy shared fallback) or resolved against the
`workers` table by token hash, on every steady-state worker RPC — task claim,
complete, fail, release, and heartbeat — and SHALL reject any request whose
bearer matches neither a registered per-worker token nor the configured legacy
token.

#### Scenario: Valid per-worker bearer authenticates a claim
- **WHEN** a worker calls `POST /tasks/claim` with `Authorization: Bearer <token>`
  whose SHA-256 hash matches a row in the `workers` table
- **THEN** the system SHALL resolve the bearer to that `worker_id`, stash it on
  `request.state.authenticated_worker_id`, and process the claim
- **AND** the system SHALL NOT emit the legacy-token deprecation warning on this
  per-worker path

#### Scenario: Unknown bearer is rejected
- **WHEN** a worker calls a steady-state RPC with a bearer that matches no
  registered worker token and no configured legacy token
- **THEN** the system SHALL respond `401 Unauthorized` with detail `invalid token`

### Requirement: Bootstrap-token auth on worker registration
The system SHALL gate `POST /workers/register` on the cluster-join bootstrap
secret — a per-operator `bootstrap_tokens` row or the legacy
`WHILLY_WORKER_BOOTSTRAP_TOKEN` env var — which is a distinct secret from the
per-worker bearer so an operator can rotate either independently.

#### Scenario: Bootstrap token mints a fresh worker identity
- **WHEN** a fresh worker (with no per-worker credentials yet) calls
  `POST /workers/register` with a valid bootstrap token
- **THEN** the system SHALL mint a new `worker_id` and per-worker bearer token,
  persist only the SHA-256 hash of the bearer in the `workers` table, and return
  the plaintext bearer exactly once in the response body

#### Scenario: Bootstrap and bearer secrets are independent
- **WHEN** an operator rotates the bootstrap secret
- **THEN** the system SHALL continue to accept already-issued per-worker bearer
  tokens on steady-state RPCs
- **AND** the system SHALL reject new `POST /workers/register` calls bearing the
  old bootstrap secret

### Requirement: 401 envelope carries WWW-Authenticate
The system SHALL respond `401 Unauthorized` with a
`WWW-Authenticate: Bearer realm="whilly"` header whenever the `Authorization`
header is missing, uses a non-Bearer scheme, carries an empty token, or carries
a token that does not match any accepted credential.

#### Scenario: Missing bearer header
- **WHEN** a worker calls a bearer-gated RPC with no `Authorization` header
- **THEN** the system SHALL respond `401 Unauthorized` with
  `WWW-Authenticate: Bearer realm="whilly"` and detail `missing bearer token`

#### Scenario: Non-Bearer scheme
- **WHEN** a request supplies `Authorization: Basic <...>` to a bearer-gated RPC
- **THEN** the system SHALL respond `401 Unauthorized` with
  `WWW-Authenticate: Bearer realm="whilly"` and detail `invalid authorization scheme`

### Requirement: Auth dependency fast-fails at startup on missing config
The system SHALL resolve the auth tokens once, when the control-plane app is
constructed, and SHALL raise a configuration error during app construction —
not on the first request — when a required secret is absent or whitespace-only,
naming the offending env var.

#### Scenario: Missing required token aborts app construction
- **WHEN** `create_app` is invoked and the required bootstrap secret is supplied
  neither as a kwarg nor via its env var
- **THEN** the system SHALL raise a `RuntimeError` naming the env var before any
  route serves traffic

#### Scenario: Whitespace-only token is treated as unset
- **WHEN** a token env var is set to a whitespace-only value
- **THEN** the system SHALL treat it as unset and fast-fail rather than binding
  an empty-string credential that would accept any client

### Requirement: Constant-time bearer comparison
The system SHALL compare presented bearer tokens against expected secrets using
a constant-time comparison (`secrets.compare_digest`) so the response time does
not leak how many leading bytes of a candidate token were correct.

#### Scenario: Wrong token rejected without timing leak
- **WHEN** a request supplies a bearer that differs from the expected secret
- **THEN** the system SHALL run a constant-time comparison over the longer of the
  two inputs and reject the request with `401 Unauthorized`

### Requirement: Worker registration and heartbeat RPCs
The system SHALL expose `POST /workers/register` to join the cluster and
`POST /workers/{worker_id}/heartbeat` to report liveness, the latter gated by
the per-worker bearer and surfacing the repository's boolean result as a 200
response body.

#### Scenario: Heartbeat for a still-registered worker
- **WHEN** a registered worker calls `POST /workers/{worker_id}/heartbeat` with
  its valid bearer
- **THEN** the system SHALL update `last_heartbeat` and return `200 OK` with
  `{"ok": true}`

#### Scenario: Heartbeat for a no-longer-registered worker
- **WHEN** a worker whose row has been removed calls its heartbeat endpoint with
  an otherwise-valid bearer
- **THEN** the system SHALL return `200 OK` with `{"ok": false}` so the caller
  re-registers rather than crashing

### Requirement: Long-polled task claim with 204 on timeout
The system SHALL hold `POST /tasks/claim` open for up to the configured
long-poll budget, polling the repository at the configured interval, and SHALL
return `200 OK` with the claimed task payload when a PENDING row transitions to
CLAIMED or `204 No Content` when the budget expires without a claim.

#### Scenario: Claim succeeds within the budget
- **WHEN** a PENDING task becomes claimable during the long-poll window
- **THEN** the system SHALL return `200 OK` carrying the post-claim task payload

#### Scenario: Claim times out idle
- **WHEN** no PENDING task becomes claimable before the long-poll budget elapses
- **THEN** the system SHALL return `204 No Content` so the worker can re-poll
  without decoding a body

### Requirement: Terminal-state RPCs with optimistic concurrency
The system SHALL expose `POST /tasks/{task_id}/complete` and
`POST /tasks/{task_id}/fail`, each accepting the `version` the worker last
observed, applying it as an optimistic lock, returning `200 OK` with the
post-update payload on success and `409 Conflict` carrying the conflict tuple
(`task_id`, `expected_version`, `actual_version`, `actual_status`) on a version
mismatch.

#### Scenario: Complete with the current version succeeds
- **WHEN** a worker calls `POST /tasks/{task_id}/complete` with the version that
  matches the task's current IN_PROGRESS row
- **THEN** the system SHALL mark the task DONE, increment its version, and return
  `200 OK` with the updated payload

#### Scenario: Stale version conflicts
- **WHEN** a worker calls a terminal-state RPC with a version that no longer
  matches the stored row
- **THEN** the system SHALL return `409 Conflict` with an error envelope carrying
  the expected and actual version and the actual status

### Requirement: Unauthenticated health probe
The system SHALL expose `GET /health` without authentication, returning
`200 OK` with `{"status": "ok"}` only after a successful database round-trip and
`503 Service Unavailable` when the database link is down.

#### Scenario: Healthy database reports ok
- **WHEN** an external probe calls `GET /health` while the asyncpg pool answers
  `SELECT 1`
- **THEN** the system SHALL return `200 OK` with `{"status": "ok"}`

#### Scenario: Database failure reports unavailable
- **WHEN** the database round-trip fails on `GET /health`
- **THEN** the system SHALL return `503 Service Unavailable` with a status of
  `unavailable` rather than a misleading bare 200

### Requirement: Plans API list, create, and update with If-Match
The system SHALL expose the operator plans surface — `GET /api/v1/plans` to
list, `POST /api/v1/plans` to create (returning `201 Created`), and
`PATCH /api/v1/plans/{plan_id}` to update — where the PATCH requires an
`If-Match` header and applies ETag-based optimistic concurrency.

#### Scenario: Create returns 201 with an ETag
- **WHEN** an authenticated operator `POST`s a valid new plan to `/api/v1/plans`
- **THEN** the system SHALL persist the plan and return `201 Created` with the
  plan payload and an `ETag` header

#### Scenario: PATCH without If-Match is rejected
- **WHEN** an operator calls `PATCH /api/v1/plans/{plan_id}` with no `If-Match`
  header
- **THEN** the system SHALL return `428 Precondition Required`

#### Scenario: PATCH with a stale If-Match is rejected
- **WHEN** an operator calls `PATCH /api/v1/plans/{plan_id}` with an `If-Match`
  ETag that no longer matches the current row
- **THEN** the system SHALL return `412 Precondition Failed` carrying the current
  ETag

### Requirement: Tasks API paginated listing
The system SHALL expose `GET /api/v1/tasks` returning a plan's tasks as a
paginated, status-filterable list where each row carries its current `version`
and any human-review annotation, accepting worker-bearer, dashboard-token, or
session-cookie credentials per the auth chain.

#### Scenario: Paginated task page for a plan
- **WHEN** an authenticated caller requests `GET /api/v1/tasks?plan_id=<id>` with
  a `limit`
- **THEN** the system SHALL return a page of task rows, each including its
  `version`, plus a forward cursor when more rows remain

#### Scenario: Invalid status filter rejected
- **WHEN** the caller passes a `status` filter value that is not a legal task
  status
- **THEN** the system SHALL return `400 Bad Request` enumerating the valid status
  values

### Requirement: Prometheus metrics endpoint
The system SHALL expose orchestration metrics at `GET /metrics` in the
Prometheus text exposition format, instrumenting claim/complete/fail counters
and the claim long-poll duration, optionally gated by a configured metrics
token.

#### Scenario: Metrics scrape returns Prometheus text
- **WHEN** a scraper calls `GET /metrics`
- **THEN** the system SHALL return the registry contents with the Prometheus
  `text/plain; version=0.0.4` content type

### Requirement: Server-Sent Events stream
The system SHALL stream control-plane events over `GET /events/stream` as
Server-Sent Events with per-event id and event-type framing, honoring the
`Last-Event-ID` header to replay committed events (capped at the replay limit,
with a synthetic truncation frame when exceeded) before handing the subscriber
to live per-subscriber broker fan-out fed by the event flusher.

#### Scenario: Subscriber streams live events
- **WHEN** an authenticated client opens `GET /events/stream`
- **THEN** the system SHALL register a per-subscriber queue and emit each event
  with its `event_id` and `event_type` framing

#### Scenario: Resume replays missed events
- **WHEN** a client reconnects with a `Last-Event-ID` header
- **THEN** the system SHALL replay committed events with `id` greater than that
  value up to the replay cap before resuming live delivery
- **AND** the system SHALL emit a `replay_truncated` frame when the cap is hit

#### Scenario: Missing credential on the stream is rejected
- **WHEN** a client opens `GET /events/stream` with no accepted credential
- **THEN** the system SHALL reject the connection with `401 Unauthorized`

### Requirement: Read-only versus mutating endpoint boundary
The system SHALL keep unauthenticated or read-only surfaces (`GET /health`, the
single-plan `GET /api/v1/plans/{plan_id}` metadata read, `GET /metrics`, the
scheduler status read, the dashboard static mount) free of state mutation, and
SHALL require an accepted credential on every mutating or worker RPC and on the
human-facing plans/tasks CRUD surface.

#### Scenario: Public read surface mutates nothing
- **WHEN** a client calls a read-only endpoint such as `GET /health` or
  `GET /api/v1/plans/{plan_id}`
- **THEN** the system SHALL return data without altering any task, plan, or
  worker state

#### Scenario: Mutating endpoint requires a credential
- **WHEN** an unauthenticated client attempts a mutating endpoint such as
  `POST /tasks/{task_id}/complete` or `PATCH /api/v1/plans/{plan_id}`
- **THEN** the system SHALL reject the request with `401 Unauthorized` before any
  state change

### Requirement: Dashboard static assets and full auth model boundary
The system SHALL serve the browser dashboard's static assets via the FastAPI
static mount and SHALL delegate the full operator authentication model — browser
session establishment, session cookies, OIDC, WebAuthn, and dashboard JWT
minting — to the `auth-security` capability rather than defining it here.

#### Scenario: Static dashboard assets are served
- **WHEN** a browser requests a dashboard static asset under the mounted path
- **THEN** the system SHALL serve the file from the configured static directory

#### Scenario: Session auth is delegated, not duplicated
- **WHEN** the plans/tasks CRUD surface needs to authenticate a human operator's
  session cookie
- **THEN** the system SHALL defer to the session/OIDC/WebAuthn model defined by
  `auth-security` and only apply the transport bootstrap/bearer split defined here

### Requirement: whilly server boots the control plane
The system SHALL provide a `whilly server` subcommand that opens the database
pool, builds the FastAPI control plane via `create_app`, and serves it under
Uvicorn, exiting with an environment-error code when the database URL is not
configured.

#### Scenario: Server starts with a configured database URL
- **WHEN** an operator runs `whilly server` with `WHILLY_DATABASE_URL` set
- **THEN** the system SHALL open the pool, construct the app, and serve it on the
  configured host and port

#### Scenario: Missing database URL fails fast
- **WHEN** an operator runs `whilly server` with `WHILLY_DATABASE_URL` unset
- **THEN** the system SHALL print an error to stderr and exit with the
  environment-error code without starting Uvicorn

### Requirement: Standalone localhost web status server
The system SHALL provide a stdlib-only localhost status server (default port
9191) answering `GET /api/status` with a JSON snapshot of run progress and
`GET /` (and `/index.html`) with an auto-refreshing HTML dashboard, returning
`404` for any other path.

#### Scenario: JSON status snapshot
- **WHEN** a client calls `GET /api/status` on the localhost status server
- **THEN** the system SHALL return `200 OK` with a JSON body carrying done,
  total, failed, cost, elapsed, and active-agent fields

#### Scenario: HTML status page
- **WHEN** a client calls `GET /` on the localhost status server
- **THEN** the system SHALL return `200 OK` with the auto-refreshing HTML status
  page

#### Scenario: Unknown path returns 404
- **WHEN** a client requests any path other than the status JSON or HTML routes
- **THEN** the system SHALL return `404 Not Found`
