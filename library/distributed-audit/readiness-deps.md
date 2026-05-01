# M1+M2+M3 — Mission Dependency Readiness

> Generated 2026-05-01 02:23 (local) / 2026-04-30 23:23 UTC; verified against VPS 213.159.6.155:23422 and local macbook (darwin 23.4.0).

## Status: READY (with caveats)

All hard dependencies resolved on both hosts; **VPS has only ~221 MB RAM available and 6.2 GB disk free**, and is running an existing `openclaw-gateway` container plus a live `tailscaled.service` — these need to be designed around but are not hard blockers. Inbound 80/443 from the macbook gets `Connection reset by peer` while no service is listening; expected to clear once Caddy binds those ports. Tailscale verification was deferred per user instructions, but tailscaled is already installed/running on the VPS.

## A. Local macbook (orchestrator host)

| Item | Result | Detail |
|---|---|---|
| Python 3.12+ | ✓ | `Python 3.12.1` at `/Users/m.v.shchegolev/.pyenv/shims/python3` |
| Project venv (`.venv/`) | ✓ | `Python 3.12.1`, editable install of `whilly-orchestrator 4.0.0` (pyproject says 4.3.1; editable not re-synced — see Constraints) |
| Project deps in venv | ✓ | `alembic 1.18.4`, `asyncpg 0.31.0`, `fastapi 0.136.1`, `httpx 0.28.1`, `keyring 25.7.0`, `pydantic 2.13.3`, `testcontainers 4.14.2`. **Jinja2 3.1.6** also already present in the system pip (not necessarily venv). No `prometheus*` / `sse-starlette` yet — will be added by mission. |
| Docker | ✓ | client `24.0.7` (colima context), server `27.4.0`, daemon up, 1 stopped container, 19 images, `overlay2` storage driver |
| pip resolve: `prometheus-fastapi-instrumentator>=7.1.0` | ✓ | resolved to **7.1.0** in throwaway venv `/tmp/whilly-readiness-1777580573/venv` |
| pip resolve: `sse-starlette>=2.0` | ✓ | resolved to **3.4.1** |
| pip resolve: `jinja2>=3.1` | ✓ | resolved to **3.1.6** |
| pip resolve: `prometheus-client>=0.20` | ✓ | resolved to **0.25.0** |
| Combined import smoke test | ✓ | `import prometheus_fastapi_instrumentator, sse_starlette, jinja2, prometheus_client → 'IMPORT OK'` |
| Local `caddy` binary | ✗ (acceptable) | not installed; not in brew. Fallback: `docker run --rm caddy:2-alpine caddy validate` |
| `gh` | ✓ | `2.83.2 (2025-12-10)` |
| `git` | ✓ | `2.39.3 (Apple Git-146)` |
| `rg` | ✓ | `13.0.0` |
| `jq` | ✓ | `1.8.1` |
| `curl` | ✓ | `8.4.0 (LibreSSL/3.3.6)` |
| `ssh` | ✓ | `OpenSSH_9.7p1, OpenSSL 3.6.2 7 Apr 2026` |
| Required ports free (8000, 8001, 5432, 3100, 80, 443) | ✓ | `lsof … LISTEN` returns "none in use" for all six |
| `agent-browser` skill | △ | Listed in Factory `<available_skills>` (personal); no on-disk path under `~/.factory/skills/` (skill discovery is via Factory runtime, not a filesystem). Invoke via `Skill` tool. |
| `tuistory` skill | △ | Same as above — present in `<available_skills>` (personal). |

Repo state: branch `main`, HEAD `1093009afe8dfddf48ab15ef66c726d6a1a284be` (matches `1093009`), `git status --porcelain` shows only `?? .planning/distributed-audit/` (expected per task).

## B. VPS (213.159.6.155:23422 as root)

| Item | Result | Detail |
|---|---|---|
| SSH | ✓ | `BatchMode=yes` succeeds (key-based auth via local agent). `uname`: `Linux srv.tr-anet-01.com 5.10.0-37-amd64 SMP Debian 5.10.247-1 (2025-12-11) x86_64`; uptime 80 days, load 0.00. |
| OS / arch | ✓ | `Debian GNU/Linux 11 (bullseye)` / `x86_64` |
| Docker | ✓ | `Docker version 29.2.1, build a5c7197`; `Docker Compose version v5.0.2` (CLI plugin v5.0.2 — note: this is unusually labelled — `v2.x` is current upstream; v5.0.2 appears to be a Docker Inc. plugin re-numbering. Compose subcommand works.) |
| Docker daemon | ✓ | `containerd` + `docker.service` running; 1 container running (`openclaw-gateway`); `containerd` v1.x via Docker Engine Community |
| Free ports | ✓ for **80, 443, 8000, 5432**; expected listeners only — see detail | Listeners: `:23422 sshd`, `:18789 openclaw-gateway`, `127.0.0.1:18792 openclaw-gateway`. Nothing on 80/443/8000/5432/3100. |
| Public IP confirmation | ✓ | `curl https://ifconfig.me` → `213.159.6.155` (matches expected; not behind NAT) |
| sslip.io DNS resolves from macbook | ✓ | `dig +short 213-159-6-155.sslip.io` → `213.159.6.155` |
| sslip.io DNS resolves from VPS | △ | `dig` and `nslookup` not installed on VPS (`bash: dig: command not found`). However outbound DNS clearly works (PyPI / GitHub HTTPS calls succeed below). Acceptable; `getent hosts 213-159-6-155.sslip.io` would be the tool to use during M2. |
| Inbound 80 reachable from macbook | △ | TCP handshake completes then `Connection reset by peer` (curl exit 56). No process is bound to :80 yet, so this is the kernel/firewall behavior with no listener. Expected to "just work" once Caddy binds 80/443; flag for verification at M2 deploy time. |
| Inbound 443 reachable from macbook | △ | Same behaviour as :80 (TLS handshake aborts on RST, curl exit 35). |
| Disk headroom (`/`) | ⚠ | `/dev/sda1`: 20G total, 13G used, **6.2G available, 68% used**. Pulling Caddy + Postgres + control-plane + worker images will eat ~1–2 GB; should fit but leaves <5 GB headroom. Recommend a `docker system prune` plan in M2. |
| RAM headroom | ⚠ | `total 964 MB`, `used 608 MB`, `free 70 MB`, `available 221 MB`; swap 2047 MB total / 740 MB used. **Postgres + FastAPI + Caddy on 1 GB RAM is tight**. Consider `shared_buffers=128MB`, low `max_connections`, and `--memory` limits in compose. |
| Conflicting services | ⚠ | `openclaw-gateway` container bound to `:18789` (and 127.0.0.1:18792) — does not conflict with Whilly ports. `tailscaled.service` is active (Tailscale check is deferred per user — but the agent is already installed and running). `unattended-upgrades` active (could cause surprise reboots — flag for ops note). No Postgres, no Caddy, no nginx running. |
| PyPI reachable | ✓ | `https://pypi.org/simple/` returns HTML root |
| GitHub reachable | ✓ | `https://github.com/` HTTP 200 |

## Whilly version pinning

| File | Version |
|---|---|
| `whilly/__init__.py` | `__version__ = "4.3.1"` |
| `pyproject.toml` (line 7) | `version = "4.3.1"` |
| `whilly_worker/pyproject.toml` (line 32) | `version = "4.3.1"` |

✓ All three pins agree on **4.3.1**. (The `.venv` editable install reports `4.0.0` because the editable record was installed before the bump and hasn't been re-installed; not a release blocker but worth a `pip install -e '.[dev]'` before mission start to keep version_info consistent.)

## Blockers

**None.** Every hard dependency is satisfied or trivially installable.

## Discovered Constraints

1. **VPS RAM is the tightest constraint.** 964 MB total, 221 MB available, swap heavily used. Compose stack must explicitly cap memory; M3 Prometheus scraping should be sampled, not high-rate.
2. **VPS disk is at 68 %** with 6.2 GB free. Plan for image hygiene (`docker image prune`, `caddy:2-alpine` instead of full image, slim Postgres image).
3. **`openclaw-gateway` already runs on `:18789` and 127.0.0.1:18792.** Whilly must not bind those ports. (M1's planned ports 8000/5432 are clear.)
4. **`tailscaled` is already installed and running** on the VPS. (Not verified per user instructions — but mission's M2 Tailscale spike has a head start: the daemon is up; only `tailscale up` / auth-key flow is needed.)
5. **`unattended-upgrades.service`** can reboot the box mid-task. M2 should document this and consider `dpkg --get-selections` / `apt-mark hold` for kernel pkgs once up.
6. **Inbound 80/443 right now returns `Connection reset by peer`** because nothing is listening. There is no firewall hop dropping SYN-ACKs — the TCP handshake completes — so when Caddy binds these ports they should serve. Re-verify reachability once Caddy is deployed in M2.
7. **DNS tools (`dig`, `nslookup`) absent on VPS.** Add `dnsutils` to bootstrap apt-install if Caddy debugging is needed; `getent hosts <name>` works in the meantime.
8. **`Compose` plugin reports `v5.0.2`** on the VPS — unusual versioning compared to upstream 2.x; assume it implements the v2 schema. Recommend pinning compose schema with `services.<svc>.image` patterns that work on both v2 and v5 plugins.
9. **`.venv` editable install lags pyproject** (records `4.3.1` source but `4.0.0` metadata). Run `pip install -e '.[dev]' --force-reinstall --no-deps` once before mission to align.
10. **`agent-browser` and `tuistory` skills** are not on disk under `~/.factory/skills/`; they are listed in the Factory runtime skill manifest as `(personal)`. Validation invocations must go through the `Skill` tool — confirmed available.

## Recommendations

- Before M1 dispatch: run `pip install -e '.[dev]' --force-reinstall --no-deps` in `.venv/` to align installed version metadata with `4.3.1`.
- M1 LAN compose: bind orchestrator UI/control-plane to `127.0.0.1:8000` on the VPS *behind Caddy* to avoid exposing FastAPI directly; keep Postgres on `127.0.0.1:5432`.
- M2 Caddy: pin `caddy:2-alpine` (≈50 MB) and validate the Caddyfile via `docker run --rm -v $(pwd)/Caddyfile:/etc/caddy/Caddyfile caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile` on the macbook before pushing.
- M2 ACME on sslip.io: HTTP-01 path is viable (public IP confirmed, port 80 free, DNS resolves). Use `213-159-6-155.sslip.io` as the canonical hostname; have a fallback `--internal` Caddy mode for first-boot smoke tests.
- M2 bootstrap tokens: `keyring 25.7.0` is already installed in `.venv` — good to go.
- M3 Prometheus: pin `prometheus-fastapi-instrumentator==7.1.0` and `prometheus-client==0.25.0`. Scrape interval ≥ 30 s on the VPS to keep RSS small. Co-locate Grafana/Loki only if you can spare 200–300 MB RSS — otherwise stream metrics to the macbook.
- M3 SSE: pin `sse-starlette==3.4.1`; test pause/resume against the existing `dashboard.Dashboard` keymap.
- Docker hygiene: run `docker system prune -af --volumes` on the VPS before first M2 deploy to free disk.
- Mid-mission validation: use the `Skill` tool with `agent-browser` for HTMX/SSE UI smoke tests and `tuistory` for `whilly worker connect` CLI flows — both are present in the runtime skill manifest.
- Defer Tailscale work to M2 spike per plan; tailscaled is already running, which de-risks that spike.
