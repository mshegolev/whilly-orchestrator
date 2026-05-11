# M1+M2+M3 Tech Research — Whilly Distributed

> Research conducted 2026-05-01. Sources cited inline. All code snippets target Python 3.10+ / Caddy v2.x / Tailscale 1.52+ / HTMX 2.x / asyncpg 0.29+. This is a worker-facing knowledge artifact: copy-paste-ready snippets + the gotchas that bite in production.

---

## 1. Caddy v2 reverse-proxy + ACME

### Recommended Caddyfile (control-plane behind Caddy, no Caddy-side auth)

`auth lives at FastAPI` — Caddy only does TLS termination, HSTS, security headers, and reverse-proxy. Bearer tokens flow through unchanged.

```caddyfile
# Caddyfile — saved at /etc/caddy/Caddyfile inside the container
# Replace <host> at deploy time. Examples:
#   203-0-113-42.sslip.io     (sslip.io / nip.io style, IPv4 dashes)
#   whilly.example.com        (real domain)
{
    # Global options
    email ops@example.com         # used by Let's Encrypt for expiry warnings
    # acme_ca https://acme-staging-v02.api.letsencrypt.org/directory   # uncomment for staging
}

{$WHILLY_PUBLIC_HOST} {
    # ACME HTTP-01 happens automatically on :80 (Caddy redirects HTTP→HTTPS by default)

    encode gzip zstd

    # Security headers — Caddy does NOT set these by default
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options    "nosniff"
        X-Frame-Options           "DENY"
        Referrer-Policy           "strict-origin-when-cross-origin"
        # Hide upstream identification
        -Server
    }

    # SSE endpoints need long-lived streaming — disable buffering and bump timeouts.
    @sse path /events/stream /workers/*/stream
    reverse_proxy @sse whilly-control-plane:8000 {
        flush_interval -1
        transport http {
            read_timeout  24h
            write_timeout 24h
        }
    }

    # Everything else (incl. /metrics, /api/*, /dashboard, /htmx fragments)
    reverse_proxy whilly-control-plane:8000 {
        header_up X-Forwarded-Proto {scheme}
        header_up X-Real-IP         {remote_host}
    }
}
```

Key points:
- **`flush_interval -1`** is mandatory for SSE — without it Caddy buffers the response and `text/event-stream` arrives in chunks (sources: Caddy `reverse_proxy` docs).
- **`Authorization`** header is forwarded by default; do NOT add `basicauth` / `forward_auth` — auth is FastAPI's job.
- HSTS **only** after you confirm cert issuance succeeds, otherwise you can lock yourself out (browsers cache HSTS even for ACME-staging-issued certs).

### `docker-compose.caddy.yml` profile

Make Caddy opt-in via Docker Compose `profiles:` so `docker compose up` (default) does not pull it.

```yaml
# docker-compose.caddy.yml — fragment merged into existing compose project
# Activate with:  docker compose --profile caddy up -d
services:
  caddy:
    image: caddy:2-alpine          # multi-arch official image
    profiles: ["caddy"]             # ← opt-in profile
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"               # HTTP/3
    environment:
      WHILLY_PUBLIC_HOST: ${WHILLY_PUBLIC_HOST}   # e.g. 203-0-113-42.sslip.io
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data            # cert + ACME account (PERSIST THIS)
      - caddy_config:/config
    networks: [whilly]
    depends_on:
      - whilly-control-plane

volumes:
  caddy_data: {}
  caddy_config: {}
```

Gotchas:
- **`caddy_data` volume MUST persist** — ACME accounts and issued certs live there. Losing it ⇒ a fresh cert request on next start ⇒ Let's Encrypt rate-limit territory.
- **Profile flag must be named consistently** (`--profile caddy`); without it, Caddy is invisible to `docker compose up` (which is what we want for the demo).
- Compose v2.20+ supports `profiles:` natively. Older versions silently ignore.

### ACME against sslip.io / nip.io

- sslip.io and nip.io **are interchangeable** (same operator, same nameservers).
- Format: `<ip-with-dashes>.sslip.io` (e.g., `203-0-113-42.sslip.io`) or `<ip-with-dots>.sslip.io`. Dashes are recommended (some software splits on dots).
- Let's Encrypt **HTTP-01** challenge works (Caddy uses this by default when port 80 is reachable).
- **Wildcard certs are NOT supported** by sslip.io (would need DNS-01; nip.io/sslip.io don't expose a DNS-API). Only single-hostname certs.
- **Rate limits** (Let's Encrypt prod):
  - 50 certs / registered-domain / week — and `nip.io` / `sslip.io` count as a single registered domain *for everyone*. That's why sslip.io's homepage explicitly says "if you get rate-limited, file a GitHub issue and we'll request an increase." sslip.io has had its rate limit raised by Let's Encrypt many times.
  - 5 duplicate certs / week
  - 300 new orders / 3 hours / account
- For **demo / dev**, prefer `acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` first to avoid eating into the prod rate limit while iterating. Browsers will warn (untrusted root) — that's expected.

### Recommendation

| Use case | Choice |
|---|---|
| Local demo on a public VPS, no domain | `sslip.io` HTTP-01 (acceptable rate-limit risk for low cert churn) |
| Stable demo URL, control over rate limit | `duckdns.org` HTTP-01 or DNS-01 (per-account subdomain, much smaller blast radius) |
| Production / repeat issuance | Real domain with DNS provider supporting ACME DNS-01 |

### Sources
- Caddy reverse-proxy quickstart — https://caddyserver.com/docs/quick-starts/reverse-proxy
- Caddy `reverse_proxy` directive (incl. `flush_interval`) — https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- Caddy automatic HTTPS — https://caddyserver.com/docs/automatic-https
- sslip.io homepage (rate-limit + wildcard policy) — https://sslip.io/
- Let's Encrypt rate limits — https://letsencrypt.org/docs/rate-limits/
- Caddy Docker image — https://hub.docker.com/_/caddy
- Compose `profiles` — https://docs.docker.com/compose/profiles/

---

## 2. Tailscale Funnel

### Setup commands (1.52+ syntax)

Funnel exposes one local port to the **public** internet via a `*.ts.net` hostname. It can be run on the host or as a sidecar container.

```bash
# 1. Install + auth Tailscale on the host
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname whilly-cp     # opens browser for login

# 2. Confirm Funnel is permitted on this tailnet
#    (Admin console → DNS → enable HTTPS certs; Settings → enable Funnel)

# 3. Start funnel — ports are restricted to 443, 8443, or 10000
sudo tailscale funnel --bg --https=443 http://localhost:8000

# Resulting URL (same shape on every tailnet):
#   https://whilly-cp.<your-tailnet>.ts.net
# Reachable by ANYONE on the public internet, no Tailscale install needed.

# Status / off
tailscale funnel status --json
sudo tailscale funnel --https=443 off
```

### The serve-vs-funnel distinction

| Command | Audience | Auth |
|---|---|---|
| `tailscale serve` | Tailnet members only | Implicit via WireGuard identity |
| `tailscale funnel` | **Public internet** | None at the Tailscale layer (TLS terminate only) |

⚠ **`tailscale funnel` does NOT add an auth layer.** It only does TLS termination + reverse-proxy to `localhost:<port>`. Bearer-token / cookie auth must be enforced by FastAPI itself. Funnel does NOT see / strip the `Authorization` header.

### Public URL reachability

Confirmed: the `https://<machine>.<tailnet>.ts.net` URL is a normal public hostname (Tailscale runs an authoritative DNS for `ts.net`). It resolves and serves TLS to **any** browser, not only Tailscale-connected devices. Funnel uses Let's Encrypt under the hood; certs are auto-renewed by `tailscaled`.

### Constraints (from official docs)

- Allowed ports: **`443`, `8443`, `10000` only**. No other ports work for HTTPS / TCP-TLS-terminated Funnel. (`tailscale funnel command` docs.)
- Funnel must be enabled at the **tailnet** (Admin) level; it is opt-in.
- Funnel availability depends on Tailscale plan; Personal/Free includes Funnel for limited use.
- Reverse-proxy target must be **`http://127.0.0.1:<port>`** (or `https://`/`https+insecure://`). Other hosts not supported.
- Persists across reboot only when `--bg` was used.

### Docker pattern: sidecar vs host

**Recommendation: tailscale-on-host** for our control-plane.

| Pattern | Pros | Cons |
|---|---|---|
| **Tailscale on host** (preferred) | Simple; tailscaled runs as system service; zero changes to compose; can also serve other host services | Couples to host OS; needs root once for install |
| **Tailscale sidecar container** (`tailscale/tailscale`) | Isolated; per-app identity | Needs `cap_add: net_admin` + `/dev/net/tun`; envs `TS_AUTHKEY`, `TS_HOSTNAME`, `TS_EXTRA_ARGS=--funnel`; PROXY-protocol gymnastics if you also want client IP |

If sidecar is needed, the canonical recipe (Tailscale docs):

```yaml
services:
  tailscale:
    image: tailscale/tailscale:stable
    hostname: whilly-cp
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY}              # ephemeral / reusable from admin console
      TS_EXTRA_ARGS: "--advertise-tags=tag:whilly"
      TS_SERVE_CONFIG: /config/serve.json    # JSON encoding of `tailscale funnel` config
      TS_STATE_DIR: /var/lib/tailscale
    volumes:
      - tailscale_state:/var/lib/tailscale
      - ./deploy/ts-serve.json:/config/serve.json:ro
    cap_add: [net_admin]
    devices: ["/dev/net/tun:/dev/net/tun"]
  whilly-control-plane:
    network_mode: "service:tailscale"        # share network namespace with sidecar
    # ...
volumes:
  tailscale_state: {}
```

### Sources
- `tailscale funnel` CLI docs (ports, flags, status, off) — https://tailscale.com/kb/1311/tailscale-funnel
- `tailscale serve` docs — https://tailscale.com/docs/reference/tailscale-cli/serve
- Tailscale Funnel feature page — https://tailscale.com/kb/1223/funnel
- Funnel examples — https://tailscale.com/docs/reference/examples/funnel
- Sidecar Docker pattern (`TS_SERVE_CONFIG`) — https://tailscale.com/kb/1453/quick-guide-docker

---

## 3. asyncpg LISTEN/NOTIFY → SSE

### The canonical pattern

**One dedicated asyncpg connection** subscribes to a Postgres channel and fans out into per-subscriber `asyncio.Queue` instances. SSE handlers `await queue.get()` and yield events.

Why one connection: `asyncpg.Pool` connections are reused; `LISTEN` state lives on the connection, so a pooled connection that gets returned would silently lose subscriptions. **Pin LISTEN to a dedicated long-lived connection outside the pool.**

### Trigger SQL

```sql
-- Generic notify-on-change trigger.
-- Payload: JSON ({op, id, plan_id, ts}). Stay under 8000 bytes/payload (Postgres limit)
-- — store full row in events table; payload contains only the lookup key.
CREATE OR REPLACE FUNCTION notify_event() RETURNS trigger AS $$
DECLARE
    payload JSONB;
BEGIN
    payload := jsonb_build_object(
        'op',      TG_OP,
        'id',      NEW.id,
        'plan_id', NEW.plan_id,
        'ts',      extract(epoch FROM now())
    );
    PERFORM pg_notify('whilly_events', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_notify ON events;
CREATE TRIGGER events_notify
AFTER INSERT ON events
FOR EACH ROW EXECUTE FUNCTION notify_event();
```

Multiple channels (e.g., per plan_id) work, but **one channel + filter in app code** is far simpler and scales to thousands of plans.

### Server pattern (the broker)

```python
# whilly_orchestrator/control_plane/sse_broker.py
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import asyncpg

log = logging.getLogger(__name__)


class EventBroker:
    """One asyncpg LISTEN connection -> per-subscriber asyncio.Queue."""

    def __init__(self, dsn: str, channel: str = "whilly_events") -> None:
        self._dsn = dsn
        self._channel = channel
        self._conn: asyncpg.Connection | None = None
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        # Dedicated connection, NOT from a pool.
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(self._channel, self._on_notify)
        log.info("SSE broker listening on channel=%s", self._channel)

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.remove_listener(self._channel, self._on_notify)
            await self._conn.close()
        for q in list(self._subscribers):
            q.put_nowait({"type": "__close__"})
        self._subscribers.clear()

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("non-JSON payload on %s: %s", self._channel, payload[:200])
            return
        # Fan out. Drop on full queue (slow client) — never block the LISTEN coroutine.
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping event for slow subscriber")

    @asynccontextmanager
    async def subscribe(self, maxsize: int = 256):
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)
```

### FastAPI integration with `sse-starlette`

`sse-starlette` (`pip install sse-starlette`) is the de-facto choice — it implements correct flushing, heartbeat, `Last-Event-ID` parsing, and keeps the connection alive through proxies.

```python
# whilly_orchestrator/control_plane/api/events.py
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ..sse_broker import EventBroker
from ..deps import get_broker, get_repo

router = APIRouter()


@router.get("/events/stream")
async def stream_events(
    request: Request,
    broker: EventBroker = Depends(get_broker),
    repo = Depends(get_repo),
):
    last_event_id = request.headers.get("Last-Event-ID")  # int as string

    async def event_generator():
        # 1. Reconcile: replay anything we missed since last_event_id.
        if last_event_id and last_event_id.isdigit():
            async for ev in repo.iter_events_since(int(last_event_id)):
                yield {"id": str(ev.id), "event": ev.kind, "data": ev.payload_json}

        # 2. Subscribe to live stream.
        async with broker.subscribe() as queue:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}    # heartbeat
                    continue
                if ev.get("type") == "__close__":
                    break
                # Look up the row (payload only carries the id) and serialize HTML / JSON.
                row = await repo.fetch_event(ev["id"])
                yield {
                    "id": str(row.id),
                    "event": row.kind,
                    "data": row.html_fragment,        # for hx-sse swap; or JSON for native EventSource
                }

    return EventSourceResponse(event_generator(), ping=15)
```

### Lifespan wiring

```python
# whilly_orchestrator/control_plane/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .sse_broker import EventBroker
from .config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = EventBroker(settings.database_url)
    await broker.start()
    app.state.broker = broker
    try:
        yield
    finally:
        await broker.stop()

app = FastAPI(lifespan=lifespan)
```

### Reconnect strategy (`Last-Event-ID`)

LISTEN/NOTIFY is **lossy across reconnects** — Postgres does not buffer notifies for a disconnected listener, and our broker is in-process (no delivery guarantee).

The robust pattern:

1. Persist every domain event to an `events` table with a monotonically-increasing `id` (`bigserial` or `bigint generated always as identity`).
2. The notify trigger fires only on insert, payload includes `id`.
3. SSE response sets `id:` header on every message (mirrors `events.id`).
4. Browser reconnect automatically sends `Last-Event-ID`.
5. Server replays `SELECT * FROM events WHERE id > :last_event_id ORDER BY id` *before* subscribing to the live broker.
6. There is a tiny race between "reconcile finished" and "subscribe started" — solve by reading `MAX(id)` *before* subscribe and re-running the reconcile if any rows arrived between SELECT and subscribe (or just deduplicate on the client by id).

### Connection pool exhaustion

- The LISTEN connection is **separate** from `asyncpg.Pool`. Don't burn pool slots on it.
- Each SSE request must NOT hold a pool connection for its lifetime — only reach into the pool transiently for the reconcile and per-event lookups, then `await conn.release()` immediately.
- For high concurrency, consider increasing `pool.max_size` and lowering it back via `pool.acquire(timeout=2)` so a slow SSE handler doesn't starve the rest of the API.

### Sources
- asyncpg `add_listener` / `LISTEN` — https://magicstack.github.io/asyncpg/current/api/index.html#asyncpg.connection.Connection.add_listener
- Postgres `pg_notify` (8 KB payload limit) — https://www.postgresql.org/docs/current/sql-notify.html
- `sse-starlette` (FastAPI integration, `EventSourceResponse`) — https://github.com/sysid/sse-starlette
- FastAPI SSE example — https://fastapi.tiangolo.com/tutorial/server-sent-events/
- MDN `EventSource` (spec for `Last-Event-ID`) — https://developer.mozilla.org/en-US/docs/Web/API/EventSource

---

## 4. Prometheus `/metrics` for control-plane

### Recommended package

**`prometheus-fastapi-instrumentator`** (PyPI: `prometheus-fastapi-instrumentator`, latest 7.1.0). It gives you the canonical HTTP-level metrics (request count / latency / size) for free, plus a clean hook (`add()`) for our custom ones. Uses `prometheus_client` underneath, so any custom metrics you create with `Counter` / `Gauge` / `Histogram` register on the same registry.

Hand-rolled `make_asgi_app()` is fine but you'll re-implement label sanitization, untemplated-route grouping, and `inprogress` gauge — all of which the instrumentator already covers.

### Setup

```python
# whilly_orchestrator/control_plane/observability.py
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# ── Custom metrics — registered on the default registry ───────────────────────
CLAIMS_TOTAL = Counter(
    "whilly_claims_total",
    "Tasks successfully claimed by a worker.",
    labelnames=("plan_id", "worker_id"),
)
COMPLETES_TOTAL = Counter(
    "whilly_completes_total",
    "Tasks completed successfully.",
    labelnames=("plan_id", "worker_id"),
)
FAILS_TOTAL = Counter(
    "whilly_fails_total",
    "Tasks reported failed.",
    labelnames=("plan_id", "worker_id", "reason"),
)
WORKERS_ONLINE = Gauge(
    "whilly_workers_online",
    "Number of workers with last_heartbeat < 30s.",
)
CLAIMS_PENDING = Gauge(
    "whilly_claims_pending",
    "Tasks in 'pending' status.",
    labelnames=("plan_id",),
)
PLAN_BUDGET_REMAINING_USD = Gauge(
    "whilly_plan_budget_remaining_usd",
    "Remaining budget per plan (USD).",
    labelnames=("plan_id",),
)
CLAIM_LONG_POLL_DURATION = Histogram(
    "whilly_claim_long_poll_duration_seconds",
    "Time a claim long-poll waits before returning a task or empty response.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

def setup_metrics(app):
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,        # avoid label explosion
        excluded_handlers=["/metrics", "/healthz"],
        inprogress_name="whilly_http_inprogress",
        inprogress_labels=False,
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
```

### Emitting from a repository class

Plain attribute access — Counters/Gauges are module-level singletons:

```python
# whilly_orchestrator/control_plane/repositories.py
from .observability import CLAIMS_TOTAL, COMPLETES_TOTAL, FAILS_TOTAL

class TaskRepository:
    def __init__(self, pool): self._pool = pool

    async def claim(self, worker_id: str) -> Task | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(SQL_CLAIM, worker_id)
        if row:
            CLAIMS_TOTAL.labels(plan_id=row["plan_id"], worker_id=worker_id).inc()
        return Task(**row) if row else None
```

For gauges that reflect aggregate state (e.g., `WORKERS_ONLINE`), it's cleaner to update them inside a periodic background task that runs `SELECT count(*) FROM workers WHERE last_heartbeat > now() - interval '30 seconds'` rather than from event hot-paths.

### Auth posture for `/metrics`

**Threat model split:**

| Deployment | Recommendation |
|---|---|
| Behind Tailscale Funnel (public TLS, but tailnet-curated identity *for management UI*) | Require bearer auth on `/metrics`, identical to the rest of the API. Public-internet-visible metrics endpoints leak business signals. |
| Behind Caddy on a public IP (sslip.io) | **Definitely** require auth. Either bearer or `IP allowlist` via Caddy `@allowed remote_ip 10.0.0.0/8` matcher. |
| Tailscale-only (`tailscale serve`, no Funnel) | Optional — tailnet identity is enough; bearer is overkill. |

Implementation: don't add `excluded_handlers=["/metrics"]` — instead leave `/metrics` covered by the auth dependency. With FastAPI, attach `dependencies=[Depends(require_bearer)]` to the included `instrumentator.expose(...)` route, or wrap with middleware that lets Prometheus scrape with a dedicated bearer (`WHILLY_METRICS_TOKEN`).

```python
# Bearer-on-/metrics example
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

bearer_scheme = HTTPBearer(auto_error=False)

async def require_metrics_token(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    expected = settings.metrics_token
    if not creds or creds.credentials != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

# Then wire after instrument():
instrumentator.expose(app, endpoint="/metrics", dependencies=[Depends(require_metrics_token)])
```

### Sources
- `prometheus-fastapi-instrumentator` README — https://github.com/trallnag/prometheus-fastapi-instrumentator
- `prometheus_client` (Counter / Gauge / Histogram API) — https://github.com/prometheus/client_python
- Prometheus instrumentation best practices — https://prometheus.io/docs/practices/instrumentation/
- Prometheus naming conventions — https://prometheus.io/docs/practices/naming/

---

## 5. HTMX patterns for live dashboards

### Polling vs SSE

| Approach | When to use |
|---|---|
| `hx-trigger="every 2s"` + `hx-get="/dashboard/workers/fragment"` | **Default**. Simple, survives proxies / mobile networks well, no server-side state. |
| `hx-ext="sse" sse-connect="..." sse-swap="EventName"` | When sub-second updates matter or you have many clients (broadcast cheaper than per-client polling). |

Concrete dashboard recipe (mixed): **poll for the table outline every 5s; SSE for per-row deltas**.

### Polling pattern (full-fragment row swap)

```html
<!-- templates/dashboard.html (Jinja2) -->
<table id="workers">
  <thead><tr><th>id</th><th>status</th><th>last seen</th></tr></thead>
  <tbody hx-get="/dashboard/workers/fragment"
         hx-trigger="every 5s"
         hx-swap="innerHTML">
    {% raw %}{% include "_worker_rows.html" %}{% endraw %}
  </tbody>
</table>
```

```python
# control_plane/api/dashboard.py
from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/dashboard/workers/fragment")
async def workers_fragment(request: Request, repo = Depends(get_repo)):
    workers = await repo.list_workers()
    return templates.TemplateResponse(
        "_worker_rows.html",
        {"request": request, "workers": workers},
    )
```

### SSE pattern (htmx-ext-sse 2.x)

```html
<head>
  <script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4"></script>
</head>
<body hx-ext="sse">
  <table>
    <tbody sse-connect="/events/stream"
           sse-swap="worker_update,worker_remove,task_claimed">
      <tr id="worker-row-abc"
          hx-target="this"
          hx-swap="outerHTML">
        <td>abc</td><td>online</td><td>3s ago</td>
      </tr>
    </tbody>
  </table>
</body>
```

Server emits an SSE event like:

```
id: 14021
event: worker_update
data: <tr id="worker-row-abc" hx-target="this" hx-swap="outerHTML"><td>abc</td><td>online</td><td>1s ago</td></tr>

```

The HTML fragment **must** include `id="worker-row-..."` so HTMX OOB-style targeting (and `hx-swap="outerHTML"`) replaces only that row. No JSON gymnastics.

### Jinja2 in FastAPI

```python
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
```

Hot reload: `uvicorn ... --reload --reload-dir templates --reload-dir static` picks up template / static changes. (Jinja2's autoescape is on by default for `.html` — keep it that way.)

### "Minimal CSS" recommendation

For a worker / sysadmin dashboard:

1. **`pico.css` (classless)** — cdn import, every default HTML element looks reasonable, dark-mode auto. Best dev-effort/look ratio.
   ```html
   <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
   ```
2. **Inline `<style>` block** in `base.html` — for ~20 lines of bespoke CSS. Avoids a build step.
3. **Raw HTML** — only acceptable if the dashboard is *truly* internal (5 ops humans). Not recommended; pico.css adds zero burden.

Pick **option 1** (`pico.css` classless) — it's CDN-loadable, ~20 KB gzipped, and doesn't fight HTMX swaps.

### Sources
- HTMX SSE extension (2.x, current) — https://htmx.org/extensions/sse/
- HTMX `hx-trigger` (`every 2s`) — https://htmx.org/attributes/hx-trigger/
- HTMX 2.0 release notes — https://htmx.org/posts/2024-06-17-htmx-2-0-0-is-released/
- FastAPI templates (Jinja2) — https://fastapi.tiangolo.com/advanced/templates/
- pico.css — https://picocss.com/

---

## 6. OS keychain integration in Python

### `keyring` — auto-detected backends

`pip install keyring`. The library picks a backend at import-time based on platform:

| Platform | Backend | Library used |
|---|---|---|
| macOS | `Keychain` (Apple Security framework) | none extra (built-in `Foundation` via `pyobjc-framework`-free path on recent versions) |
| Windows | `Windows Credential Locker` | `pywin32-ctypes` |
| Linux (GNOME / KDE) | `Secret Service` (D-Bus) | `secretstorage` (transitively `jeepney`) |
| Linux (KDE alternative) | `KWallet` | `dbus-python` (often fails to compile on `pip install`) |
| Headless server / no D-Bus | `keyring.backends.fail.Keyring` | n/a |

**Discovery order**: `keyring.get_keyring()` returns the chosen backend after probing. Override via env var `PYTHON_KEYRING_BACKEND=keyring.backends.SecretService.Keyring` or programmatically with `keyring.set_keyring(...)`.

### Idiomatic API

```python
import keyring

SERVICE = "whilly-orchestrator"

# Store a per-worker bearer
keyring.set_password(SERVICE, f"worker:{worker_id}", token)

# Retrieve
token = keyring.get_password(SERVICE, f"worker:{worker_id}")    # None if absent

# Remove
try:
    keyring.delete_password(SERVICE, f"worker:{worker_id}")
except keyring.errors.PasswordDeleteError:
    pass
```

### Fallback file (no usable backend)

Detect the `fail.Keyring` backend and fall through to a chmod-600 JSON store:

```python
# whilly_orchestrator/cli/secrets.py
import json
import os
import stat
from pathlib import Path

import keyring
from keyring.backends.fail import Keyring as FailKeyring

SERVICE = "whilly-orchestrator"
FALLBACK_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) \
                / "whilly" / "credentials.json"


def _backend_works() -> bool:
    return not isinstance(keyring.get_keyring(), FailKeyring)


def _read_fallback() -> dict[str, str]:
    if not FALLBACK_PATH.exists():
        return {}
    return json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))


def _write_fallback(data: dict[str, str]) -> None:
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FALLBACK_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)        # 0o600
    tmp.replace(FALLBACK_PATH)


def store_token(key: str, value: str) -> None:
    if _backend_works():
        keyring.set_password(SERVICE, key, value)
    else:
        data = _read_fallback()
        data[key] = value
        _write_fallback(data)


def get_token(key: str) -> str | None:
    if _backend_works():
        return keyring.get_password(SERVICE, key)
    return _read_fallback().get(key)
```

### Gotchas

- On macOS, the **first** call from a new binary triggers a Keychain UI prompt ("allow `whilly` to access keychain"); user must approve. Subsequent calls in the same session are silent.
- On Linux, the Secret Service backend requires a unlocked D-Bus session bus; on a headless box without a desktop environment the `fail.Keyring` is selected — fallback path is mandatory.
- Some CI runners (`secrets unavailable`) — use `WHILLY_TOKEN` env var as the highest-priority lookup before `keyring`.
- Don't store the token in process env then forward to subprocesses without scrubbing logs.

### Sources
- `keyring` PyPI / docs — https://pypi.org/project/keyring/, https://keyring.readthedocs.io/
- macOS Keychain Services overview — https://developer.apple.com/documentation/security/keychain_services
- Linux Secret Service spec — https://specifications.freedesktop.org/secret-service/

---

## 7. Free subdomain for ACME

### Comparison

| Service | Format | DNS-01 (wildcard) | HTTP-01 | Account needed | Rate-limit shielding |
|---|---|---|---|---|---|
| **sslip.io / nip.io** | `<ip-w-dashes>.sslip.io` | ❌ | ✅ | ❌ | LE rate-limit increases granted on request |
| **traefik.me** | `<ip>.traefik.me` | ❌ | ✅ | ❌ | Smaller / less proven |
| **DuckDNS** | `<name>.duckdns.org` | ✅ via DNS-01 with token | ✅ | ✅ (free, GitHub OAuth) | LE prod rate limits apply per-account; effectively unbounded for typical use |
| **freedns.afraid.org** | `<name>.<your-pick>.<tld>` | depends on subdomain owner | ✅ | ✅ | Variable |

### Recommended choice

**For automated Caddy + Let's Encrypt against a VPS public IP, the right answer depends on the deploy stability:**

| Need | Pick |
|---|---|
| Demo / one-off, no signup, public IP fixed | `sslip.io` (or `nip.io`) HTTP-01 |
| Reusable demo, IP may change, OK with signup | **DuckDNS** + Caddy DNS-01 (`caddy-dns/duckdns` plugin) — wildcard + per-account rate limits, IP refresh by GET to update URL |

### sslip.io setup (zero-signup)

```bash
# Find your IP
PUBLIC_IP=$(curl -fsSL https://ifconfig.me)

# Convert dots→dashes for safety
HOST="${PUBLIC_IP//./-}.sslip.io"     # e.g., 203-0-113-42.sslip.io
echo "$HOST"

# Ensure inbound 80 + 443 reachable on the VPS, then start Caddy
WHILLY_PUBLIC_HOST="$HOST" docker compose --profile caddy up -d
```

Caddy will solve HTTP-01, get a Let's Encrypt cert in seconds, and all subsequent requests get a real green padlock.

### DuckDNS setup (account, wildcard-capable)

1. Sign up at https://www.duckdns.org/ (GitHub login).
2. Create a name, e.g., `whilly-demo` → resolves to `whilly-demo.duckdns.org`.
3. Get your account token from the duckdns.org page.
4. Use a Caddy build that includes the DuckDNS DNS plugin (`caddy-dns/duckdns`) — easiest via the official `caddy:builder` image or `xcaddy`.
5. Caddyfile uses `tls { dns duckdns {env.DUCKDNS_TOKEN} }` for DNS-01 (wildcards possible: `*.whilly-demo.duckdns.org`).
6. A side-channel cron / systemd timer pings `https://www.duckdns.org/update?domains=whilly-demo&token=...&ip=` to keep the A-record current if dynamic.

### Rate-limit considerations

- `sslip.io` and `nip.io` cert-issuance shares one global Let's Encrypt registered-domain bucket. Short bursts of issuance from many users can rate-limit *you* even if you only request once. Mitigation: stable IP → cert is reused for 90 days.
- DuckDNS is per-account-name → effectively your own bucket. Strongly preferred for repeatedly-issuing CI environments.
- Always test against `acme-staging-v02` first.

### Sources
- sslip.io homepage — https://sslip.io/
- nip.io memorial page — https://nip.io/
- DuckDNS — https://www.duckdns.org/
- Caddy DuckDNS module (`caddy-dns/duckdns`) — https://github.com/caddy-dns/duckdns
- Let's Encrypt rate limits — https://letsencrypt.org/docs/rate-limits/
- Let's Encrypt staging — https://letsencrypt.org/docs/staging-environment/

---

## Summary: gotchas + non-obvious decisions

- **Caddy SSE requires `flush_interval -1`** in `reverse_proxy` *plus* `read_timeout 24h`. Without both, SSE arrives as one giant chunk after disconnect.
- **Tailscale Funnel terminates TLS but adds NO auth**. Funnel is only safe to expose if FastAPI itself enforces bearer / mTLS. Funnel ports are restricted to `443 / 8443 / 10000`.
- **LISTEN/NOTIFY is lossy**. Always pair with an `events` table + `Last-Event-ID` reconciliation. Pin LISTEN to a dedicated asyncpg connection (NOT pool) or you'll silently lose subscriptions when the pool churns.
- **sslip.io/nip.io share ONE Let's Encrypt rate-limit bucket** for the whole world. For low cert churn it's fine; for CI / demo-on-every-PR use DuckDNS or a real domain.
- **`prometheus-fastapi-instrumentator` is the right default**, but lock down `/metrics` with bearer auth when behind any public TLS terminator (Caddy, Funnel). Use a dedicated `WHILLY_METRICS_TOKEN` so Prometheus scrapers don't share the worker bearer.
- **HTMX SSE extension is now `htmx-ext-sse@2.2.4`** (separate package; `hx-sse` attribute is gone). Use `hx-ext="sse" sse-connect=... sse-swap=...`. Server fragments must include the row id and `hx-swap="outerHTML"` for surgical updates.
- **`keyring` falls back silently to a no-op (`fail.Keyring`)** on headless Linux. Detect with `isinstance(keyring.get_keyring(), FailKeyring)` and route to a chmod-600 file under `~/.config/whilly/credentials.json`. Always allow an env-var override (`WHILLY_TOKEN`) for CI.
- **Caddy `caddy_data` volume MUST persist** across container restarts — losing it forces reissuance and risks rate-limits.
- **Funnel sidecar containers need `cap_add: NET_ADMIN`, `/dev/net/tun`, and `network_mode: service:tailscale`** on the FastAPI container. On-host install is simpler — pick that unless you have a strong isolation requirement.
- **Postgres NOTIFY payloads are capped at 8000 bytes**. Always carry only an `id` + minimal metadata; full row goes through the `events` table on the SSE side.
