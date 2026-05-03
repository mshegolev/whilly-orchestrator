# Deploy M2 — Public-internet exposure (v4.5)

> **Status:** Released in **v4.5** (M2 of the Whilly Distributed v5.0
> mission). Pairs with `docker-compose.control-plane.yml` +
> `docker-compose.demo.yml` (M2 adds the profile-gated `funnel`
> sidecar service to both), the `bootstrap_tokens` table (migration
> 009), the `funnel_url` table (migration 010), and the `whilly admin`
> CLI namespace. The single-host workshop demo
> (`docker-compose.demo.yml` + `workshop-demo.sh`) is unchanged when
> the new `funnel` profile is OFF — see [`DEMO.md`](../DEMO.md).
>
> **Pivot note (2026-05-02).** The earlier draft of M2 fronted the
> control-plane with **either** Caddy + Let's Encrypt **or** Tailscale
> Funnel. Both paths were CANCELLED. M2 now ships exactly one public-
> exposure mechanism: a **localhost.run sidecar** (free anonymous SSH
> reverse tunnel, wildcard `*.lhr.life` cert managed upstream). This
> doc reflects that decision.

This is the operator-facing deploy + ops doc for v4.5. It covers:

1. The two supported topologies (laptop-host vs VPS-host).
2. The localhost.run "staging vs prod" decision matrix
   (free anonymous tier vs SSH-key stable URL).
3. Bringing up the `funnel` sidecar end-to-end.
4. Adjacent runbooks: cert renewal (`docs/Cert-Renewal.md`) and admin
   token rotation (`docs/Token-Rotation.md`).

---

## Contents

1. [What's new in v4.5 (M2)](#whats-new-in-v45-m2)
2. [Decision matrix — pick a topology and a tier](#decision-matrix--pick-a-topology-and-a-tier)
3. [staging vs prod warning callout](#staging-vs-prod-warning-callout)
4. [Topology A — laptop-host control-plane](#topology-a--laptop-host-control-plane)
5. [Topology B — VPS-host control-plane](#topology-b--vps-host-control-plane)
6. [Worker-side URL re-discovery](#worker-side-url-re-discovery)
7. [Adjacent runbooks](#adjacent-runbooks)
8. [Reference: env vars added in v4.5](#reference-env-vars-added-in-v45)

---

## What's new in v4.5 (M2)

| Surface | Change |
|---|---|
| `docker-compose.demo.yml` | Adds `funnel` service under `profiles: ["funnel"]`. Default `docker compose up` is byte-equivalent to v4.4 — `funnel` only starts with `--profile funnel`. |
| `docker-compose.control-plane.yml` | Adds the same profile-gated `funnel` service for VPS deployments. |
| `Dockerfile.funnel` | New small alpine image (`alpine + openssh-client + bash + curl + postgresql-client`, ≤ 32 MB). |
| Migration 008 | `workers.owner_email` column + partial index. |
| Migration 009 | `bootstrap_tokens` table — per-operator bootstrap minting. |
| Migration 010 | `funnel_url` singleton table — sidecar publishes the live `lhr.life` URL here. |
| `whilly admin bootstrap mint\|revoke\|list` | Per-operator bootstrap CLI; never reveals plaintext after mint. |
| `whilly admin worker revoke <id>` | Live worker eviction + RELEASE of in-flight tasks. |
| `make_admin_auth` factory | DB-backed admin bearer with `is_admin` scope check; legacy `WHILLY_WORKER_BOOTSTRAP_TOKEN` falls back to non-admin (one-minor compat window). |
| Worker env `WHILLY_FUNNEL_URL_SOURCE` | `static` (default, back-compat) / `postgres` / `file` — re-discovers the funnel URL on rotation. |

---

## Decision matrix — pick a topology and a tier

There are two independent choices: **where the control-plane runs**,
and **which localhost.run tier you use**. Pick one row from each.

### Topology — where does the control-plane live?

| Topology | When to use it | What it costs | Reference section |
|---|---|---|---|
| **A. Laptop-host** | Hands-on demo, working session with one or two colleagues, you don't own a VPS. | Your laptop must stay online; sidecar consumes one outbound TCP/22 to localhost.run. | [Topology A](#topology-a--laptop-host-control-plane) |
| **B. VPS-host** | Long-running cluster, several workers, control-plane survives laptop sleep. | A VPS (≥ 1 GB RAM, ≥ 2 GB disk, public outbound TCP/22). | [Topology B](#topology-b--vps-host-control-plane) |

Either way the `funnel` sidecar runs **on whichever host owns the
control-plane** (laptop or VPS) and publishes a rotating
`https://<random>.lhr.life` URL that workers anywhere on the public
internet can reach.

### localhost.run tier — staging vs prod

The localhost.run service offers two operationally distinct tiers.
Treat them like Let's Encrypt's staging vs prod environments: one is
free + ephemeral + safe-to-poke-at, the other is for real users.

| Tier | URL shape | Lifetime | Auth required | Use when |
|---|---|---|---|---|
| **Free anonymous (staging)** | `https://<random>.lhr.life` | Rotates "after a few hours" of session lifetime | None — `nokey@localhost.run` over SSH | Demos, proofs of concept, M2 sign-off, anything you can re-share a fresh URL for. **Default in v4.5.** |
| **SSH-key stable (prod)** | `https://<your-name>.lhr.life` (or your own custom domain on the paid tier) | Stable across reconnects | Free localhost.run account + dedicated SSH key registered with them | A cluster you want to put into a colleague's `~/.bashrc`. **Deferred to M3 in this mission**, but supported by the sidecar today if you wire your own key in. |

> **staging vs prod warning.** The free anonymous tier rotates the
> public URL on a cadence localhost.run does not pin (their FAQ says
> "after a few hours"). **Do NOT bake the live `lhr.life` URL into
> any persistent artifact** — `.bashrc`, GitHub Actions secret,
> wiki page, kubeconfig, monitoring config, etc. Workers must read
> the URL through `WHILLY_FUNNEL_URL_SOURCE=postgres` or `=file` so
> rotations are absorbed transparently. If you need a stable URL,
> upgrade to the SSH-key path (deferred to M3) — do not pretend the
> anonymous tier is stable.

A second consequence of the rotation cadence: validators, dashboards,
and monitoring systems reading `/health` or `/api/v1/tasks` over the
public URL must re-fetch the URL from postgres / the shared file
between attempts; never cache it across runs.

---

## staging vs prod warning callout

> ⚠️ **Read this before pasting any `*.lhr.life` URL anywhere.**
>
> The free-tier (staging) localhost.run URL is **ephemeral by design**.
> It rotates on a cadence the upstream documents as "after a few
> hours" of session connection. Treat it the same way you treat
> a Let's Encrypt **staging** certificate: useful for end-to-end
> smoke tests, never trust-on-first-use as a permanent endpoint.
>
> The **prod** path (SSH-key stable URL via a free localhost.run
> account, deferred to **M3** in this mission) is what you want for
> anything that survives a laptop sleep. M2 ships the sidecar; M3
> adds the SSH-key wiring + `funnel_url`-stability promise.
>
> **Workers absorb rotations** when `WHILLY_FUNNEL_URL_SOURCE`
> is `postgres` (poll the `funnel_url` table every 30 s) or
> `file` (poll `/funnel/url.txt` every 5 s). Default is `static`
> for back-compat; if you set `static` you must restart the worker
> by hand on every rotation.

---

## Topology A — laptop-host control-plane

End-to-end recipe: control-plane + sidecar on a macbook, workers on
a VPS / second laptop / phone-tethered colleague.

### A.1. Bring up the control-plane + sidecar

```bash
cd /opt/develop/whilly-orchestrator
git checkout v4.5.0     # or `main` for unreleased

export WHILLY_WORKER_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"

# Profile-gated; default `up` is unchanged from v4.4.
docker compose -f docker-compose.demo.yml \
    --profile funnel \
    up -d

# Verify postgres + control-plane are healthy:
docker compose -f docker-compose.demo.yml ps

# Verify the sidecar parsed an `lhr.life` URL within ~10 s:
docker compose -f docker-compose.demo.yml logs funnel | grep -E 'https://[a-z0-9-]+\.lhr\.life'
```

### A.2. Mint a per-operator bootstrap token

Replace the legacy shared `WHILLY_WORKER_BOOTSTRAP_TOKEN` env var with
per-operator rows from the `bootstrap_tokens` table.

```bash
# WHILLY_DATABASE_URL must point at the control-plane Postgres
# (same DSN your control-plane is configured with — see ./scripts/db-up.sh
# and docker-compose.demo.yml for the demo credentials shape).
whilly admin bootstrap mint --owner alice@example.com --expires-in 30d
# ⇒ token: <plaintext>     ← capture this once, never reprintable
#   owner: alice@example.com
#   token_hash: <sha256-prefix>
#   is_admin: false
#   expires_at: 2026-06-02T...
```

The plaintext is shown **once**. Re-running `whilly admin bootstrap
list` only ever shows the truncated `token_hash` — capture the
plaintext at mint time and hand it to alice through whatever secure
channel you'd normally use for shared secrets.

### A.3. Discover the live `lhr.life` URL

```bash
URL=$(psql "$WHILLY_DATABASE_URL" -t -A -c \
    "SELECT url FROM funnel_url ORDER BY updated_at DESC LIMIT 1")
echo "$URL"     # → https://abc123def456.lhr.life
```

### A.4. Worker (any host) joins via the lhr.life URL

```bash
# Either pip install (Python path) or use the Docker worker image.
pip install 'whilly-orchestrator[worker]'

whilly worker connect "$URL" \
    --bootstrap-token "$ALICE_BOOTSTRAP_TOKEN" \
    --plan demo \
    --hostname "$(hostname)"

# No --insecure needed — localhost.run terminates a real
# Let's Encrypt prod cert at the edge. The worker's URL-scheme
# guard accepts the URL because it's HTTPS.
```

> The `--insecure` loopback-bypass is **not needed** here. That flag
> exists for plain-HTTP-to-non-loopback edge cases (LAN demos before
> M2 lands). Once you're on `https://*.lhr.life`, drop it.

For URL-rotation tolerance, the worker host should run with
`WHILLY_FUNNEL_URL_SOURCE=postgres` (preferred) or `=file`. See
[Worker-side URL re-discovery](#worker-side-url-re-discovery) below.

---

## Topology B — VPS-host control-plane

The same flow, but the control-plane + sidecar run on a public VPS.
Useful when you want the cluster to survive your laptop sleeping.

```bash
export VPS_HOST=vps.example.com
ssh root@$VPS_HOST
cd /root/whilly
git checkout v4.5.0

export WHILLY_WORKER_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"

# Postgres + control-plane bound to loopback (default), sidecar
# bridges them to localhost.run.
docker compose -f docker-compose.control-plane.yml \
    --profile funnel \
    up -d

docker compose -f docker-compose.control-plane.yml logs funnel \
    | grep -E 'https://[a-z0-9-]+\.lhr\.life'
```

Workers on laptops then read the URL the same way as Topology A.4.

> **VPS resource note.** The 964 MB-RAM VPS profile keeps the
> stack under 600 MB total — postgres ~256 MB, control-plane
> ~256 MB, funnel sidecar ~32 MB. Validate after bring-up:
> `docker stats --no-stream`.

---

## Worker-side URL re-discovery

The localhost.run free-tier URL rotates "after a few hours". Workers
absorb the rotation transparently when one of the two non-static
discovery modes is enabled:

| `WHILLY_FUNNEL_URL_SOURCE` | Default | Behaviour |
|---|---|---|
| `static` | yes — back-compat | Use `WHILLY_CONTROL_URL` verbatim. Operator must restart the worker on rotation. |
| `postgres` | no | Poll `funnel_url` table every `WHILLY_FUNNEL_URL_POLL_SECONDS` (default 30 s). On change: release in-flight task, re-register against the new URL with the same `worker_id`, resume long-poll. |
| `file` | no | Same, but reads `WHILLY_FUNNEL_URL_FILE` (default `/funnel/url.txt`). 5 s default cadence. |

Re-registration is idempotent server-side: the same `worker_id` row
gets its `last_heartbeat` and `host_url` columns updated in place.
The bearer in the OS keychain is reused if still valid; otherwise the
worker re-runs the bootstrap flow with its stored bootstrap token.

> The publishing side (sidecar → `funnel_url` table +
> `/funnel/url.txt`) ships with `m2-localhostrun-funnel-sidecar`.
> The worker-side polling loop that consumes the URL ships in
> `m2-worker-url-refresh-on-rotation`: opt in by setting
> `WHILLY_FUNNEL_URL_SOURCE=postgres` or `=file` on the worker.
> See [`docs/Distributed-Setup.md` § "Two-host via
> localhost.run"](Distributed-Setup.md#two-host-via-localhostrun)
> for the static-URL fallback recipe operators can also use.

---

## Adjacent runbooks

Two operational concerns are split into their own runbooks so you can
find them by name:

* [`docs/Cert-Renewal.md`](Cert-Renewal.md) — TLS-cert lifecycle for
  the localhost.run wildcard `*.lhr.life` cert (what to check when
  workers start failing handshake), and a forward-looking section on
  the BYO-cert path you'll want when you outgrow localhost.run.
* [`docs/Token-Rotation.md`](Token-Rotation.md) — separate playbooks
  for **per-user bootstrap leaks** (one operator's plaintext got
  pasted in Slack — limited blast radius) and **admin / shared
  legacy-token leaks** (the cluster-wide secret got out — full
  rotation required).

Both are linked from the README quick-start and from the in-tree
`CHANGELOG.md` v4.5 entry so operators can grep them by name.

---

## Reference: env vars added in v4.5

| Variable | Default | Purpose |
|---|---|---|
| `WHILLY_FUNNEL_URL_SOURCE` | `static` | Worker URL discovery mode: `static` / `postgres` / `file`. See [Worker-side URL re-discovery](#worker-side-url-re-discovery). |
| `WHILLY_FUNNEL_URL_FILE` | `/funnel/url.txt` | Path to the shared-volume file in `file` mode. |
| `WHILLY_FUNNEL_URL_POLL_SECONDS` | `30` (postgres) / `5` (file) | Poll cadence on the chosen source. |
| `FUNNEL_LOCAL_HOST` | `control-plane` | Sidecar — host the SSH reverse tunnel forwards to. |
| `FUNNEL_LOCAL_PORT` | `8000` | Sidecar — port the SSH reverse tunnel forwards to. |
| `FUNNEL_SERVER_ALIVE_INTERVAL` | `60` | Sidecar — SSH `ServerAliveInterval` seconds. |
| `FUNNEL_RETRY_BACKOFF_SECONDS` | `5` | Sidecar — sleep between SSH reconnect attempts. |
| `FUNNEL_URL_FILE` | `/funnel/url.txt` | Sidecar — file the parsed URL is atomically rewritten to. |

`WHILLY_WORKER_BOOTSTRAP_TOKEN` (the legacy shared secret) is **still
honoured** in v4.5 as a one-minor-version backwards-compat fallback
on `POST /workers/register`. Setting it produces a one-shot
deprecation warning at startup; the per-operator bootstrap flow
(`whilly admin bootstrap mint`) is the supported path.
