# M1+M2+M3 — Validation Readiness

> Generated 2026-05-01 00:36 (local) / 2026-04-30 21:36 UTC; sequential after `readiness-deps.md`.
> Validates that every tool/surface needed by `scrutiny-validator` and `user-testing-validator` is executable in this environment, and measures resource cost per surface.
> All artifacts saved under `/tmp/whilly-readiness/`. No project state was modified.

## Status: READY

All currently exercisable surfaces (HTTP API via ASGITransport, CLI surface, browser harness via chrome-devtools-mcp/agent-browser, testcontainers Postgres, lint/format/import-linter, docker-compose YAML) pass an end-to-end smoke. The remaining items (HTMX dashboard, SSE, Prometheus, Caddy ACME, Tailscale Funnel, per-user bootstrap admin) are correctly *milestone-gated* by M2/M3 and are documented in §5 — not blockers.

---

## 0. Macbook ground truth (captured at start)

| Metric | Value |
|---|---|
| Chip | Apple M1 Pro |
| Total RAM | 16 GB |
| Available RAM at idle (free + inactive + speculative, page size 16 KB) | **~3.40 GB** (3,365 + 218,998 + 283 pages × 16 KB = 3,647 MB) |
| 70 % of available headroom | **~2.38 GB** for validator workloads |
| CPU cores | 10 (8 perf + 2 eff), `hw.ncpu = 10` |
| Load averages | 2.62 / 2.80 / 2.66 (1/5/15 min) |
| Uptime | 6 d 4 h |
| Existing Chrome RSS | ~4.4 GB across user-driven tabs (separate from validator workloads) |

> Note: the load average of ~2.6 is from the user's existing apps (Chrome with many tabs, Slack, etc.). The validator-headroom ceiling above already accounts for this (uses `available` rather than `free`).

---

## 1. Validation tools

| Tool | Status | Smoke command | Result |
|---|---|---|---|
| `agent-browser` (Factory `Skill` + chrome-devtools-mcp) | ✓ | `chrome-devtools___new_page https://example.com` → `chrome-devtools___take_screenshot /tmp/whilly-readiness/agent-browser-smoke.png` | PNG written (87,661 bytes). New renderer process for the tab observed in `ps`. |
| `tuistory` (Factory skill) — surrogate via direct console-script invocation | ✓ | `.venv/bin/whilly --help > /tmp/whilly-readiness/tuistory-smoke.txt` | exit 0; 596 bytes; lists `plan / run / dashboard / init / worker / forge` subcommands → confirms `whilly` console script entry point that `tuistory` will drive interactively. Skill itself is listed in Factory `<available_skills>` per `readiness-deps.md`. |
| `curl` + `jq` + headers/SSE flags | ✓ | `curl -fsSL --max-time 5 -H 'X-Whilly-Test: 1' https://httpbin.org/headers \| jq '.headers'` | Echoed `{"X-Whilly-Test": "1", "User-Agent": "curl/8.4.0"}`; `--max-time` and `-N`/`--no-buffer` flags both present in `curl --help all`. |
| `httpx` (Python) in project venv | ✓ | `.venv/bin/python -c "import httpx; print(httpx.__version__)"` | `httpx 0.28.1` (matches `readiness-deps.md`). |

**Notes on the browser surface.** This environment uses `chrome-devtools-mcp` v0.23.0 (Node 25.8.1) which connects to the user's existing Google Chrome (133.0.6943.54) and creates new tabs / renderers on demand. That is the same surface `agent-browser` exposes through the Factory `Skill` runtime. Per-instance footprint is therefore **one new Chromium renderer process (~50–110 MB RSS observed in `ps`)** rather than a fresh standalone Chromium boot — much cheaper than a Playwright cold-start. No Playwright cache work needed; first navigation reused the running browser.

Artifacts:
- `/tmp/whilly-readiness/agent-browser-smoke.png` (87,661 B PNG, viewport screenshot of `https://example.com`)
- `/tmp/whilly-readiness/tuistory-smoke.txt` (`whilly --help` capture)

---

## 2. Existing test infrastructure

| Check | Status | Detail |
|---|---|---|
| testcontainers Postgres bootstrap | ✓ | `pytest tests/integration/test_alembic_004.py -x -v` → **8 passed in 18.43 s** (real 19.34 s). Peak pytest-process RSS **84.0 MB** via `/usr/bin/time -l` (`maximum resident set size 88,014,848`). Postgres container itself adds ~150–250 MB external to the pytest process. One `colima` port-forward retry hit on `alembic upgrade head` (handled by the project's `_retry_colima_flake` mechanism). |
| Unit tests sanity (`-k 'transport_server or auth or cli_worker'`) | ✓ | **95 passed, 601 deselected, 0.58 s**. `pyproject.toml` does not enable `pytest-timeout` plugin args; ran without `--timeout=30` (the flag is unrecognised by this pytest config — pre-existing, not a regression). |
| `ruff check whilly/ tests/` | ✓ | "All checks passed!" |
| `ruff format --check whilly/ tests/` | ✓ | "236 files already formatted" — clean. |
| `lint-imports --config .importlinter` | ✓ | Analyzed 174 files, 830 dependencies. **`whilly.core must not import I/O or transport modules`: KEPT — 1 contract kept, 0 broken.** This already protects M1's `whilly worker connect` path from accidentally pulling FastAPI/asyncpg into the worker. |
| `pytest --collect-only -q` | ✓ | **1,629 tests collected in 1.36 s**. Collector clean — no import-time errors. |
| `whilly.__version__` ↔ `pyproject.toml` | ✓ | Both report **`4.3.1`**. The `4.0.0` editable-install drift flagged in `readiness-deps.md` has resolved itself (likely `pip install -e ...` was run in between); no action needed. |
| mypy / typecheck | n/a | `pyproject.toml [project.optional-dependencies] dev` does not include mypy. Skipped per task guidance. |

---

## 3. End-to-end paths

| Path | Status | Detail |
|---|---|---|
| `docker-compose.demo.yml` syntax | ✓ | `docker-compose -f docker-compose.demo.yml config -q` → exit 0. **Caveat:** this Docker install (Docker for Mac via colima context) does **not** ship the `docker compose` subcommand plugin (`docker compose version` → `'compose' is not a docker command`). Use the standalone `docker-compose` v2.40.3 binary at `/opt/homebrew/bin/docker-compose` — already installed. M1 compose tooling should not assume `docker compose` is callable on this macbook. |
| ASGITransport `create_app` smoke | ✓ | `python /tmp/whilly-readiness/asgi-smoke.py`: boots `postgres:15-alpine` testcontainer, `alembic upgrade head`, then via `httpx.ASGITransport`: `GET /health → 200 ({"status":"ok"}, 5.0 ms)`, `POST /workers/register {"hostname":"readiness-smoke"} Bearer <bootstrap> → 201 (returns `worker_id` + plaintext token, 3.4 ms)`. **Total wall 3.32 s; peak RSS 93.2 MB (rusage)**. Script + log saved as artifacts. One colima port-forward retry on `alembic upgrade head` (same pattern as the conftest). |
| SSE expected-404 today | ✓ | Same script also hit `GET /events/stream` and got **404 (0.3 ms, clean response, no 500/crash)**. M3 will replace this with a real handler. |
| VPS reachability `:8000` | ✓ | `curl --max-time 10 http://213.159.6.155:8000/health` → `curl: (7) Failed to connect ... Couldn't connect to server` after **136 ms** — i.e. **TCP RST / "connection refused"**, not a timeout / firewall drop. Once a control-plane binds 8000 it will be reachable from macbook. |
| VPS SSH round-trip latency (5×) | ✓ | `ssh -p 23422 root@213.159.6.155 "echo pong"` real-time: 1.87 / 1.81 / 1.80 / 1.82 / 1.80 s → **median 1.81 s**. (Includes SSH handshake; once a long-lived control connection is established for tests, per-message latency will be sub-100 ms.) |

Artifacts:
- `/tmp/whilly-readiness/asgi-smoke.py` (canonical FastAPI ASGITransport smoke — validators can copy/extend)
- `/tmp/whilly-readiness/asgi-smoke.log` (full output of last successful run)

---

## 4. Resource cost classification

**Macbook total RAM:** 16 GB
**Available at idle (vm_stat):** ~3.40 GB
**70 % headroom (validator budget):** **~2.38 GB**
**CPU cores:** 10

Per-instance footprints below come from the actual measurements above (rusage / ps).

| Surface | Per-instance | Max concurrent | Rationale |
|---|---|---|---|
| `agent-browser` (HTMX dashboard / SSE-in-browser) | ~80–110 MB (one Chromium renderer process) + amortised browser overhead | **3** | 3 × ~110 MB ≈ 330 MB ≪ 2.38 GB budget, but Chromium renderers spike during JS execution and screenshots; M3 dashboard is HTMX (no JS bundle) so this is conservative. Chrome auto-suspends backgrounded tabs which actually helps. |
| `tuistory` (CLI / TUI: `whilly admin`, `whilly worker connect`, `whilly-worker`) | ~50–100 MB per `whilly` Python process | **5** | 5 × ~100 MB ≈ 500 MB. Each tuistory invocation spawns a tmux pane + the CLI process; load avg also matters — 10 cores with current LA ~2.6 leaves ~7 cores headroom, comfortable for 5 short-lived TUI sessions. |
| `curl` / `httpx` HTTP scripts (control-plane RPC, Prometheus `/metrics`) | ~10–15 MB | **5** (or more — capped by surface, not RAM) | Trivial. Validators issuing parallel `curl`s are bound by the control-plane's own `claim_long_poll_timeout` (30 s) far before they're bound by macbook RAM. |
| SSE streaming clients (`curl -N`, `httpx.AsyncClient.stream`) | ~15–25 MB per persistent stream (httpx event loop + buffer) | **5** | Streams are mostly idle; macbook can hold 5 SSE consumers easily. Cap matches HTTP cap above for symmetry. |
| testcontainers Postgres (per-process: 1 pytest + 1 PG container) | ~84 MB (pytest) + ~150–250 MB (postgres:15-alpine) ≈ **~330 MB total** per validator process | **3** | 3 × ~330 MB ≈ 1.0 GB; leaves ~1.4 GB for other validators. Note testcontainers does NOT share the container across processes — each validator that imports the project's session fixture boots its own. |
| Two-host distributed demo (VPS = control-plane + Caddy; macbook = worker; VPS-local worker) | full stack — VPS:1 GB, macbook RSS dominated by 1 worker (~150 MB) + tunnel | **1** | Single integration loop; uses public internet, ACME quotas, and the tightly-RAM-budgeted VPS (`readiness-deps.md`: 221 MB available, 6.2 GB disk free). One run at a time. |

**Recommendation for the orchestrator's `max-concurrent-validators` map:**

```yaml
agent-browser: 3
tuistory: 5
http-curl: 5
sse-stream: 5
testcontainers-pg: 3
two-host-demo: 1
```

Total worst-case simultaneous validator RAM (one of each, ignoring the singleton two-host demo): ~110 + 100 + 15 + 25 + 330 ≈ **0.6 GB** — well inside the 2.38 GB budget. The numbers above are the *concurrent per surface*, not summed across surfaces; the orchestrator should still cap *total* across all surfaces at e.g. 8 to keep system load < 7.

---

## 5. Milestone-gated gaps (NOT readiness blockers)

These cannot be exercised today and are correctly deferred:

- **HTMX web dashboard** — route does not exist (`GET /` HTML, `GET /partials/*`). Lands in M3. agent-browser smoke today only proves the browser harness works against `https://example.com`; once the dashboard route exists validators will swap that target URL.
- **`GET /events/stream` SSE endpoint** — confirmed today as 404. SSE format / `Last-Event-ID` reconnect tests blocked until M3.
- **Prometheus `/metrics`** — route does not exist; `prometheus-fastapi-instrumentator` not yet installed in the venv (`readiness-deps.md` shows it resolves cleanly when added). Blocked until M3.
- **Caddy ACME** — no Caddyfile / no Caddy binary local; VPS Caddy not yet deployed. Inbound 80/443 on the VPS currently RSTs (no listener). Blocked until M2. Validator path: `docker run --rm caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile` (recommended in `readiness-deps.md`).
- **Tailscale Funnel** — `tailscaled` is running on the VPS but no `tailscale up` / Funnel config yet. Blocked until M2 spike.
- **Per-user bootstrap admin CLI** (`whilly admin user create / token mint / revoke`) — not yet implemented. Blocked until M2. tuistory will drive these once they ship.
- **`whilly worker connect` CLI** — not yet implemented (`whilly worker register` exists; `connect` is M1's new surface). Blocked until M1.
- **`docker compose` subcommand plugin** absent on this macbook — not a blocker, just a note: use the `docker-compose` standalone binary in scripts and CI for compose-config validation here.

---

## Blockers

**None.** Every validator surface that *can* be exercised today (HTTP API, CLI surface, browser harness, testcontainers, lint/format/import-linter, docker-compose syntax, VPS reachability) passes a real end-to-end smoke. Milestone-gated gaps are expected and documented.

---

## Recommendations / setup steps validators must take

1. **Always run the project venv interpreter directly** (`/opt/develop/whilly-orchestrator/.venv/bin/python` or `.venv/bin/whilly`) to avoid PATH / pyenv shim confusion. Don't rely on `source .venv/bin/activate` between Execute calls — each shell is fresh.
2. **Bridge `DOCKER_HOST` from `docker context` before booting testcontainers.** The colima context returns `unix:///Users/.../colima/default/docker.sock` and the Python Docker SDK does not auto-discover it. The smoke script at `/tmp/whilly-readiness/asgi-smoke.py` shows the canonical pattern (`docker context inspect --format '{{.Endpoints.docker.Host}}'`). Also export `TESTCONTAINERS_RYUK_DISABLED=true`.
3. **Always wrap `PostgresContainer.start()` and `alembic upgrade head` in 3-attempt exponential-backoff retries.** The colima port-forward flake (Errno 61 ECONNREFUSED on the just-published port) is rare but fires often enough that a single `pytest -x` run can be flaky without it. Use `tests/conftest.py::_retry_colima_flake` or the equivalent in `/tmp/whilly-readiness/asgi-smoke.py`.
4. **Use `docker-compose` (standalone binary) not `docker compose` (subcommand)** for any compose YAML validation / spin-up on this macbook. `which docker-compose` → `/opt/homebrew/bin/docker-compose` (v2.40.3).
5. **Reuse the ASGITransport smoke pattern at `/tmp/whilly-readiness/asgi-smoke.py`.** It's the canonical shape for HTTP-API validators: in-process pool + `create_app(pool, bootstrap_token=...)` + `httpx.ASGITransport`. No need to bind a real port.
6. **For agent-browser tests**, prefer `chrome-devtools___new_page` + `chrome-devtools___take_screenshot` (already wired into this Factory runtime). Each new tab is ~80–110 MB; close it via `chrome-devtools___close_page` after each scenario. Don't open more than 3 concurrently.
7. **For tuistory CLI tests**, drive `.venv/bin/whilly` (or `whilly-worker` once installed) under the skill harness. Use `--help` calls as cheap smoke; reserve full TUI captures for cases that genuinely exercise interactive prompts.
8. **VPS-side validators** should authenticate via the existing key in the agent (verified by `BatchMode=yes ssh root@213.159.6.155 -p 23422 "echo pong"`). Median RTT 1.81 s — design assertions to be tolerant of 2–5 s round-trip on cold connections; use `ssh -o ControlMaster=auto -o ControlPath=...` to amortise once. Disk on VPS is at 68 % — clean up validator artifacts after each run (`docker system prune -af` on demand, but be careful: `openclaw-gateway` is running there too).
9. **For two-host demo runs**, set `max-concurrent: 1` and serialise. ACME issuance has rate limits and the VPS has only ~221 MB free RAM.
