# Cert Renewal Runbook (v4.5)

> **Scope.** What to check, what to fix, and where to look when TLS
> handshakes start failing between Whilly workers and the public
> control-plane URL. Pairs with [`docs/Deploy-M2.md`](Deploy-M2.md)
> (M2 deploy doc) and [`docs/Token-Rotation.md`](Token-Rotation.md)
> (admin / per-user token rotation runbook).
>
> **Pivot note (2026-05-02).** M2 cancelled both the Caddy + ACME
> path and the Tailscale Funnel path. The current public-exposure
> mechanism is the **localhost.run sidecar** — TLS is terminated at
> the localhost.run edge with a public Let's Encrypt cert for the
> wildcard `*.lhr.life` zone. Whilly does NOT run an ACME client and
> does NOT manage the cert directly. This runbook reflects that —
> it documents what to verify, what files / paths matter, and how to
> migrate to a self-managed cert when you outgrow localhost.run.

---

## Contents

1. [Where the cert actually lives](#where-the-cert-actually-lives)
2. [Symptoms — when do you reach for this runbook?](#symptoms--when-do-you-reach-for-this-runbook)
3. [Diagnose: is it the cert?](#diagnose-is-it-the-cert)
4. [Force-renew the cert](#force-renew-the-cert)
5. [Migrating off localhost.run to a self-managed cert](#migrating-off-localhostrun-to-a-self-managed-cert)
6. [Reference: file paths the funnel sidecar uses](#reference-file-paths-the-funnel-sidecar-uses)
7. [staging vs prod cert reminder](#staging-vs-prod-cert-reminder)

---

## Where the cert actually lives

| Layer | Owner | What is stored | Where |
|---|---|---|---|
| **TLS terminator** | localhost.run (upstream) | Let's Encrypt prod wildcard cert for `*.lhr.life` | localhost.run edge — **not on your host**. You cannot `cat` it. |
| **Sidecar SSH client** | The `funnel` service in `docker-compose.demo.yml` / `docker-compose.control-plane.yml` | SSH known_hosts entries for `localhost.run` | Container-local: `~root/.ssh/known_hosts` (alpine image). Recreated on container restart with `StrictHostKeyChecking=accept-new`. |
| **Funnel URL state** | The control-plane Postgres | Latest `lhr.life` URL | `funnel_url` table (singleton row, `id=1`). |
| **Funnel URL fallback** | The `funnel_url_volume` named volume | Latest `lhr.life` URL | `/funnel/url.txt` inside the sidecar; mounted from the host volume. |
| **Worker trust store** | The OS / Python runtime | Standard `certifi` CA bundle | Worker container: typical `python:3.12-slim` cert path; macOS workers: System Roots; Linux workers: `/etc/ssl/certs/ca-certificates.crt`. **No custom CA bundle is required** because localhost.run uses Let's Encrypt prod, which is in the system trust store everywhere. |

> **Key consequence.** Because the cert lives at the localhost.run
> edge, "renewing the cert" usually means **letting the sidecar
> reconnect** so the SSH transport picks up whatever the upstream
> currently serves. There is no `certbot renew` step on Whilly's
> side.

---

## Symptoms — when do you reach for this runbook?

| Symptom | Likely cause | Fix path |
|---|---|---|
| Worker stderr: `httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired` | Worker CA bundle is stale (e.g. `certifi` package > 1 year old in a frozen image) | `pip install -U certifi` in the worker image, or rebuild from a fresh Python base image. |
| Worker stderr: `httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] hostname '<random>.lhr.life' doesn't match` | Worker is using a stale URL (rotated upstream) | Restart worker; ensure `WHILLY_FUNNEL_URL_SOURCE=postgres` or `=file`. See [`docs/Deploy-M2.md` § Worker-side URL re-discovery](Deploy-M2.md#worker-side-url-re-discovery). |
| Sidecar log: `kex_exchange_identification: Connection closed by remote host` | localhost.run rejected the SSH session (rate-limit, transient outage, blocked source IP) | Wait for the sidecar's backoff loop to retry; check `https://status.localhost.run` if it persists. |
| Sidecar log: `Permission denied (publickey)` after switching to a stable URL | You've started passing an SSH key (M3 prod path) but the key isn't registered with localhost.run | Re-register the key in your localhost.run account; or fall back to anonymous tier by clearing the key. |
| `psql -c 'SELECT url FROM funnel_url ...'` returns NULL or stale value | Sidecar exited / never published | `docker compose logs funnel` and force-renew (next section). |

---

## Diagnose: is it the cert?

Run this checklist top-to-bottom; stop at the first failure.

### 1. Is the sidecar even running?

```bash
docker compose -f docker-compose.demo.yml ps funnel
# Look for STATE=running, EXIT=0
```

If `Exit (1)`: the SSH session died; jump to [Force-renew the cert](#force-renew-the-cert).

### 2. What URL did the sidecar last publish?

```bash
psql "$WHILLY_DATABASE_URL" -t -A -c \
    "SELECT url, updated_at FROM funnel_url ORDER BY updated_at DESC LIMIT 1"
# OR (no postgres reachability):
docker compose -f docker-compose.demo.yml exec funnel cat /funnel/url.txt
```

If `updated_at` is more than a few hours old, the sidecar's session
likely died and reconnected to a new URL but the worker is still
holding the old one — see Symptom 2 above.

### 3. Does the URL still terminate TLS correctly?

```bash
URL=$(psql "$WHILLY_DATABASE_URL" -t -A -c \
    "SELECT url FROM funnel_url ORDER BY updated_at DESC LIMIT 1")
HOST=${URL#https://}; HOST=${HOST%/*}
echo "$HOST"

# Plain handshake — no app-level traffic.
echo | openssl s_client -connect "$HOST":443 -servername "$HOST" -showcerts 2>/dev/null \
    | openssl x509 -noout -subject -issuer -dates
```

You should see `subject=CN = *.lhr.life`, `issuer=…Let's Encrypt…`,
and `notAfter` in the future.

If `notAfter` is in the past, the upstream cert is expired — that's
on localhost.run. Check `https://status.localhost.run`. There is
nothing to fix on Whilly's side; reaching for the
[migration path](#migrating-off-localhostrun-to-a-self-managed-cert)
becomes the right move.

### 4. Does the worker's CA bundle trust the cert?

```bash
docker compose -f docker-compose.worker.yml exec whilly-worker \
    python -c "import certifi, ssl; print(certifi.where()); print(ssl.OPENSSL_VERSION)"
docker compose -f docker-compose.worker.yml exec whilly-worker \
    python -c "import urllib.request as u; u.urlopen('$URL/health').read()[:80]"
```

If the second command raises `CERTIFICATE_VERIFY_FAILED`, the worker
image's CA bundle is stale; rebuild from a fresh base image.

---

## Force-renew the cert

There is no in-place `certbot renew` for localhost.run — the closest
operator action is **forcing the sidecar to reconnect**, which gives
you a fresh SSH session against the upstream's current certificate.

```bash
# Topology A (laptop / demo file):
docker compose -f docker-compose.demo.yml restart funnel

# Topology B (VPS / control-plane file):
docker compose -f docker-compose.control-plane.yml restart funnel

# Watch for the new URL line:
docker compose -f docker-compose.demo.yml logs -f --tail=50 funnel
```

Within ~10 seconds you should see the sidecar print a new
`https://<random>.lhr.life` URL and write it to both the
`funnel_url` table and `/funnel/url.txt`.

Then verify worker pickup:

```bash
psql "$WHILLY_DATABASE_URL" -t -A -c \
    "SELECT url, updated_at FROM funnel_url ORDER BY updated_at DESC LIMIT 1"

# Workers running with WHILLY_FUNNEL_URL_SOURCE=postgres should
# pick up the new URL within WHILLY_FUNNEL_URL_POLL_SECONDS (30 s
# default). Confirm via:
docker compose -f docker-compose.worker.yml logs --tail=20 whilly-worker \
    | grep -i 'reconnect\|register\|funnel'
```

If the URL did not change after a restart, the upstream may have
issued the same one (their session cache); that is fine — the cert
is still freshly negotiated on the new SSH session.

---

## Migrating off localhost.run to a self-managed cert

When you outgrow the free anonymous tier — typically because you want
a stable URL or your own domain — the migration path is:

### Option 1 — localhost.run paid / SSH-key tier (M3 in this mission)

Stable subdomain, no rotation, still TLS-terminated upstream by their
Let's Encrypt wildcard cert. **Closest to a no-op migration**, but
still needs a free localhost.run account + an SSH key registered with
them. The sidecar gains an env var (`FUNNEL_SSH_KEY_PATH`) in the M3
release; until then you can mount `~/.ssh/id_ed25519` into the
sidecar by hand and replace `nokey@localhost.run` with
`<your-handle>@localhost.run`.

### Option 2 — Bring your own domain + self-managed cert

The deferred (post-M3) path. Two sub-options:

* **Caddy reverse-proxy** (was the original M2 plan). One-line
  `your.domain.com { reverse_proxy control-plane:8000 }`. Caddy
  manages a Let's Encrypt prod cert via HTTP-01 or DNS-01 ACME, with
  `caddy_data` and `caddy_config` named volumes for state.
* **Cloudflared / Tailscale Funnel / ngrok / etc.** Same shape —
  external tunnel terminator owns the cert; Whilly stays on
  loopback.

When you go this route, the **cert renewal runbook moves to that
tunnel's docs**, not Whilly's. Whilly's job remains: serve plain HTTP
on loopback, let the terminator do TLS.

### Option 3 — Run TLS on the control-plane itself

Possible (uvicorn supports `--ssl-certfile` / `--ssl-keyfile`) but
not recommended in this mission. Operationally identical to a Caddy
sidecar, but with no obvious win and a worse failure mode (uvicorn
restart on cert reload). If you really want this, the cert-file env
vars exist in the underlying uvicorn launcher and the cert renewal
runbook becomes "rerun your `certbot` cron, then `docker compose
restart control-plane`".

---

## Reference: file paths the funnel sidecar uses

| Path | Purpose | Persistence |
|---|---|---|
| `/funnel/url.txt` | Latest published URL — fallback for workers without postgres reachability. Atomic-rename rewrite on every change. | Backed by named volume `funnel_url_volume` (mounted from host). Survives container restarts. |
| `~root/.ssh/known_hosts` | Recorded host key for `localhost.run`. First connect uses `accept-new`. | Container-local; reset on `docker compose down -v`. |
| `~root/.ssh/id_ed25519` (optional) | SSH key for the M3 stable-URL path. Default sidecar does NOT mount one — the anonymous tier is keyless. | When mounted, sourced from your host's `~/.ssh/id_ed25519` (chmod 600 inside the container). |
| `funnel_url` table (Postgres) | Singleton row (`id=1`) of the latest published URL. Sidecar writes via `INSERT ... ON CONFLICT (id) DO UPDATE`. | Survives compose restarts (Postgres-backed). |

There are **no** Whilly-managed `*.crt` / `*.key` files in M2. The
absence is deliberate: localhost.run owns the cert.

---

## staging vs prod cert reminder

The localhost.run free anonymous tier serves the same Let's Encrypt
**prod** wildcard cert as their paid tier — it is **not** a
Let's Encrypt staging cert. There is no untrusted-CA pitfall to
work around. What's "staging-like" about the free tier is the
**URL rotation cadence**, not the cert: the URL changes "after a
few hours", the cert (`*.lhr.life`) does not. Workers do not need
a custom CA bundle on either tier.

If you migrate to **Option 2** (bring your own domain) and stand up
your own ACME flow, that's where the Let's Encrypt staging vs prod
distinction starts to matter — keep your reverse-proxy pinned to
**staging** until your DNS / firewall / SAN list is settled, and
flip to **prod** only when you want browsers to trust the cert
without a security warning. That's a runbook for whichever
terminator you pick (Caddy, nginx, Traefik, …), not Whilly.

---

## See also

* [`docs/Deploy-M2.md`](Deploy-M2.md) — full M2 deploy walk-through.
* [`docs/Token-Rotation.md`](Token-Rotation.md) — token rotation
  runbook (per-user leak vs admin leak).
* [`docs/Distributed-Setup.md`](Distributed-Setup.md) — M1 multi-host
  deployment (the foundation M2 builds on).
* [`CHANGELOG.md`](../CHANGELOG.md) v4.5 entry for the full list of
  M2 changes.
