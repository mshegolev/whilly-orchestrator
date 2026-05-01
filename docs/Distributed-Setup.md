# Distributed Setup — VPS A control-plane, laptops B/C workers (M1)

> **Status:** Released in **v4.4** (M1 of the Whilly Distributed v5.0 mission).
> **Pairs with:** `docker-compose.control-plane.yml`, `docker-compose.worker.yml`,
> `whilly worker connect <url>`. The single-host workshop demo
> (`docker-compose.demo.yml` + `workshop-demo.sh`) is unchanged and continues
> to work identically — see [`DEMO.md`](../DEMO.md). M1 is purely additive.

This doc is a copy-paste-ready walkthrough for the **two-host** (or N-host)
deployment shape that lands in v4.4: one VPS runs the control-plane, two or
more laptops join as workers, and the operator watches the audit log fan out
across multiple `worker_id`s.

The end-state demo:

```
       +----------------------------+              +----------------------------+
       |  Host A: VPS (e.g. Hetzner)|  HTTP(S)     |  Host B: macbook /         |
       |  postgres + control-plane  |◄────────────►|  Host C: peer VM           |
       |  docker-compose            |  register +  |  whilly worker connect     |
       |     -f control-plane.yml   |  long-poll   |     <url>                  |
       +----------------------------+   /tasks/    +----------------------------+
                                       claim
```

For the design of the future per-worker editing workspace (M4), see the
companion document [`docs/Workspace-Topology.md`](Workspace-Topology.md). M1
intentionally does **not** implement that workspace; M1 only ships the
deployment artifacts that make a multi-host control-plane possible.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Two-host via Tailscale (recommended for private deployments)](#two-host-via-tailscale-recommended-for-private-deployments)
3. [VPS A — control-plane](#vps-a--control-plane)
4. [Laptop B / C — workers](#laptop-b--c--workers)
5. [Verifying the cluster](#verifying-the-cluster)
6. [Operating the cluster](#operating-the-cluster)
7. [Backwards compatibility](#backwards-compatibility)
8. [Reference: env vars added in v4.4](#reference-env-vars-added-in-v44)
9. [Audit reports](#audit-reports)

---

## Prerequisites

| Host | Required | Reason |
|---|---|---|
| VPS A | Docker 24+, Docker Compose v2 (the dash-separated `docker-compose` binary is fine), 1 GB RAM, 2 GB free disk, ports 80/443/8000 free, public IPv4 | Runs Postgres (256 MB) + control-plane (256 MB) under the M1 mission's 600 MB budget |
| Laptop B/C | Python 3.12+ with `whilly-orchestrator` installed (see below), or Docker for the worker container path, network reachability to VPS A on port 8000 (or 443 behind Caddy at M2) | Runs `whilly worker connect <url>` or `docker-compose -f docker-compose.worker.yml up` |

> **Default agent: opencode + Groq gpt-oss-120b (free tier).** Since
> v4.4 (`m1-opencode-groq-default`), worker containers ship with
> `WHILLY_CLI=opencode` and `WHILLY_MODEL=groq/openai/gpt-oss-120b`
> (~14k requests/day on the free tier). Get a free key at
> [https://console.groq.com](https://console.groq.com) and put it in
> `.env` (gitignored) as `GROQ_API_KEY=...` — **never commit a real
> key** anywhere. The worker fails fast at startup with a single-line
> diagnostic if `WHILLY_CLI=opencode` and `GROQ_API_KEY` is empty;
> override `WHILLY_MODEL=anthropic/claude-opus-4-6` (or any other
> non-groq provider) to opt out of the Groq path entirely.

Two install closures cover the worker side. Pick whichever fits the host:

```bash
# Python install (no Docker on the laptop required)
pip install 'whilly-orchestrator[worker]'

# Docker install (uses the same image as the control-plane)
docker pull mshegolev/whilly:4.4.0
```

> **TIP:** the worker install closure is intentionally narrow — it does
> NOT pull `fastapi` or `asyncpg`. The `.importlinter` `core-purity`
> contract enforces this on every release; a worker laptop never needs the
> server-side dependency tree.

---

## Two-host via Tailscale (recommended for private deployments)

If you do **not** want to expose the control-plane to the public internet
(no public IP, no ngrok, no Tailscale Funnel), the simplest two-host
deployment is to put both hosts on a private **Tailscale tailnet** and
let the worker reach the control-plane via its tailnet hostname. This is
the recommended path for the M1 demo when both hosts are personal /
private machines.

The worker container ships with `tailscaled` baked in (since v4.4 —
`m1-tailscale-worker-bootstrap`). When `TAILSCALE_AUTHKEY` is set in the
worker's `.env`, the container joins the tailnet at startup using
**userspace-networking** mode — no host TUN device, no `--privileged`,
no `NET_ADMIN` cap required. The worker then resolves the control-plane
via its tailnet hostname (e.g. `http://m-mac-pro:8000`).

### (a) Install Tailscale on the control-plane host

On the laptop / VPS / VM that runs the control-plane (Postgres +
control-plane container):

```bash
# macOS:
brew install tailscale && sudo tailscale up
# Debian / Ubuntu:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Note the **magic-DNS hostname** Tailscale assigns the host — for example
`m-mac-pro`, `mvs-laptop`, or `vps-eu-1`. You can verify with:

```bash
tailscale status
# 100.64.0.1   m-mac-pro            you@         macOS    -
```

That hostname is what the worker container will use to reach the
control-plane (via Tailscale's MagicDNS, no DNS records to maintain).

### (b) Generate a worker auth key on admin.tailscale.com

Visit [https://login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)
and create a new auth key with these settings:

* **Reusable**: yes — multiple workers can share the same key
* **Ephemeral**: yes (recommended) — worker nodes auto-deregister when
  the container stops, so the tailnet stays clean
* **Pre-approved**: yes (skip MFA prompt)
* **Tags**: `tag:whilly-worker` — the worker entrypoint advertises this
  tag via `--advertise-tags=tag:whilly-worker`, so the auth key MUST
  permit it (configure in your tailnet ACL: `"tagOwners": {"tag:whilly-worker": ["autogroup:admin"]}`)

The key has the form `tskey-auth-...`. Treat it like an SSH key — never
commit it. Copy it into the worker host's `.env` (or `.env.worker`) in
the next step.

### (c) Configure the worker host's `.env`

On the **worker** host (the host that will run `docker-compose -f
docker-compose.worker.yml up -d`):

```bash
cd /path/to/whilly-orchestrator
cp .env.worker.example .env.worker
$EDITOR .env.worker
```

The minimum set of variables for the tailnet path:

```env
# .env.worker
WHILLY_CONTROL_URL=http://m-mac-pro:8000      # tailnet magic-DNS hostname
WHILLY_WORKER_BOOTSTRAP_TOKEN=<cluster bootstrap token>
TAILSCALE_AUTHKEY=tskey-auth-...               # tagged + ephemeral + reusable (paste yours)
WHILLY_USE_CONNECT_FLOW=1                     # use the connect flow
WHILLY_INSECURE=1                             # plain HTTP over tailnet (loopback-bypass)
WHILLY_PLAN_ID=demo
```

> **Why `WHILLY_INSECURE=1`?** Plain HTTP over a Tailscale tailnet is
> end-to-end encrypted at the WireGuard layer. The
> `--insecure` flag here only bypasses Whilly's own URL-scheme guard
> (which otherwise rejects plain HTTP to a non-loopback hostname); the
> traffic still rides the encrypted tailnet. If you prefer end-to-end
> TLS on top, add Caddy in front of the control-plane (M2) and drop
> the flag.

### (d) Start the worker container

```bash
docker-compose -f docker-compose.worker.yml --env-file .env.worker up -d
docker logs whilly-worker
```

You should see the entrypoint announce the tailnet bootstrap:

```
[entrypoint] role=worker
[entrypoint] joining tailnet hostname=whilly-worker-<container-id> (userspace-networking)
[entrypoint] tailnet up; magic-DNS hostname=whilly-worker-<container-id>
[entrypoint] waiting for control plane at http://m-mac-pro:8000/health
[entrypoint] control plane is up
```

### (e) Verify the tailnet path

Inside the running worker container:

```bash
docker exec whilly-worker tailscale status
# 100.64.0.1   m-mac-pro                you@           macOS    -
# 100.64.0.5   whilly-worker-abc123     tagged-devices linux    active

docker exec whilly-worker curl -sf "${WHILLY_CONTROL_URL}/health"
# {"status":"ok"}
```

Both lines confirm the worker is on the tailnet and can reach the
control-plane via its tailnet hostname.

> **Backwards compatibility.** If `TAILSCALE_AUTHKEY` is unset (the
> default), the worker container behaves exactly as before — no
> tailscaled, no tailnet, direct HTTP to `WHILLY_CONTROL_URL`. The
> opt-in is one env var.

> **Slim worker image without Tailscale.** Operators who never use the
> tailnet path can shrink the image by passing
> `--build-arg WHILLY_INCLUDE_TAILSCALE=0` at build time:
>
> ```bash
> docker buildx build --target worker \
>     --build-arg WHILLY_INCLUDE_TAILSCALE=0 \
>     -t whilly-worker:no-ts .
> ```

---

## VPS A — control-plane

Everything below runs as root on the VPS. The default config keeps the
API on `127.0.0.1` (loopback only), which is the LAN-safe default for
Tailscale / VPN deployments. The two most common public-facing options
(`WHILLY_BIND_HOST=0.0.0.0` for plain HTTP, or Caddy + sslip.io at M2 for
HTTPS) are both one env var away.

### 1. Clone the repo

```bash
ssh root@vps.example.com
cd /root
git clone https://github.com/mshegolev/whilly-orchestrator.git whilly
cd whilly
git checkout v4.4.0
```

### 2. Create a per-cluster bootstrap secret

```bash
mkdir -p /root/whilly/secrets
openssl rand -hex 32 > /root/whilly/secrets/bootstrap.token
chmod 600 /root/whilly/secrets/bootstrap.token
export WHILLY_WORKER_BOOTSTRAP_TOKEN="$(cat /root/whilly/secrets/bootstrap.token)"
```

The bootstrap token is the cluster-join secret. It only authenticates
`POST /workers/register`; per-worker bearers are minted server-side and
stored in each worker's OS keychain. The token can be rotated at any
time without invalidating already-registered workers (per FR-1.2 split,
see [`whilly/adapters/transport/auth.py`](../whilly/adapters/transport/auth.py)).

### 3. Pick a bind interface

```bash
# Default (loopback only — safe for Tailscale / VPN).
unset WHILLY_BIND_HOST

# Expose on all IPv4 interfaces (e.g. plain HTTP + LAN demo, or before
# Caddy is in front).
export WHILLY_BIND_HOST=0.0.0.0

# IPv6 dual-stack (Linux: ``[::]:8000`` listener).
export WHILLY_BIND_HOST=::

# Bind only to a specific LAN IP.
export WHILLY_BIND_HOST=10.0.0.5
```

Compose validates the value at port-mapping parse time — an invalid host
fails fast with stderr identifying the bind error, rather than silently
falling back to the wildcard.

### 4. Bring the control-plane up

```bash
# Modern Docker Compose v2 (recommended — `docker compose` with a space):
docker compose -f docker-compose.control-plane.yml up -d
docker compose -f docker-compose.control-plane.yml ps
docker compose -f docker-compose.control-plane.yml logs -f control-plane

# Legacy v1 ``docker-compose`` (dash) binary still works identically:
docker-compose -f docker-compose.control-plane.yml up -d
docker-compose -f docker-compose.control-plane.yml ps
docker-compose -f docker-compose.control-plane.yml logs -f control-plane
```

> **Note on the binary name.** Compose v2 ships as a `docker` subcommand
> (`docker compose ...`, with a space). The standalone `docker-compose`
> (dash form, v1) is end-of-life upstream but still works on hosts that
> retained it. The compose files themselves are byte-equivalent for
> both invocations — pick whichever your VPS image already has.

Within ~60 s both `postgres` and `control-plane` should be `running`,
with `postgres` reaching `healthy`. From the VPS itself:

```bash
curl -fsS http://127.0.0.1:8000/health
# {"status":"ok"}
```

If you set `WHILLY_BIND_HOST=0.0.0.0`, a `curl` from your laptop should
also succeed:

```bash
curl -fsS http://vps.example.com:8000/health
```

### 5. Import a plan

```bash
docker-compose -f docker-compose.control-plane.yml exec control-plane \
    whilly plan import examples/demo/tasks.json
docker-compose -f docker-compose.control-plane.yml exec control-plane \
    whilly plan show demo
```

The control-plane is multi-tenant per `plan_id`; you can import as many
plans as you like and steer each worker at a specific one with
`--plan <id>`.

---

## Laptop B / C — workers

This is the one-line bootstrap that distinguishes v4.4 from v4.3.1. Each
laptop registers, persists its per-worker bearer in the OS keychain, and
becomes a long-running worker process.

### Option 1 — Native install (`whilly worker connect`)

```bash
pip install 'whilly-orchestrator[worker]'

whilly worker connect http://vps.example.com:8000 \
    --bootstrap-token "$WHILLY_WORKER_BOOTSTRAP_TOKEN" \
    --plan demo \
    --hostname "$(hostname)" \
    --insecure   # dev-only: opts out of the loopback-only HTTP guard
```

> ⚠️ `--insecure` here is a **dev-only loopback-bypass**: the
> `whilly-worker` URL-scheme guard otherwise rejects plain HTTP to a
> non-loopback host (see the warning blockquote below for the full
> details and the recommended HTTPS path that lands in **M2**).

Stdout shows two `key: value` lines (line-oriented and pipeable):

```
worker_id: w-XXXXXXXX
token: <plaintext bearer>
```

After printing those, the process `execvp`s into `whilly-worker` —
foreground PID 1 of the operator's shell becomes the worker loop. The
bearer is also written to the OS keychain (macOS Keychain, Linux
Secret Service, Windows Credential Manager) under
`service="whilly", user=<canonical control URL>`. On a headless Linux
host (no D-Bus), the bearer is written to `~/.config/whilly/credentials.json`
at mode `0600` instead.

> **Plain HTTP to a non-loopback host** is rejected up front with
> `--insecure` advice in stderr. Pass `--insecure` (as shown in the
> snippet above) to acknowledge the risk if you really must use
> plaintext over the LAN — this is a **dev-only loopback-bypass**.
> HTTPS is the recommended production path; once **M2** lands the
> Caddy + ACME / Tailscale Funnel story, drop `--insecure` and point
> the worker at the `https://` URL instead.

If the OS keychain is unavailable and the fallback file write also
fails, the bearer is still printed to stdout — capture it manually and
pass it to `whilly-worker --token <bearer>` later.

### Option 2 — Docker (`docker-compose.worker.yml`)

If the laptop has Docker but no Python, the worker can run as a
container.

```bash
cp .env.worker.example .env.worker
$EDITOR .env.worker        # set WHILLY_CONTROL_URL, WHILLY_WORKER_BOOTSTRAP_TOKEN

docker-compose -f docker-compose.worker.yml --env-file .env.worker up -d
docker logs whilly-worker
```

The container's entrypoint runs the legacy bash-awk register flow by
default (`WHILLY_USE_CONNECT_FLOW` unset / `0`). To exercise the new
`whilly worker connect` path inside the container, set
`WHILLY_USE_CONNECT_FLOW=1` in `.env.worker` — the entrypoint then
delegates URL validation, registration, keychain persistence, and exec
to the same Python codepath that `pip`-installed laptops use.

```env
# .env.worker
WHILLY_USE_CONNECT_FLOW=1
WHILLY_CONTROL_URL=http://vps.example.com:8000
WHILLY_WORKER_BOOTSTRAP_TOKEN=<paste cluster bootstrap token here>
WHILLY_PLAN_ID=demo
```

> **Truthiness rules.** The entrypoint accepts `1`, `true`, `yes`,
> `on` (case-insensitive) as truthy. Empty / unset / `0` / `false` /
> `no` / `off` are falsy and keep the legacy path. Mirrors what the
> rest of the entrypoint already does for `WHILLY_INSECURE`.

---

## Verifying the cluster

Once both laptops are connected, you should see two distinct
`worker_id`s in the audit log on the VPS:

```bash
docker-compose -f docker-compose.control-plane.yml exec postgres \
    psql -U whilly -d whilly -c \
    "SELECT DISTINCT worker_id FROM events
     WHERE event_type='CLAIM' AND plan_id='demo';"
```

A 5-task `demo` plan should drain across both workers within a couple
of minutes (depending on the agentic CLI / stub binary in use). Final
state should show all 5 tasks `DONE` and at least two distinct
`worker_id`s contributing `COMPLETE` events:

```bash
docker-compose -f docker-compose.control-plane.yml exec postgres \
    psql -U whilly -d whilly -c \
    "SELECT status, count(*) FROM tasks
     WHERE plan_id='demo'
     GROUP BY status;"
```

---

## Operating the cluster

### Disconnect / reconnect a worker

`Ctrl-C` on the laptop's foreground process triggers a graceful
release: the worker emits a `RELEASE` event for its current claim and
exits. The control-plane's offline-worker sweep picks up the released
claim within ≤150 s and re-offers it to other workers.

### Re-running connect

Re-running `whilly worker connect` against the same control-plane URL
mints a *new* `worker_id` row server-side and overwrites the keychain
entry locally — the old bearer no longer authenticates. The keychain
key is the canonical control URL (trailing slashes stripped) so two
runs against `http://vps:8000/` and `http://vps:8000` resolve to the
same entry.

### Memory budget

On the 964 MB-RAM VPS profile, expect:

| Service | Cap | Typical RSS |
|---|---|---|
| postgres | 256 MB | 80–120 MB |
| control-plane | 256 MB | 60–100 MB |
| (Caddy at M2) | 64 MB | 30–50 MB |

Validate with `docker stats --no-stream` after the demo run.

---

## Backwards compatibility

v4.4 is strictly additive. Specifically:

* `docker-compose.demo.yml` is byte-for-byte unchanged from v4.3.1.
* `mshegolev/whilly:4.3.1` continues to pass `bash workshop-demo.sh --cli claude`.
* `docker/entrypoint.sh` defaults to the legacy bash-awk register path;
  the new `whilly worker connect` codepath is only taken when
  `WHILLY_USE_CONNECT_FLOW` is truthy.
* All v3-era CLI flags continue to dispatch correctly. `whilly --tasks tasks.json`,
  `whilly --headless`, `whilly --resume`, `whilly --reset` all still work.

If anything in your existing single-host workflow regresses against
v4.4, that is a bug — please open an issue.

---

## Reference: env vars added in v4.4

| Variable | Default | Purpose |
|---|---|---|
| `WHILLY_BIND_HOST` | `127.0.0.1` | Host interface the control-plane's port 8000 is mapped to. Set to `0.0.0.0` (IPv4 wildcard), `::` (IPv6 wildcard), or any explicit interface IP to expose the API beyond loopback. |
| `WHILLY_USE_CONNECT_FLOW` | unset (legacy) | When truthy (`1`, `true`, `yes`, `on`), the worker container's entrypoint uses `whilly worker connect` instead of the legacy bash-awk register flow. Default OFF preserves byte-equivalent v4.3.1 stderr/stdout. |
| `WHILLY_WORKER_HOSTNAME` | `whilly-worker` | Hostname the worker self-reports during register. Surfaces in the `workers` table and event payloads — set this to something humans can grep (`macbook-mvs`, `vps-eu-1`). |
| `TAILSCALE_AUTHKEY` | unset (no-op) | When set to a Tailscale tagged auth key (`tskey-auth-…`), the worker container joins a private tailnet at startup using userspace-networking. Required tag: `tag:whilly-worker`. See §"Two-host via Tailscale" above. |
| `TAILSCALE_HOSTNAME` | `whilly-worker-<container-hostname>` | Optional override for the tailnet hostname the worker advertises. Defaults to `whilly-worker-$(hostname)` so multiple workers on the same image stay distinct in `tailscale status`. |

Both new variables are documented in [`.env.example`](../.env.example)
and on each compose file's header comment block.

---

## Reference: Dockerfile build-args (image build-time)

The `Dockerfile` in this repo exposes a build-arg on **both** image
targets that controls which agent CLIs are pre-installed in the image.
This is a fallback / size-optimization escape hatch for constrained
build environments (e.g. a Colima VM with limited disk) — default
builds preserve zero functional regression.

| Build-arg | Stage | Default | Purpose |
|---|---|---|---|
| `WHILLY_AGENT_CLIS` | `runtime` (multi-role image, `mshegolev/whilly:<version>`) | `@anthropic-ai/claude-code @google/gemini-cli opencode-ai @openai/codex` | Space-separated list of npm packages to install with `npm install -g`. |
| `WHILLY_AGENT_CLIS` | `worker` (worker-only image) | `opencode-ai` | Same — but the worker stage's default reflects v4.4's opencode-by-default policy (m1-opencode-groq-default). |
| `WHILLY_INCLUDE_TAILSCALE` | both | `1` | When `1` (default) installs the tailscale + tailscaled static binaries into `/usr/local/bin/`, enabling the `TAILSCALE_AUTHKEY` runtime opt-in. Pass `0` for slim builds without tailscale. |
| `TAILSCALE_VERSION` | both | `1.74.1` | Pinned Tailscale release used by the static-binary install. Bump when upgrading. |

### Examples

```bash
# Slim worker image with only opencode (== current default; explicit form):
docker buildx build --target worker \
    --build-arg WHILLY_AGENT_CLIS='opencode-ai' \
    -t whilly-worker:slim .

# Worker image with NO npm-installed CLIs (operator BYOs the binary via
# volume-mount or follow-on RUN layer):
docker buildx build --target worker \
    --build-arg WHILLY_AGENT_CLIS='' \
    -t whilly-worker:no-clis .

# Slim multi-role image: only opencode + claude-code on PATH (skip gemini
# and codex to fit the image into a disk-constrained build VM):
docker buildx build \
    --build-arg WHILLY_AGENT_CLIS='opencode-ai @anthropic-ai/claude-code' \
    -t whilly:slim .
```

> **NOTE.** When `WHILLY_AGENT_CLIS=''` is passed, the build-time sanity
> check that normally validates `opencode --version` is also skipped —
> there is no binary to probe. Default builds retain the existing
> sanity check unchanged.

---

## Audit reports

The mission's distributed-systems audit reports live at the canonical
mirror [`library/distributed-audit/`](../library/distributed-audit/),
which is byte-equal to the working copy under
`.planning/distributed-audit/` and the legacy `docs/distributed-audit/`
mirror retained for backwards-compatibility:

* `current-state.md` — what v4.3.1 already does for distributed deploys.
* `gap-analysis.md` — what's missing and why M1/M2/M3 close those gaps.
* `extension-surfaces.md` — concrete extension points in the codebase.
* `research-findings.md` — referenced upstream patterns / RFCs / SDKs.
* `readiness-deps.md` — package-readiness check results.
* `readiness-validation.md` — surface-readiness check results.

The mirror is regenerated idempotently via
[`scripts/m1_baseline_fixtures.py`](../scripts/m1_baseline_fixtures.py); a
re-run on a clean checkout is a no-op.
