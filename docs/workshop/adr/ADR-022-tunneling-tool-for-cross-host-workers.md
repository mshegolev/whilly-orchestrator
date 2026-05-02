# ADR-022 — Tunneling tool for cross-host workers and Claude proxy

- **Status:** accepted
- **Date:** 2026-05-02
- **Deciders:** project author
- **Domain:** networking / distributed deployment
- **Relates to:** ADR-014 (workflow sink protocol — multi-container packaging), `docs/PRD-v41-claude-proxy.md` (SC-1 SSH-tunnel scenario), `docs/distributed-audit/readiness-validation.md` (cross-host worker connectivity), TASK-111 (`scripts/whilly-share.sh`, not yet landed)

## Context

Three whilly scenarios need to expose a service running on the **primary machine** to a process running on a **secondary machine** without requiring the secondary to have VPN access or SSH credentials on the primary:

1. **Workshop demo (hour 5).** A participant on their corporate laptop runs `whilly-worker` and connects to the trainer's control plane (Postgres on `:5432`, Claude proxy on `:11112`) over the public internet. See `docs/Continuing-On-Another-Machine.md` table row "Cross-host tunnel via `scripts/whilly-share.sh`".
2. **Distributed worker pool.** Multiple workers on cloud VMs reach back into a single primary that holds the canonical plan + Postgres. SSH tunneling each worker by hand (the current 4.2 SSH-tunnel default in `docs/Whilly-Workstation-Bootstrap.md`) doesn't scale to N>3.
3. **Claude proxy fallback.** When `WHILLY_CLAUDE_PROXY_URL` points to a remote tunnel endpoint instead of `127.0.0.1:11112` (PRD-v41 SC-1, second variant), the same tunnel infrastructure is reused.

The market in 2026 has consolidated around five usable options. This is the comparison that drove the decision (rates current as of 2026-05).

### Pricing landscape — localhost.run reference plans

| Plan                  | What it gives                                                    |
| --------------------- | ---------------------------------------------------------------- |
| Free                  | Random `*.lhr.life` subdomain, 1 tunnel, ~6 h disconnect         |
| Plus ($3.50/mo)       | Custom subdomain, multiple tunnels, longer sessions              |
| Pro / Team ($10+/mo)  | Own domain, API, analytics, SLA                                  |

### Tool comparison matrix

| Service           | Client                                          | Free tier                              | Corp-proxy friendly      |
| ----------------- | ----------------------------------------------- | -------------------------------------- | ------------------------ |
| localhost.run     | `ssh` (preinstalled everywhere)                 | ✅ random subdomain                     | ✅ goes out on :22        |
| serveo.net        | `ssh`                                           | ✅ random / custom name                 | ✅ :22                    |
| ngrok             | own binary + auth-token                         | ⚠️ random URL, session/bandwidth limits | ⚠️ HTTPS-only on :443     |
| cloudflared       | `cloudflared` binary + Cloudflare account       | ✅ `trycloudflare.com`                  | ⚠️ HTTPS-only             |
| bore              | own Rust binary                                 | ✅ self-hosted                          | depends on chosen port   |
| frp               | own client + own server                         | ✅ DIY                                  | DIY                      |
| tailscale funnel  | Tailscale agent + account                       | ⚠️ Pro tier only                        | over WireGuard           |

The corporate-proxy column is doing most of the work here: workshop participants behind employer firewalls regularly cannot reach `:443` for tunnel handshakes when SNI inspection is on, but `:22` outbound is almost always permitted because employees still need git over SSH.

## Decision

**Adopt a tiered default with `localhost.run` as the workshop/demo default and `cloudflared` as the power-user / persistent option. Self-hosted (`bore` / `frp`) is documented but explicitly out of scope for v4.**

Concretely:

- `scripts/whilly-share.sh` (TASK-111) will detect available tunnel binaries in this priority order: `ssh` → `cloudflared` → `ngrok`. First match wins; the user can override with `WHILLY_TUNNEL_TOOL={lhr,cloudflared,ngrok}`.
- The default invocation is `ssh -R 80:127.0.0.1:${PORT} nokey@localhost.run` (no account, no signup, runs from any laptop with `ssh`).
- The script extracts the assigned `*.lhr.life` URL from the SSH banner and writes it to `.whilly_share/url` for downstream consumers (worker bootstrap, doc snippets, Telegram pings).
- Documentation in `docs/Continuing-On-Another-Machine.md` is updated to reference the wrapper script instead of hand-rolled SSH commands.

## Considered alternatives

### A. Default to `cloudflared` + `trycloudflare.com`

- ✅ Stable random hostname, no 6 h disconnect.
- ✅ HTTP/2 and WebSocket support out of the box.
- ❌ Requires an extra binary install on every workshop laptop — kills the "30-second start" pitch.
- ❌ HTTPS-only (`:443`) — fails behind SNI-filtering corporate proxies that workshop participants frequently sit behind.
- ❌ Cloudflare account creation friction for the persistent variant.

### B. Default to `ngrok`

- ✅ Best-in-class observability dashboard.
- ❌ Free tier rotates URLs every restart; workshop participants get confused when the URL in their slide deck no longer works.
- ❌ Corporate-firewall coverage is identical to cloudflared (`:443`-only) without the upside of being already-deployed.
- ❌ Requires an auth-token in env — one more secret to leak in shell history during a live demo.

### C. Self-host `bore` or `frp` on the trainer's VPS

- ✅ Zero rate limits, custom hostname, no third-party dependency.
- ❌ Trainer becomes responsible for an always-on TCP relay — operational burden disproportionate to "show it once a quarter".
- ❌ Workshop laptops still need to install the client binary.
- Rejected for v4; revisit if/when whilly-cloud lands a managed control plane.

### D. Tailscale Funnel

- ✅ Encrypted by default, identity-bound (no anonymous tunnels).
- ❌ Funnel is a paid tier only.
- ❌ Workshop participants would all need Tailscale accounts and an admin invite — kills the "open the URL and you're in" demo flow.

### E. localhost.run + cloudflared tiered default (chosen)

- ✅ `ssh` is already on every laptop that runs whilly (the `whilly` CLI itself shells to git over SSH).
- ✅ No signup required for the demo path — participant copies one command, gets a URL, done.
- ✅ Outbound `:22` slips past corporate filters that block ngrok/cloudflared.
- ✅ Persistent / longer-running case has a documented upgrade path to `cloudflared` without rewriting the wrapper.
- ❌ Free tier disconnects after ~6 h — accepted for workshop sessions (a workshop runs 4 h); production users upgrade or switch to cloudflared.
- ❌ Public anonymous endpoint — mitigated by the auth-header guidance already in `docs/distributed-audit/readiness-validation.md` ("Anonymous tunnels" row).

## Decision details

### Wrapper detection logic (`scripts/whilly-share.sh`)

```bash
# Priority order, first match wins
WHILLY_TUNNEL_TOOL=${WHILLY_TUNNEL_TOOL:-auto}

case "$WHILLY_TUNNEL_TOOL" in
  lhr)         exec ssh -R 80:127.0.0.1:"$PORT" nokey@localhost.run ;;
  cloudflared) exec cloudflared tunnel --url "http://127.0.0.1:$PORT" ;;
  ngrok)       exec ngrok http "$PORT" --log stdout ;;
  auto)        # try ssh → cloudflared → ngrok
               command -v ssh         >/dev/null && exec ssh -R 80:127.0.0.1:"$PORT" nokey@localhost.run
               command -v cloudflared >/dev/null && exec cloudflared tunnel --url "http://127.0.0.1:$PORT"
               command -v ngrok       >/dev/null && exec ngrok http "$PORT" --log stdout
               echo "no tunnel tool available; install openssh-client or cloudflared" >&2
               exit 127 ;;
esac
```

### Security guidance (carried over from distributed-audit)

The wrapper itself does **not** add authentication — the exposed port must already require a token (control-plane API), TLS client cert (Postgres via `sslmode=verify-full`), or basic-auth (Claude proxy). Operators are expected to read `docs/distributed-audit/readiness-validation.md` §"Anonymous tunnels" before using the wrapper in any non-workshop context.

### What the script will NOT do

- **Auto-restart on disconnect.** The 6 h cap is a feature for workshops (forces session boundary). Long-lived deployments use `cloudflared` + `systemd` per `docs/Whilly-Workstation-Bootstrap.md` "SSH tunnel as systemd unit" pattern, generalised to cloudflared.
- **Multiplex multiple ports.** One `whilly-share.sh` invocation = one port. A worker that needs Postgres + Claude proxy starts two side-by-side. (Multiplexing is the ngrok upsell and not worth the dependency.)

## Consequences

### Positive

- Workshop participants can join a remote control plane with `whilly-worker --connect $(curl https://trainer.example/url)` in two commands, no account, no install.
- Corporate-firewall users — historically the most painful onboarding path — get a working default.
- The `WHILLY_TUNNEL_TOOL` env knob keeps the door open for cloudflared/ngrok power users without forking the script.
- ADR-014 multi-container packaging gains a documented "how do containers reach the trainer's primary" answer.

### Negative

- localhost.run is a single point of dependency for the demo path. Mitigation: `cloudflared` fallback in the same script costs ~10 lines and ships in v4.
- Anonymous endpoints are inherently scary for security-conscious teams; the wrapper documentation must lead with the auth-required disclaimer, not bury it.
- The 6 h cap will eventually surprise someone who tries to run a workshop demo as a permanent staging environment — the README `--connect` example links to this ADR for the "why not 24/7" answer.

### Neutral

- The `--connect` flag itself (TASK-111) is unaffected by this decision; it consumes a URL, agnostic of which tool produced it.
- Existing SSH-tunnel pattern in `docs/Whilly-Claude-Proxy-Guide.md` (port-forward to a known peer) is **not deprecated** — it remains the right choice when both peers are SSH-reachable. The wrapper covers the case where they aren't.

## References

- TASK-111 — `scripts/whilly-share.sh` (not yet landed; this ADR is the design lock for the implementation).
- `docs/Continuing-On-Another-Machine.md` — table row "Cross-host tunnel via `scripts/whilly-share.sh`".
- `docs/PRD-v41-claude-proxy.md` SC-1 — original SSH-tunnel scenario this generalises.
- `docs/distributed-audit/readiness-validation.md` — "Anonymous tunnels" security row carried into the wrapper docs.
- `docs/Whilly-Workstation-Bootstrap.md` — current 4.2 SSH-tunnel default and systemd unit pattern (template for the persistent cloudflared variant).
- ADR-014 — multi-container workflow sink, defines the consumers of the exposed ports.
- localhost.run pricing reference: <https://localhost.run/pricing/> (Free / Plus $3.50 / Pro+ $10+ as of 2026-05).
