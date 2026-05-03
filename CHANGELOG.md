# Changelog

All notable changes to Whilly Orchestrator will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — NEXT

> **v4.5 entry preview.** The block immediately below previews the
> changes that will ship in **v4.5.0** (M2 of the Whilly Distributed
> v5.0 mission). Code-side M2 features (`m2-worker-url-refresh-on-
> rotation`, `m2-cross-host-demo`, `m2-release-v4.5`) are still
> landing — this entry is the docs / runbook + sidecar portion. The
> release-driver feature (`m2-release-v4.5`) will move this preview
> into a dated `## [4.5.0] - YYYY-MM-DD` section at sign-off.

### Added (v4.5 — M2 funnel sidecar — date TBD until release)

- **`funnel` sidecar service** (`m2-localhostrun-funnel-sidecar`).
  Adds a profile-gated `funnel` service to **both**
  `docker-compose.demo.yml` and `docker-compose.control-plane.yml`.
  The sidecar holds an outbound SSH reverse tunnel against
  `nokey@localhost.run` (free anonymous tier — no account, no key),
  parses the assigned `https://<random>.lhr.life` URL from SSH
  stdout, and publishes it to:
  - The new `funnel_url` Postgres singleton table (primary;
    created by migration **010**).
  - The shared-volume file `/funnel/url.txt` (fallback for workers
    without postgres reachability).
  Auto-reconnects with exponential backoff on disconnect and
  re-publishes the freshly-assigned URL. Default `docker compose
  ... up` is byte-equivalent to v4.4 — the sidecar only starts with
  `--profile funnel`. Replaces the previously-planned (and
  cancelled) Caddy + ACME + Tailscale Funnel stack.
- **`Dockerfile.funnel`** — ≤ 32 MB Alpine image
  (`alpine:3.20 + openssh-client + bash + curl + postgresql-client`).
- **`scripts/funnel/run.sh`** — bash loop that runs the SSH tunnel,
  parses the `lhr.life` URL via `https://[a-z0-9-]+\.lhr\.life`
  regex, writes the file via tmp-file + atomic rename, and upserts
  the Postgres row via `INSERT ... ON CONFLICT (id) DO UPDATE`.
  Honours env vars `FUNNEL_LOCAL_HOST` / `FUNNEL_LOCAL_PORT`
  (defaults `control-plane:8000`), `FUNNEL_SERVER_ALIVE_INTERVAL`
  (default 60 s), `FUNNEL_RETRY_BACKOFF_SECONDS` (default 5 s),
  `FUNNEL_URL_FILE` (default `/funnel/url.txt`), and
  `WHILLY_DATABASE_URL` (parsed once into discrete `PG*` env vars
  so the password never appears on `psql` argv).
- **Migration 010 — `funnel_url` table.** `CREATE TABLE
  funnel_url (id INTEGER PRIMARY KEY DEFAULT 1, url TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), CONSTRAINT
  funnel_url_singleton CHECK (id = 1));`. Singleton invariant is
  enforced at the schema level; `schema.sql` is hand-updated in the
  same commit (mission migration discipline).
- **`docs/Distributed-Setup.md` § "Two-host via localhost.run"** —
  end-to-end walkthrough for both Scenario A (laptop-host
  control-plane, workers anywhere) and Scenario B (VPS-host
  control-plane, workers on laptops). Documents the URL-discovery
  strategies (postgres re-discovery vs one-shot static URL), the
  free-tier rotation caveat, and links to the M3 stable-URL path.

### Documentation (v4.5 — date TBD until release)

### Documentation (v4.5 — date TBD until release)

- **v4.5 (M2) deploy + ops doc set landed.** Three new operator
  docs ship under `docs/`:
  - [`docs/Deploy-M2.md`](docs/Deploy-M2.md) — M2 deploy walkthrough
    covering both topologies (laptop-host vs VPS-host control-plane),
    the localhost.run **staging vs prod** decision matrix
    (free anonymous tier with rotating `https://<random>.lhr.life`
    URL vs the deferred SSH-key stable-URL path), profile-gated
    `funnel` sidecar bring-up, worker-side URL re-discovery via
    `WHILLY_FUNNEL_URL_SOURCE=postgres|file`, and the full env-var
    reference for v4.5.
  - [`docs/Cert-Renewal.md`](docs/Cert-Renewal.md) — TLS / cert
    renewal runbook. Documents the file paths the `funnel` sidecar
    uses (`/funnel/url.txt` shared volume, `funnel_url` Postgres
    table, `~root/.ssh/known_hosts` inside the alpine container),
    explains why there is no `certbot renew` step on Whilly's side
    (TLS terminated upstream by localhost.run with a Let's Encrypt
    prod wildcard cert for `*.lhr.life`), and gives the
    force-renew procedure (`docker compose restart funnel`) plus
    the migration path to a self-managed cert (Caddy / Cloudflared
    / etc.) when you outgrow localhost.run.
  - [`docs/Token-Rotation.md`](docs/Token-Rotation.md) — admin-token
    rotation runbook with **two separate playbooks**: Playbook A for
    a per-user bootstrap-token leak (`whilly admin bootstrap
    revoke <prefix>` + re-mint, limited blast radius, 5-min SLA)
    and Playbook B for an admin / shared legacy-token leak (full
    cluster rotation, mass-revoke, forensic checklist, 30-min
    SLA). Includes a per-worker bearer-leak side path
    (`whilly admin worker revoke <id>`) and a mandatory
    post-rotation forensic checklist.
- **DEMO.md** gets a new **«Сценарий M2 — public exposure через
  localhost.run (v4.5)»** section walking through `--profile
  funnel` bring-up, per-operator bootstrap minting via `whilly
  admin bootstrap mint`, worker connect against the live
  `lhr.life` URL, and `whilly admin worker revoke` live eviction.
  The trailing «Дальше — M2 (Caddy / Tailscale Funnel)» pointer
  is updated to reflect the 2026-05-02 pivot (localhost.run
  sidecar replaces both cancelled paths).
- **README.md** quickstart gets a cross-link from the M1
  distributed-deployment block over to `docs/Deploy-M2.md` and the
  two adjacent runbooks so operators can find them by name without
  spelunking the full `docs/` tree.
- **Pivot reflected throughout.** All references to "Caddy + ACME"
  / "Tailscale Funnel" / "custom CA bundle on the worker" in M2
  context are removed or rewritten to point at the localhost.run
  sidecar (per AGENTS.md mission boundaries — see also the cancelled
  `m2-tailscale-funnel`, `m2-caddy-compose`, `m2-worker-cert-trust`
  features in `features.json`).

- **Backcompat strategy clarified: the v3-flag shim is the canonical
  v4 backcompat surface.** The v3 in-process Wiggum loop was removed in
  TASK-107 (commit `91bcfbd`); the legacy-flag shim added in commit
  `0bfd8b1` (`whilly/cli/__init__.py::_apply_legacy_shim`) is the only
  authoritative compatibility layer for v3-era top-level flags
  (`--tasks`, `--headless`, `--init`, `--prd-wizard`, `--workspace`,
  `--no-workspace`, `--no-worktree`, `--resume`, `--reset`, `--all`).
  The shim rewrites argv into the equivalent v4 subcommand
  (`run --plan`, `init`, `plan reset`) and lets the v4 dispatcher take
  over — it does **not** restore the v3 in-process executor, the
  `.whilly_state.json` resume file, or the `.whilly_workspaces/{slug}/`
  git-worktree feature. The mission validation contract was reframed
  (mission feature `m1-backcompat-reframer`) to match this reality:
  13 backcompat assertions now test the shim's argv-rewrite + dispatch
  contract instead of v3-loop semantics, one assertion
  (`VAL-CROSS-BACKCOMPAT-904`, the `.whilly_state.json` forward-readability
  chain) is removed because the file format is dead. See
  `library/architecture.md` § "Backcompat strategy (v3 → v4)" for the
  full rewrite table and rationale. **No source code changes** ship in
  this NEXT entry — the shim implementation has been in place since
  v4.4.0 and the reframe is contract-document only.

## [4.4.3] - 2026-05-03

> **Patch release — fixes a third M1-round-6 regression in v4.4.2's
> published runtime image.** No new features. Strictly additive: every
> v4.4.2 user should upgrade. v4.4.2 is officially deprecated; see the
> GitHub Release banner.

### ⚠️ v4.4.2 deprecation notice

`mshegolev/whilly:4.4.2` (Docker Hub) shipped with one confirmed
regression surfaced after the round-6 v4.4.2 publish smoke:

1. **`mshegolev/whilly:4.4.2` runtime image excluded
   `tests/fixtures/fake_claude.sh` and
   `tests/fixtures/fake_claude_demo.sh`, so workers running
   `WHILLY_IMAGE_TAG=4.4.2 bash workshop-demo.sh --cli stub` claimed
   tasks but immediately failed with
   `claude binary not found at /opt/whilly/tests/fixtures/fake_claude_demo.sh`.**
   `Dockerfile.demo` already shipped these fixtures, but the production
   `Dockerfile` runtime stage did not. Blocked the canonical end-to-end
   proof of `VAL-M1-COMPOSE-011` against the published image.

`mshegolev/whilly:4.4.2` and `whilly-worker==4.4.2` remain reachable on
their respective registries (Docker Hub digest discipline + PyPI's
no-overwrite policy) for users who pin by digest, but **users on tag
`:4.4.2` or `==4.4.2` should upgrade to v4.4.3 immediately.**

### Fixed

- **Runtime image now ships `tests/fixtures/fake_claude.sh` and
  `tests/fixtures/fake_claude_demo.sh`.** Added a targeted
  `COPY tests/fixtures/fake_claude.sh tests/fixtures/fake_claude_demo.sh
  /opt/whilly/tests/fixtures/` to the runtime stage of `Dockerfile`,
  matching the contract that already held in `Dockerfile.demo`. Both
  scripts are chmod-ed `+x` and end up at
  `/opt/whilly/tests/fixtures/fake_claude*.sh` so
  `workshop-demo.sh --cli stub` (which points worker `CLAUDE_BIN` at
  the demo stub) now drains all 5 tasks against the published image.
  New regression test
  `tests/integration/test_runtime_image_ships_fake_claude_stubs.py`
  is the install-time gate (static parse + docker-gated `ls -la`
  check; skips cleanly when the docker daemon is unavailable). Fix
  committed in `4167c5e`
  (`fix(m1): runtime image ships fake_claude stubs for workshop-demo
  --cli stub (VAL-M1-COMPOSE-011)`).

### Verification

- `docker buildx imagetools inspect mshegolev/whilly:4.4.3` → both
  `linux/amd64` and `linux/arm64` manifests; `Config.Cmd` =
  `["control-plane"]` on both arches.
- `mshegolev/whilly:latest` moved to point at v4.4.3 (Docker Hub tag
  rewrite; v4.4.3 digest stable for users pinning by digest).
- `docker run --rm mshegolev/whilly:4.4.3 ls /opt/whilly/tests/fixtures/`
  lists `fake_claude.sh` and `fake_claude_demo.sh`, both executable.
- `WHILLY_IMAGE_TAG=4.4.3 bash workshop-demo.sh --cli stub --no-color`
  exits 0 with `5 DONE 0 PENDING` (canonical proof of
  `VAL-M1-COMPOSE-011`).
- `python3.12 -m pip download whilly-orchestrator==4.4.3 --no-deps` and
  `python3.12 -m pip download whilly-worker==4.4.3 --no-deps` both
  succeed against `files.pythonhosted.org`.

## [4.4.2] - 2026-05-03

> **Patch release — fixes two M1-round-6 regressions in v4.4.1's
> published artefacts.** No new features. Strictly additive: every
> v4.4.1 user should upgrade. v4.4.1 is officially deprecated; see the
> GitHub Release banner.

### ⚠️ v4.4.1 deprecation notice

`mshegolev/whilly:4.4.1` (Docker Hub) shipped with two confirmed
regressions surfaced by M1 user-testing-validator round 6:

1. **`mshegolev/whilly:4.4.1` runtime image excluded `examples/demo/`,
   so `WHILLY_IMAGE_TAG=4.4.1 bash workshop-demo.sh --cli stub` failed
   at the plan-import step.** The runtime stage of the multi-stage
   `Dockerfile` deliberately did not `COPY examples/`; `workshop-demo.sh`
   shells out `whilly plan import "$PLAN_FILE"` where `PLAN_FILE` is
   `/opt/whilly/examples/demo/parallel.json`, so the demo could not
   drain its 5 tasks against `mshegolev/whilly:4.4.1`. Blocked
   `VAL-M1-COMPOSE-011` end-to-end.
2. **`docker/entrypoint.sh` legacy register-and-exec path ignored
   `WHILLY_INSECURE=1`.** The non-`WHILLY_USE_CONNECT_FLOW` branch
   `exec`'d `whilly-worker` without `--insecure`, so plain-HTTP control
   planes (the M1 cross-host smoke flow before the M2 funnel sidecar
   ships) were rejected by the worker scheme guard regardless of the
   env var. Blocked `VAL-M1-ENTRYPOINT-001`.

`mshegolev/whilly:4.4.1` and `whilly-worker==4.4.1` remain reachable on
their respective registries (Docker Hub digest discipline + PyPI's
no-overwrite policy) for users who pin by digest, but **users on tag
`:4.4.1` or `==4.4.1` should upgrade to v4.4.2 immediately.**

### Fixed

- **Runtime image now ships `examples/demo/`.** Added
  `COPY examples /opt/whilly/examples/` to the runtime stage of
  `Dockerfile` (~10 KB total — negligible). Updated the leading
  comment to reflect the new contract. New regression test
  `tests/integration/test_runtime_image_ships_examples_demo.py` is the
  install-time gate (static parse + docker-gated `ls` check; skips
  cleanly when the docker daemon is unavailable). Fix committed in
  `02fc9f2` (`fix(m1): runtime image ships examples/demo for
  workshop-demo.sh (VAL-M1-COMPOSE-011)`).
- **Entrypoint legacy path honours `WHILLY_INSECURE`.** Wrapped the
  legacy `exec` in a `worker_argv` array and conditionally appended
  `--insecure` via the existing `is_truthy` helper, mirroring the
  connect-flow branch. The truthiness matrix (`1` / `true` / `yes` /
  `on`, case-insensitive) is identical to the connect-flow handling.
  Restores the v4.3.1 plain-HTTP behaviour the M1 round-5 contract
  reframer described. New regression test
  `tests/unit/test_entrypoint_legacy_path.py` covers the truthiness
  matrix, the pre-supplied `WHILLY_WORKER_TOKEN` sub-branch, and
  positional `"$@"` pass-through (23 cases). Fix committed in
  `c5aa5fb` (`fix(m1): honor WHILLY_INSECURE in entrypoint legacy
  register-and-exec path`).

### Verification

- `docker buildx imagetools inspect mshegolev/whilly:4.4.2` → both
  `linux/amd64` and `linux/arm64` manifests; `Config.Cmd` =
  `["control-plane"]` on both arches.
- `mshegolev/whilly:latest` moved to point at v4.4.2 (Docker Hub tag
  rewrite; v4.4.2 digest stable for users pinning by digest).
- `docker run --rm mshegolev/whilly:4.4.2 ls /opt/whilly/examples/demo`
  lists `parallel.json`, `tasks.json`, `PRD-demo.md`.
- `WHILLY_IMAGE_TAG=4.4.2 bash workshop-demo.sh --cli stub --no-color`
  exits 0 with `5 DONE 0 PENDING` (canonical proof of
  `VAL-M1-COMPOSE-011`).
- Smoke-run of the published image with
  `-e WHILLY_INSECURE=1` (no `WHILLY_USE_CONNECT_FLOW`) against a
  plain-HTTP control plane confirms the scheme guard is bypassed.
- `python3.12 -m pip download whilly-orchestrator==4.4.2 --no-deps` and
  `python3.12 -m pip download whilly-worker==4.4.2 --no-deps` both
  succeed against `files.pythonhosted.org`.

## [4.4.1] - 2026-05-02

> **Patch release — fixes two regressions in v4.4.0's published
> artefacts.** No new features. Strictly additive: every v4.4.0 user
> should upgrade. v4.4.0 is officially deprecated; see the GitHub
> Release banner.

### ⚠️ v4.4.0 deprecation notice

`mshegolev/whilly:4.4.0` (Docker Hub) and `whilly-worker==4.4.0`
(PyPI) shipped with two confirmed regressions:

1. **`mshegolev/whilly:4.4.0` had `Config.Cmd=["worker"]` instead of
   `["control-plane"]`.** Commit `7ae66b7` appended the `worker-builder`
   + `worker` stages AFTER the `runtime` stage in `Dockerfile`; since
   `.github/workflows/docker-publish.yml` invokes
   `docker/build-push-action@v6` without a `target:` key, the default
   build target silently switched to `worker`. Operators running
   `docker run mshegolev/whilly:4.4.0` (no explicit command) got a
   worker process instead of the control-plane.
2. **`whilly-worker==4.4.0` crashed on `--help`.** Top-level imports in
   `whilly/cli/worker.py` eagerly pulled in `fastapi` / `asyncpg` /
   `sse-starlette` / `prometheus_fastapi_instrumentator`. Operators who
   followed the documented `pip install whilly-worker` install path
   (which only installs the `[worker]` dep closure: `httpx` + base)
   could not even run `whilly-worker --help` — argparse never got a
   chance because the import chain raised `ModuleNotFoundError:
   fastapi` at startup.

`mshegolev/whilly:4.4.0` and `whilly-worker==4.4.0` remain reachable on
their respective registries (Docker Hub digest discipline + PyPI's
no-overwrite policy) for users who pin by digest, but **users on tag
`:4.4.0` or `==4.4.0` should upgrade to v4.4.1 immediately.**

### Fixed

- **Dockerfile stage order.** Reordered so `runtime` is the LAST
  `FROM ... AS ...` stanza (`builder → worker-builder → worker →
  runtime`). `docker buildx build .` (no `--target`) now correctly
  produces the multi-role image with `CMD=["control-plane"]`. Defence
  in depth: `.github/workflows/docker-publish.yml` was also updated to
  pin `target: runtime` so the published image is correct even if a
  future commit accidentally appends another stage. New regression
  test `tests/integration/test_dockerfile_default_target_cmd.py`
  parses the Dockerfile and asserts the last stage is `runtime`. Fix
  committed in `4d143a2` (`fix(m1): reorder Dockerfile so runtime is
  the last stage`).
- **`whilly-worker --help` no longer ImportErrors on a `[worker]`-only
  install.** Deferred all `fastapi` / `asyncpg` /
  `prometheus_fastapi_instrumentator` / `sse-starlette` imports to
  inside the lifespan-bound code paths in
  `whilly/cli/worker.py::main`. The worker entry's import closure now
  matches the `lint-imports --config .importlinter` contract: only
  `httpx` / `pydantic` / `whilly.core` /
  `whilly.adapters.transport.client` are touched. New regression test
  `tests/unit/test_whilly_worker_help_runs_with_worker_extra_only.py`
  reproduces the v4.4.0 failure mode and asserts `--help` exits 0 in
  a venv that only has `whilly-orchestrator[worker]` installed. Fix
  committed in `e4bc4eb` (`fix(m1): defer fastapi/asyncpg imports in
  worker entry closure`).

### Verification

- `docker buildx imagetools inspect mshegolev/whilly:4.4.1` → both
  `linux/amd64` and `linux/arm64` manifests; `Config.Cmd` =
  `["control-plane"]` on both arches.
- `mshegolev/whilly:latest` moved to point at v4.4.1 (Docker Hub tag
  rewrite; v4.4.1 digest stable for users pinning by digest).
- `python3.12 -m pip download whilly-orchestrator==4.4.1 --no-deps` and
  `python3.12 -m pip download whilly-worker==4.4.1 --no-deps` both
  succeed against `files.pythonhosted.org`.
- In a fresh `python3.12 -m venv` with only
  `pip install whilly-worker==4.4.1`, `whilly-worker --help` exits 0
  (no `ModuleNotFoundError: fastapi`).

## [4.4.0] - 2026-05-01

> **M1 of Whilly Distributed v5.0 — split-host deployments.** Adds two new
> additive compose files (`docker-compose.control-plane.yml`,
> `docker-compose.worker.yml`), the one-line `whilly worker connect <url>`
> bootstrap, and the supporting docs/env knobs needed for a VPS+laptops
> deployment shape. **Strictly additive** — existing single-host
> `docker-compose.demo.yml`, `workshop-demo.sh`, and `mshegolev/whilly:4.3.1`
> continue to work identically and pass `bash workshop-demo.sh --cli claude`
> byte-for-byte.

### Added

- **`docker-compose.control-plane.yml`** — postgres + control-plane only,
  sized for the 964 MB-RAM VPS profile (256 MB cap each, PG tuned with
  `shared_buffers=64MB` / `work_mem=4MB`). Bind interface controlled by the
  new `WHILLY_BIND_HOST` env var (default `127.0.0.1`; set `0.0.0.0` /
  `::` / explicit IP to expose). IPv6 wildcard supported via long-form
  port mapping (the short form swallows colons).
- **`docker-compose.worker.yml`** + **`.env.worker.example`** — single
  worker service that targets a remote control-plane via
  `WHILLY_CONTROL_URL`. Workspace volume placeholder (`./workspace:/work`)
  declared but unused at M1 — reserved for the M4 per-worker plan
  workspace (see `docs/Workspace-Topology.md`).
- **`whilly worker connect <url>`** — one-line operator bootstrap. Validates
  URL (scheme guard, port range, no path), registers via the bootstrap
  token, persists the per-worker bearer in the OS keychain (with a
  chmod-600 `~/.config/whilly/credentials.json` fallback for headless
  Linux), then `execvp`s into `whilly-worker`. Stdout shape is
  line-oriented (`worker_id: ...` / `token: ...`) so it pipes cleanly into
  shell scripts. Extensive failure-mode handling: 401 / unreachable / 5xx
  retries / SIGINT mid-register / missing-on-PATH are all surfaced with
  actionable stderr.
- **`whilly-worker --insecure`** — explicit opt-in for plain HTTP to a
  non-loopback control-plane URL. Loopback hosts (`127.0.0.0/8`, `::1`,
  `localhost`) are exempt; RFC1918 / link-local / `0.0.0.0` are NOT
  exempt. Mirrors on `whilly worker connect`.
- **`docker/entrypoint.sh` `WHILLY_USE_CONNECT_FLOW` switch** — when truthy
  (`1` / `true` / `yes` / `on`, case-insensitive), the worker container
  delegates registration + keychain persistence + exec to `whilly worker
  connect` instead of the legacy bash-awk register path. **Default OFF**
  preserves byte-equivalent v4.3.1 stderr/stdout behaviour for the
  workshop demo.
- **`docs/Distributed-Setup.md`** — VPS-A control-plane → laptop-B/C
  workers walkthrough. Copy-paste-ready commands, env-var reference, and
  cross-link to the workspace-topology design doc. References the
  canonical audit-report mirror at `library/distributed-audit/`.
- **`docs/Workspace-Topology.md`** — design-only spec for the M4
  per-worker editing workspace. Locks in **Option A** (per-worker git
  clone + push-branch); options B (shared workspace) and C (patch-based)
  are documented and ruled out. Carries an explicit "design only — NOT
  implemented in this mission" callout to prevent confusion with shipping
  features.
- **DEMO.md** — new "Сценарий M1 — two-host demo" section walking through
  the VPS + macbook + VPS-local-worker path.
- **`.env.example`** — documented `WHILLY_BIND_HOST` and
  `WHILLY_USE_CONNECT_FLOW` with default values, semantics, and example
  overrides.
- **README.md** — quickstart now references
  `docker-compose.control-plane.yml`, `docker-compose.worker.yml`, and
  `whilly worker connect`. Documentation index links the two new docs.
- **`scripts/m1_baseline_fixtures.py` extension** — the existing
  idempotent fixture mirror now also writes
  `library/distributed-audit/` (canonical M1 location per
  VAL-M1-DOCS-004 / VAL-M1-COMPOSE-902) in addition to the
  `docs/distributed-audit/` mirror introduced by m1-readiness-baseline.
  Re-runs are byte-equality no-ops.

### Compatibility

- `docker-compose.demo.yml`, `workshop-demo.sh`, `Dockerfile.demo`, and
  `mshegolev/whilly:4.3.1` are unchanged from v4.3.1. The single-host
  workshop demo is verified byte-equivalent on every M1 validator pass.
- `WHILLY_USE_CONNECT_FLOW` defaults to OFF; existing containers that
  rely on the legacy bash-awk register flow continue to use it.
- All v3-era CLI flags (`whilly --tasks`, `--headless`, `--resume`,
  `--reset`, `--init`, `--prd-wizard`) continue to dispatch correctly;
  the `whilly worker` / `whilly admin` subcommand additions do not
  shadow them.

## [4.3.1] - 2026-04-30

> **Hotfix: Node 22 LTS + четвёртый agentic CLI (Codex).** Production-образ
> v4.3.0 ронял `gemini --version` с `SyntaxError: Invalid regular
> expression flags` потому что Debian bookworm даёт ноду 18, а
> `@google/gemini-cli` использует regex-флаги недоступные до Node 20.
> Bump базы → Node 22 LTS через NodeSource. Добавлен **OpenAI Codex CLI**
> (gpt-5.x семейство) как четвёртый --cli; общий dispatcher
> (`docker/cli_adapter.py`) теперь умеет `claude-code | gemini | opencode
> | codex`.

### Fixed

- **`@google/gemini-cli` поднимается в production-образе.** Заменили
  Debian'овский `apt install nodejs npm` (даёт Node 18.20 на bookworm)
  на NodeSource `setup_22.x` + `nodejs` (Node 22 LTS включает npm).
  Затрагивает оба `Dockerfile` и `Dockerfile.demo`. Build-time sanity
  check теперь дополнительно дёргает `--version` у каждого CLI — мы
  поймаем такой регресс на сборке, а не на runtime.

### Added

- **Codex CLI (`--cli codex`).** OpenAI's official Codex CLI (`@openai/codex`,
  v0.128+) теперь часть production-образа. Поддерживает sub-agents, skills,
  MCP, plugins, AGENTS.md, hooks, sandbox modes — то же самое, что и
  остальные три CLI. `docker/cli_adapter.py:run_codex` парсит JSONL-stream
  событий (`thread.started` / `turn.started` / `item.completed` /
  `turn.completed`), читает финальный agent message из `--output-last-message`
  файла, суммирует `usage.input_tokens` + `output_tokens` +
  `reasoning_output_tokens` из всех `turn.completed` events. Permission/sandbox
  off через `--dangerously-bypass-approvals-and-sandbox` (analog claude's
  `--dangerously-skip-permissions`).
- **`openai` provider в `llm_resource_picker`.** Tier→model map для codex:
  TINY/SMALL → `gpt-5.4-mini` (fast/cheap), MEDIUM/LARGE → `gpt-5.4`
  (флагман для API-key auth; `gpt-5.5` lock'нут на ChatGPT subscription
  и поэтому не используется в default map). Override через `LLM_MODEL`.
- **`workshop-demo.sh --cli codex`.** Ожидает `OPENAI_API_KEY`. Также
  выставляет `CODEX_HOME=/home/whilly/.codex` для config + auth cache
  (можно volume-mount'ить read-only снаружи для skills/plugins).
- **Unit-тесты codex adapter'а.** `tests/unit/test_cli_adapter.py`
  пополнился классом `TestCodexAdapter` (10 cases: argv, single/multi-turn
  usage, last-msg fallback на stream events, auth/general/timeout/missing
  failure modes, model resolution через picker).

### Changed

- Image-side версия CLI обвязки теперь четыре: claude / gemini / opencode /
  codex. Размер production-образа подрос примерно на ~150 MB
  (codex bundle), итого ~1.6 GB.
- `cli_adapter.SUPPORTED_CLIS` добавился `codex`; `_RUNNERS` keys теперь
  `{claude-code, opencode, gemini, codex}`.

## [4.3.0] - 2026-04-30

> **Agentic CLIs ship in the box.** Production Docker image
> (`mshegolev/whilly:4.3.0`) теперь включает три полноценных кодинг-агента
> — Claude Code, Gemini CLI, OpenCode — со своими sub-agents, skills,
> MCP-серверами и file-tools. Whilly worker внутри контейнера может зайти
> в любой из них через единый adapter без правки кода. Раньше нужно было
> либо ставить агента наружу и пробрасывать как volume, либо собирать
> custom image — теперь pull-and-go.

### Added

- **Agentic CLI mode (`--cli claude-code|opencode|gemini`).**
  `docker/cli_adapter.py` — Python adapter, который whilly-worker зовёт
  как `$CLAUDE_BIN`. Транслирует whilly-style argv (`--output-format json
  -p "<prompt>"`) в native argv каждого CLI и парсит native output в
  whilly envelope. claude-code — passthrough (whilly изначально под него
  заточен); opencode — JSONL stream → result event; gemini — single
  `{response, stats}`. Tier-aware model selection через
  `llm_resource_picker` работает для всех трёх.
- **Three agentic CLIs preinstalled в production Docker image.**
  `npm install -g @anthropic-ai/claude-code @google/gemini-cli opencode-ai`
  встроен в `Dockerfile`. Размер образа вырос с ~250 MB до ~1.5 GB,
  но pull-and-go работает с любым из трёх агентов out-of-the-box.
- **`workshop-demo.sh --cli <agent>`** — выбор agentic CLI для demo.
  Маппит на `WHILLY_CLI` env + правильный credentials env (для
  claude-code: `ANTHROPIC_API_KEY`; для opencode: `OPENROUTER_API_KEY`
  по умолчанию или `ANTHROPIC_API_KEY` / `GROQ_API_KEY`; для gemini:
  `GEMINI_API_KEY`). Опция `--cli stub` сохраняет старый default.
- **`docker/cli_adapter.py`** покрыт unit-тестами:
  `tests/unit/test_cli_adapter.py` — 24 cases (dispatch, claude
  passthrough, opencode JSONL parser, gemini single-JSON parser, argv
  compat, force-complete, model resolution).

### Changed

- **Production `Dockerfile` ставит agentic CLIs.** Если нужен slim
  образ — собирайте локально с `--build-arg INCLUDE_CLIS=false` (TODO:
  пока CLI'и встроены безусловно). Альтернатива — использовать v4.2.x.
- **Default `--cli` поведение в `workshop-demo.sh`.** Если ни `--cli`,
  ни `--llm` не заданы — используется `--cli stub` (был `--llm stub`).
  Поведение идентично, но семантика правильнее: stub — это «не agentic
  CLI», а не «не raw LLM».
- **DEMO.md** дополнен секцией «Agentic CLI workflow» с описанием
  sub-agents, skills, MCP, volume-mount'ов для конфигов
  `~/.claude/`, `.opencode/`, `.gemini/`. Сравнительная таблица CLI и
  feature-matrix (provider lock, sub-agents, skills, MCP, file-tools,
  free path).

### Documentation

- README не трогали (whilly orchestrator API не изменился).
- Все примеры из CHANGELOG протестированы вручную:
  `docker run -e WHILLY_CLI=claude-code mshegolev/whilly:4.3.0 …`
  корректно стартует и упирается в `Not logged in` без `ANTHROPIC_API_KEY`
  (а с ним — отрабатывает реальный agentic workflow).

## [4.2.1] - 2026-04-30

> **Hotfix for v4.2.0 Docker images.** The `mshegolev/whilly:4.2.0`
> (and `ghcr.io/mshegolev/whilly:4.2.0`) image crashed on `control-plane`
> startup with `create_app() missing 1 required positional argument: 'pool'`.
> Root cause: `uvicorn whilly.adapters.transport.server:create_app --factory`
> in the README and entrypoint cannot pass an asyncpg pool — uvicorn
> calls the factory with no args, but `create_app(pool, ...)` requires
> one. This release ships the production launcher and a working demo
> image. PyPI 4.2.0 was unaffected (Python source is identical).

### Fixed

- **Production Docker image: control-plane now starts.** New
  `docker/control_plane.py` opens the asyncpg pool, calls
  `create_app(pool)`, and runs `uvicorn.Server` in-process — same shape
  as `tests/integration/test_phase5_remote.py`. Both `Dockerfile` and
  `Dockerfile.demo` COPY this module and `docker/entrypoint.sh` exec's
  it for the `control-plane` role.
- **Demo image: `whilly` package importable at runtime.** Switched from
  editable pip install (whose `.pth` pointed at `/build`, which doesn't
  exist in the runtime stage) to non-editable install. Added explicit
  COPY of `docker/`, `examples/`, `alembic.prod.ini`, and
  `tests/fixtures/fake_claude*.sh` to runtime stage.
- **Demo image: `alembic upgrade head` finds migrations.** Set
  `ALEMBIC_CONFIG=/opt/whilly/alembic.ini` and use the production
  variant of `alembic.ini` (absolute migrations path inside venv) instead
  of the source-checkout-relative one.
- **`workshop-demo.sh`: workers start before plan import.** Bringing
  workers up first means both replicas are long-polling `/tasks/claim`
  when tasks land — `FOR UPDATE SKIP LOCKED` then distributes them
  cleanly across workers (the v4 distributed-claim contract).
- **`workshop-demo.sh`: events SELECT uses `created_at`.** Was
  referencing a non-existent `ts` column.
- **`docker-compose.demo.yml`: dropped `./examples` volume mount.** On
  Colima/Docker-Desktop-on-macOS the host path isn't shared with the
  VM by default, leaving the directory empty inside the container.
  Files come from the COPY in `Dockerfile.demo` instead.

### Added

- **`docker/control_plane.py`.** Production launcher. Reads
  `WHILLY_DATABASE_URL` / `WHILLY_HOST` / `WHILLY_PORT` /
  `WHILLY_LOG_LEVEL` from the environment and owns the pool lifecycle
  (open before `create_app`, close after `server.serve()` returns).
- **`tests/fixtures/fake_claude_demo.sh`.** Demo-only Claude stub that
  sleeps `FAKE_CLAUDE_DEMO_DELAY` seconds (default 2.5s) before emitting
  the `<promise>COMPLETE</promise>` envelope. The instant
  `fake_claude.sh` next to it is preserved unchanged for unit /
  integration tests; this stub is workshop-only and lets the audience
  see the parallel-claim "money frame" before tasks finish.

## [4.2.0] - 2026-04-30

> **Docker distribution release.** Adds official multi-arch (linux/amd64 +
> linux/arm64) container images on Docker Hub (`mshegolev/whilly`) and GHCR
> (`ghcr.io/mshegolev/whilly`), a presentation-ready 2-container demo
> (compose + script + checklist + plans) and a tag-driven publish pipeline
> with SBOM + provenance attestations. No Python source changes — this is
> infrastructure-only.

### Added

- **Production Docker image.** `Dockerfile` builds a lean multi-stage image
  with `[server,worker]` extras, non-root user `whilly:1000`, tini PID 1,
  HEALTHCHECK on `/health`, OCI labels via build-args. Single image,
  multiple roles via entrypoint dispatch (`control-plane` / `worker` /
  `migrate` / `shell`).
- **Multi-arch publish pipeline.** `.github/workflows/docker-publish.yml`
  triggers on `v*.*.*` tag push and manual `workflow_dispatch`. Uses QEMU
  + buildx for `linux/amd64` + `linux/arm64`. Publishes to Docker Hub
  (`mshegolev/whilly`) and GHCR (`ghcr.io/mshegolev/whilly`) in parallel.
  Generates SBOM and provenance attestations. Syncs Docker Hub README
  from `docs/dockerhub-README.md` after a real `v*.*.*` push.
- **Demo infrastructure.** `Dockerfile.demo` + `docker-compose.demo.yml`
  + `docker/entrypoint.sh` give a 3-service stack (Postgres + control-plane
  + scalable workers) for workshops and presentations. Each worker replica
  auto-registers via bootstrap-token. `docker compose up --scale worker=N`
  brings up N parallel workers.
- **Workshop runner.** `workshop-demo.sh` is a one-command end-to-end
  driver: pre-flight → build → up → import plan → wait for parallel claim
  → audit log dump → cleanup. Flags: `--workers N`, `--skip-build`,
  `--keep-running`.
- **Demo plans.** `examples/demo/parallel.json` (2 independent tasks for
  parallel-claim demo) and `examples/demo/tasks.json` (4-task DAG).
- **Documentation.** `DEMO.md` (Russian, primary), `DEMO.en.md` (English,
  brief), `DEMO-CHECKLIST.md` (9-step parallel-2-worker checklist with
  troubleshooting matrix and slide storyboard), `docs/dockerhub-README.md`
  (Hub-specific quickstart that's auto-synced to the registry).

### Changed

- **`.gitignore`** ignores `.remember/` (local Factory CLI session state).

## [4.1.0] - 2026-04-30

> **v4.1 cleanup release.** Builds on the v4.0 distributed orchestrator with a
> pure Decision Gate, per-task TRIZ contradiction analyzer, per-worker bearer
> auth, plan-level budget guard, lifespan-managed event flusher, GitHub-issue
> Forge intake, the `whilly init` PRD pipeline port, and Claude HTTPS_PROXY
> support. The v3.x legacy CLI (`whilly/cli_legacy.py`) is removed and the
> `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` env vars are now silent no-ops.

### Added

- **TASK-104c — pure Decision Gate.** New `whilly/core/gates.py` keeps the
  gate logic dependency-free. New `whilly plan apply --strict` rejects plans
  that contain skip-flagged tasks; non-strict mode warns and continues.
  `repo.skip_task` emits `task.skipped` events scoped to the current
  `plan_id` so audit trails stay plan-local.
- **TASK-104b — per-task TRIZ analyzer.** New `whilly/core/triz.py` runs a
  TRIZ contradiction pass per task; results land in the new `events.detail
  jsonb` column. Gated by `WHILLY_TRIZ_ENABLED`; subprocess timeout 25 s;
  fail-open on missing/timeout/malformed JSON (a `triz.error` event with
  `detail.reason="timeout"` is still emitted on timeout per the validation
  contract).
- **TASK-101 — per-worker bearer auth.** Migration `004_per_worker_bearer`
  makes `workers.token_hash` nullable and adds a partial UNIQUE index on
  non-NULL values. Deprecates the global `WHILLY_WORKER_TOKEN` (one-shot
  log warning, suppress with `WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`).
  New `whilly worker register` CLI mints per-worker tokens; bearer-token
  identity is now bound to the request `worker_id` (cross-worker mismatch
  → 403). `POST /workers/register` stays bootstrap-gated by
  `WHILLY_WORKER_BOOTSTRAP_TOKEN`.
- **TASK-102 — plan budget guard.** Migration `005_plan_budget` adds
  `plans.budget_usd` / `plans.spent_usd`, makes `events.plan_id NOT NULL`,
  and relaxes `events.task_id` to nullable for plan-scoped sentinels. New
  `whilly plan create --budget USD` flag. Atomic spend accumulator via
  `_INCREMENT_SPEND_SQL` with `FOR UPDATE OF t SKIP LOCKED`. A
  `plan.budget_exceeded` sentinel is emitted exactly once per crossing
  with payload `{plan_id, budget_usd, spent_usd, crossing_task_id, reason:
  "budget_threshold", threshold_pct: 100}`.
- **TASK-106 — lifespan-managed event flusher.** New
  `whilly/api/event_flusher.py` runs as a FastAPI lifespan task. Bounded
  `asyncio.Queue`, flushes on (100 ms timer OR 500-row threshold)
  whichever-first via an `asyncio.Event` wake. Checkpoint persistence uses
  tempfile + `os.replace` for atomicity; SIGTERM/SIGINT trigger a graceful
  drain.
- **TASK-108a — GitHub-issue Forge intake.** Migrations `006_plan_github_ref`
  (`plans.github_issue_ref text NULL` + partial UNIQUE) and
  `007_plan_prd_file` (`plans.prd_file text NULL`). New
  `whilly forge intake owner/repo/N` subcommand shells out to `gh` via
  `gh_subprocess_env()`. Idempotent re-run via the partial UNIQUE; concurrent
  intake is at-most-once `gh issue edit` via creator-vs-loser flag. A
  `plan.created` event is emitted with payload `{github_issue_ref, name,
  tasks_count, prd_file}`. Label transitions `whilly-pending` →
  `whilly-in-progress`. `GET /api/v1/plans/{id}` now exposes
  `github_issue_ref` and `prd_file`.
- **Cross-area events.** A `task.created` event is emitted per inserted
  task row, and a `plan.applied` event is emitted per `whilly plan apply`
  invocation with `{tasks_count, skipped_count, warned_count, strict}`.

### Added — Claude HTTPS_PROXY support (TASK-109)

- New env var `WHILLY_CLAUDE_PROXY_URL` — Whilly injects `HTTPS_PROXY`
  + `NO_PROXY` into the **spawned** Claude env only, never into its
  own process env. Worker-side asyncpg / control-plane httpx keep
  going direct via `NO_PROXY` (default
  `localhost,127.0.0.1,::1`, override via `WHILLY_CLAUDE_NO_PROXY`).
- Inherited shell `HTTPS_PROXY` is honoured as a fallback so the
  existing `claudeproxy` shell-function flow keeps working without
  setting a new env var.
- New CLI flags on `whilly init`: `--claude-proxy URL` (override env
  for one run) and `--no-claude-proxy` (force-disable, opt-out).
  Mutually exclusive at argparse level.
- Pre-flight TCP probe runs once on startup if proxy is active —
  surfaces "tunnel not up" as a sub-second exit with the actionable
  `ssh -fN -L PORT:127.0.0.1:8888 host` hint instead of letting
  Claude time out 5+ minutes deep in its HTTPS client. Opt-out via
  `WHILLY_CLAUDE_PROXY_PROBE=0` for proxies that reject bare TCP
  probes.
- See [`docs/Whilly-Claude-Proxy-Guide.md`](docs/Whilly-Claude-Proxy-Guide.md)
  for SSH-tunnel setup, systemd unit, and troubleshooting.

### Added — `whilly init` subcommand (TASK-104a)

- New CLI subcommand `whilly init "<idea>"` that combines the v3
  PRD-wizard flow with v4 Postgres-backed plan storage. Produces
  `docs/PRD-<slug>.md` via Claude (interactive in TTY, single-shot
  outside) and imports the resulting task plan straight into Postgres
  — no `tasks.json` ever materialised on disk. See
  [`docs/Whilly-Init-Guide.md`](docs/Whilly-Init-Guide.md) for the
  full surface.
- Flags: `--slug` (explicit plan_id), `--interactive` /
  `--headless` (force mode override), `--no-import` (write PRD only),
  `--force` (overwrite existing PRD), `--model`, `--output-dir`.
  TTY detection picks the default mode via `sys.stdin.isatty()`.
- Exit-code contract: `0` success, `1` user error (validation, wizard
  failure, plan-import failure), `2` env error
  (`WHILLY_DATABASE_URL` unset), `130` `KeyboardInterrupt`.
- New `whilly.prd_generator.generate_tasks_dict(prd_path, plan_id,
  model)` — in-memory counterpart to `generate_tasks` for the v4
  flow. Existing `generate_tasks` (v3 file-based) keeps working
  unchanged.
- New `whilly.adapters.filesystem.plan_io.parse_plan_dict(payload,
  plan_id)` — in-memory counterpart to `parse_plan`. Reuses the
  existing private `_plan_from_dict` validation helper.
- `whilly.prd_generator._call_claude` now reads `CLAUDE_BIN` from
  env (default `"claude"`) — same override pattern that
  `whilly.adapters.runner.claude_cli` already used. Lets integration
  tests substitute a deterministic stub
  (`tests/fixtures/fake_claude_prd.sh`) without monkeypatching.
- `whilly.prd_generator.generate_prd` accepts an opt-in `slug`
  keyword. Default (`None`) preserves the v3 auto-derivation path
  so the legacy `whilly --prd-wizard` flow stays unchanged.

### Removed

- **TASK-107 — v3 legacy CLI removed.** `whilly/cli_legacy.py` is gone, one
  release after the v4.0 deprecation window. The `WHILLY_WORKTREE` and
  `WHILLY_USE_WORKSPACE` env vars are now silent no-ops (kept for backward
  compatibility with shell wrappers that set them unconditionally).

### Migration chain

- Final state after v4.1: `001 → 002 → 003_events_detail → 004_per_worker_bearer
  → 005_plan_budget → 006_plan_github_ref → 007_plan_prd_file`.

### Quality

- 47 new unit tests in `tests/unit/test_cli_init.py` covering FR-1..FR-8
  of [`docs/PRD-v41-prd-wizard-port.md`](docs/PRD-v41-prd-wizard-port.md).
- 8 new unit tests for `parse_plan_dict` in
  `tests/unit/test_plan_io.py`.
- 12 new unit tests for `generate_tasks_dict` in
  `tests/unit/test_prd_generator_dict.py` (first time
  `prd_generator.py` has any unit tests).
- 3 new integration tests in `tests/integration/test_init_e2e.py`
  driving `python -m whilly.cli init` as a subprocess against
  testcontainers Postgres + the deterministic Claude stub.
- Suite-wide: 1530+ tests passing; `mypy --strict whilly/core/` clean;
  `ruff check`, `ruff format --check`, and `lint-imports` all green.
  CI parity enforced by `pip install -e '.[dev]'` at session start.

## [4.0.0] - 2026-04-29

> **v4.0 is a big-bang rewrite.** The single-process Ralph-Wiggum-style loop
> from v3.x has been replaced by a distributed orchestrator: a Postgres-backed
> task queue, a FastAPI control plane, and remote workers that talk to it
> over HTTP. There is **no backwards compatibility** with v3.x runtime state —
> see [`docs/Whilly-v4-Migration-from-v3.md`](docs/Whilly-v4-Migration-from-v3.md)
> for the migration path. The v3.x line stays available at tag
> [`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final)
> for teams that need it.

### Added

- **Hexagonal architecture** (PRD TC-8 / SC-6). New top-level layout:
  `whilly/core/` (pure domain — zero external deps), `whilly/adapters/`
  (db / transport / runner / filesystem), `whilly/cli/` (v4 sub-CLI),
  `whilly/worker/` (local + remote loops). The `.importlinter`
  core-purity contract enforces the boundary: `whilly.core` cannot
  import asyncpg, httpx, fastapi, subprocess, uvicorn, or alembic — CI
  fails on regression.
- **Postgres-backed task queue** with optimistic locking, `SKIP LOCKED`
  claim, visibility-timeout sweep, heartbeat-driven offline detection.
  Schema in `whilly/adapters/db/schema.sql`; migrations under
  `whilly/adapters/db/migrations/` driven by Alembic.
- **Remote-worker HTTP protocol** (PRD FR-1.x). Endpoints:
  `POST /workers/register`, `POST /workers/{id}/heartbeat`,
  `POST /tasks/claim` (long-polled), `POST /tasks/{id}/complete`,
  `POST /tasks/{id}/fail`, `POST /tasks/{id}/release`. Bearer + bootstrap
  token auth split. Full spec in
  [`docs/Whilly-v4-Worker-Protocol.md`](docs/Whilly-v4-Worker-Protocol.md).
- **`whilly-worker` console script** — standalone remote-worker entry
  point. Two equivalent install routes: `pip install whilly-orchestrator[worker]`
  (httpx-only extras) or `pip install whilly-worker` (meta-package).
- **New CLI surface**: `whilly plan {import,export,show}`, `whilly run`
  (local worker composition root), `whilly dashboard` (Rich Live TUI
  over the tasks table).
- **End-to-end gates** for each PRD success criterion:
  `tests/integration/test_phase{1..6}*.py` plus
  `tests/integration/test_release_smoke.py`.
- **Operator-facing demo**: `docs/demo-remote-worker.sh` reproduces SC-3
  on a single host (control plane + remote worker over loopback HTTP).

### Changed (BREAKING)

- **`requires-python = ">=3.12"`** (was `>=3.10`). v4 uses
  `asyncio.TaskGroup` and `@override` from `typing` which need 3.12+.
  3.10/3.11 cells were removed from the CI matrix.
- **Plan storage moved off disk into Postgres.** `tasks.json` is now an
  *import format*, not the runtime source of truth. v3.x state files
  (`.whilly_state.json`, `.whilly_workspaces/`) are not read by v4 —
  re-import via `whilly plan import path/to/tasks.json`.
- **Dependency closure split into extras.** `pip install whilly-orchestrator`
  no longer pulls every backend; pick `[worker]` (httpx) or `[server]`
  (asyncpg + fastapi + uvicorn + alembic + sqlalchemy) or `[all]`
  based on deployment shape.
- **State machine: `(COMPLETE, CLAIMED) → DONE` is now a valid edge**
  (was rejected in v3). Required for the remote-worker shape since
  the HTTP transport doesn't expose `/tasks/{id}/start` — see
  `whilly/core/state_machine.py` docstring.
- **Removed surface**: tmux runner, plan-level workspace, per-task
  worktrees, `--workspace` / `--worktree` flags, interactive menu.
  Legacy v3 CLI lives in `whilly/cli_legacy.py` for one release cycle
  and will be removed in a v4.1+ follow-up.

### Quality gates

- **`mypy --strict whilly/core/`** — pure domain layer is strictly
  typed; CI fails on any new untyped def. Currently green: 5 source
  files, 0 issues.
- **`coverage report --include='whilly/core/*' --fail-under=80`** —
  coverage gate live in CI; actual coverage 100% (233 stmts, 74
  branches, 0 misses).
- **`lint-imports`** — import-linter contract `core-purity` blocks
  `whilly.core` from importing I/O / transport modules.
- **`grep -rnE '\bos\.(chdir|getcwd)\b' whilly/core/`** — CI grep step
  catches stdlib I/O regressions that import-linter can't see.

See also: [`docs/Whilly-v4-Architecture.md`](docs/Whilly-v4-Architecture.md),
[`docs/Whilly-v4-Migration-from-v3.md`](docs/Whilly-v4-Migration-from-v3.md),
[`docs/Whilly-v4-Worker-Protocol.md`](docs/Whilly-v4-Worker-Protocol.md),
[`docs/v4.0-release-checklist.md`](docs/v4.0-release-checklist.md).

## [3.3.0] - 2026-04-24

### Changed (BREAKING)
- **Plan-level git worktree workspace is OFF by default.** Previously `USE_WORKSPACE` defaulted to `true`, so every `run_plan` invocation created/entered `.whilly_workspaces/{slug}/` via `git worktree add`. In the real-world pilot flows (pending-change-heavy repos, subprocess pipelines with absolute paths into `.venv`) this more often surprised users than it protected them. New default is `false` — agents run in cwd. Opt back in with `whilly --workspace` (alias `--worktree`) or `WHILLY_USE_WORKSPACE=1` / `USE_WORKSPACE = true` in `whilly.toml`. `--no-workspace` / `--no-worktree` are retained as no-ops for backward compatibility. Docs: `README.md`, `docs/Whilly-Usage.md`, `docs/Getting-Started.md`, `CLAUDE.md` all refreshed.

## [3.2.2] - 2026-04-24

### Added
- `whilly doctor` now detects **ghost plans** — task-plan JSONs referenced by state/history that no longer exist on disk (or point outside the repo). Surfaced as a dedicated diagnostic row so a stale `.whilly_state.json` can't silently re-point the orchestrator at a deleted plan ([#209](https://github.com/mshegolev/whilly-orchestrator/pull/209), [`a6ac28d`](https://github.com/mshegolev/whilly-orchestrator/commit/a6ac28d)).

### Fixed
- Interactive menu: `n` hotkey (new plan / PRD wizard entry) was swallowed by the Rich Live layer in some terminals; now routed through the same keybind dispatcher as `q`/`p`/`d`/`l`/`t`/`h` ([#209](https://github.com/mshegolev/whilly-orchestrator/pull/209), [`a6ac28d`](https://github.com/mshegolev/whilly-orchestrator/commit/a6ac28d)).

### Chore
- `.gitignore` now excludes `.claude/` so per-machine Claude Code project state (settings, memory, transcripts) never leaks into PRs. Sync-state manifests refreshed from the 2026-04-23 sync run ([#211](https://github.com/mshegolev/whilly-orchestrator/pull/211), [`757c0fd`](https://github.com/mshegolev/whilly-orchestrator/commit/757c0fd)).

## [3.2.1] - 2026-04-22

### Fixed
- `scripts/whilly-auto.sh` now parses the PR URL out of `gh pr create` stdout with a strict regex instead of capturing the whole output — fixes spurious `gh pr merge failed 3` when `gh` prepends a `Warning: 1 uncommitted change` line, which was causing the retry loop to close an otherwise-clean PR. Post-mortem walkthrough: [POSTMORTEM-PR-204.md](docs/workshop/POSTMORTEM-PR-204.md) (fixed in [`feb02b2`](https://github.com/mshegolev/whilly-orchestrator/commit/feb02b2), observed as [PR #204 closed](https://github.com/mshegolev/whilly-orchestrator/pull/204) → [PR #205 merged](https://github.com/mshegolev/whilly-orchestrator/pull/205)).
- `scripts/whilly-auto.sh` no longer passes `--delete-branch` to `gh pr merge`; the branch is removed in a separate cleanup step after a successful merge so that merge and cleanup exit codes can be attributed independently ([`4409ac7`](https://github.com/mshegolev/whilly-orchestrator/commit/4409ac7)).
- `scripts/whilly-auto.sh` stays checked out on `$BASE_BRANCH` after the pre-flight `git fetch + pull --ff-only` so that the subsequent workspace worktree is rooted at the latest origin commit — previously, a leftover detached HEAD from an earlier iteration could produce a worktree branched from a stale SHA ([`41cc7f9`](https://github.com/mshegolev/whilly-orchestrator/commit/41cc7f9)).
- Post-merge Projects v2 card move falls back to a plain `gh project-item-edit` call using the `gh` CLI's token when the GraphQL mutation fails with "Resource not accessible by personal access token" — prevents the card from getting stuck in *In Review* when the primary PAT lacks `projects:write` but `gh` itself is authenticated via device flow ([`feb02b2`](https://github.com/mshegolev/whilly-orchestrator/commit/feb02b2), [`d5237aa`](https://github.com/mshegolev/whilly-orchestrator/commit/d5237aa)).
- Post-merge step now explicitly calls `gh issue close` after the PR merges, instead of relying on GitHub's automatic "Closes #N" detection, which was silently failing when the PR body's `Closes #` reference was rewritten during squash-merge ([`d5237aa`](https://github.com/mshegolev/whilly-orchestrator/commit/d5237aa)).

### Added
- `scripts/whilly-auto-loop.sh` — bounded retry loop wrapping `whilly-auto-reset.sh` + `whilly-auto.sh`. `MAX_ATTEMPTS` (default `10`) and `BACKOFF_SEC` (default `30`) caps. Each iteration writes a timestamped log under `whilly-auto-runs/iter-N-<ts>.log` ([`b5adee4`](https://github.com/mshegolev/whilly-orchestrator/commit/b5adee4), [`65e95e0`](https://github.com/mshegolev/whilly-orchestrator/commit/65e95e0)).
- `scripts/whilly-proxy-preflight.sh` — checks the Claude CLI proxy is reachable before `whilly-auto.sh` kicks off, so a dead proxy fails fast at the start of an iteration instead of midway through a 6-minute agent run ([`65e95e0`](https://github.com/mshegolev/whilly-orchestrator/commit/65e95e0)).
- `docs/workshop/POSTMORTEM-PR-204.md` — reproducible case study of the self-healing retry loop recovering from the PR-URL parse bug, wired into `docs/workshop/INDEX.md` as reading-order row 8.

### Notes
- This is a **self-healed release**: the `whilly-auto.sh` bug fixed here was discovered and worked-around in-flight by the retry loop introduced in the same release. PR #204 (closed) and PR #205 (merged) document the exact handoff. Preserved closed PRs are the primary evidence for this post-mortem — do not purge them.
- No changes to the `whilly/` Python package contents. Version bumped for release discipline: the shell scripts shipped with `whilly-orchestrator` are part of the distribution surface.

## [3.2.0] - 2026-04-22

### Added
- **Layered config** (`whilly.toml` + OS keyring) with `whilly --config {show,path,edit,migrate}`. Five-layer precedence *defaults < user TOML < repo TOML < .env < shell env < CLI flags*. Secrets live in the OS keyring and never hit disk plaintext (PRs #177, #178, #179, #180, #181, #195).
- **Jira full lifecycle** — `whilly --from-jira ABC-123` source adapter and `JiraBoardClient` that drives Jira transitions in lock-step with task status. stdlib only, no `requests` dependency (#191, #192).
- **GitHub Projects v2 live sync** — cards move `Todo → In Progress → In Review → Done` as tasks run; `whilly --ensure-board-statuses` creates any missing columns; post-merge hook lands cards in Done (#183, #184, #190, #192).
- **`claude_handoff` backend** — delegate any task to an interactive Claude Code session or human operator via file-based RPC. New task statuses `blocked` and `human_loop` with matching board columns (#187).
- **New CLI flags**: `--from-issue owner/repo#N`, `--from-jira ABC-123`, `--from-issues-project <url>`, `--handoff-{list,show,complete}`, `--post-merge <plan>`, `--ensure-board-statuses`, `--config {show,path,edit,migrate}` (#179, #184, #186, #190, #191).
- **Audio announcements** include the task title + classification ("Фичу: X" / "Баг: Y" / …) instead of the generic "Задача готова" (#182).
- **Cross-platform CI** — Windows and macOS runners alongside Ubuntu × 3.10/3.11/3.12 (#179).
- **Documentation site** on GitHub Pages (https://mshegolev.github.io/whilly-orchestrator/) with a step-by-step `Getting-Started` walkthrough and fully annotated `whilly.example.toml` (#193, #194, #195).
- Board bootstrap helpers: `scripts/populate_board.py`, `scripts/move_project_card.py` (#185, #190).

### Fixed
- `ExternalIntegrationManager.is_integration_available(name)` — interactive GitHub menu no longer prints "Ошибка проверки интеграций" on every invocation (#170).
- `--from-github all` now actually fetches every open issue — previously the CLI passed `None` and `generate_tasks_from_github` silently re-applied default labels (#174).
- Centralised `gh` auth env — `WHILLY_GH_TOKEN`, `WHILLY_GH_PREFER_KEYRING`, `[github].token` resolved in one place; fixes stale `GITHUB_TOKEN` shadowing keyring auth across seven subprocess call sites (#177).
- `ProjectBoardClient._load_meta` paginates `items(first: 100)` — boards with 100+ cards previously returned HTTP 400 and every live-sync transition failed (#186 drive-by).
- Log files always opened as UTF-8 — Windows cp1252 default was crashing on the Cyrillic preamble (#179 drive-by).
- `termios` / `tty` imports guarded for Windows compatibility (#179 drive-by).
- `claude_handoff` sync `run(timeout=0)` no longer enters a hot loop — `timeout=0` now means "no wait" instead of falling back to the default (#187 drive-by).

### Changed
- `WhillyConfig.from_env()` is now a thin wrapper over `load_layered()` — every existing caller transparently gets TOML support without code changes.
- `scripts/move_project_card.py` refactored to a 25-line wrapper around `ProjectBoardClient` (#183 drive-by).
- `whilly.example.toml` expanded to 36 top-level keys + 3 nested sections with Linux-man-style per-field annotations (#195).
- `.env` loader emits a one-time deprecation warning (silence with `WHILLY_SUPPRESS_DOTENV_WARNING=1`); run `whilly --config migrate` to convert existing `.env` into `whilly.toml` and push tokens into the OS keyring (#179).

### Deprecated
- `.env` support. Still functional — migrate with `whilly --config migrate`.

### Packaging
- Subpackages (`whilly.agents`, `whilly.sources`, `whilly.sinks`, `whilly.workflow`, `whilly.classifier`, `whilly.hierarchy`, `whilly.quality`) now actually ship in the wheel — previous builds silently dropped them (#173).
- New runtime dependencies: `platformdirs>=4.0`, `keyring>=24.0`, `tomli>=2.0` on Python 3.10 (stdlib `tomllib` on 3.11+).

### Tests
- 490 → **643 passing** (+153 new). Full suite runs on Linux / macOS / Windows on every PR.

## [3.1.0] - 2026-04-20

### Added
- 🛡️ **Self-Healing System** — Automatically detects, analyzes, and fixes code errors
  - Smart error detection via traceback pattern analysis  
  - Automated fixes for `NameError`, `ImportError`, `TypeError`
  - Auto-restart with exponential backoff strategy (max 3 retries)
  - Learning from historical error patterns in logs
  - Recovery suggestions for complex issues
- **New Scripts**:
  - `scripts/whilly_with_healing.py` — Self-healing wrapper with auto-restart
  - `scripts/sync_task_status.py` — Task status synchronization utility
  - `scripts/check_status_sync.py` — Status consistency monitoring
- **New Modules**:
  - `whilly/self_healing.py` — Core error analysis and auto-fix engine
  - `whilly/recovery.py` — Task status recovery and validation
- **Documentation**:
  - `docs/Self-Healing-Guide.md` — Comprehensive self-healing documentation
  - Updated README.md with self-healing features

### Fixed
- Fixed `NameError: name 'config' is not defined` in `wait_and_collect_subprocess`
- Fixed task status synchronization issues after orchestrator crashes
- Improved error handling in external task integrations

### Changed
- Enhanced README.md with self-healing system overview
- Updated project description to include self-healing capabilities
- Improved error reporting with structured analysis

### Technical Details
- Added `config` parameter to `wait_and_collect_subprocess` function signature
- Implemented pattern-based error detection using regex and AST analysis
- Created recovery mechanisms for task status inconsistencies
- Added exponential backoff retry logic with intelligent error categorization

## [3.0.0] - 2026-04-19

### Added
- Initial release of Whilly Orchestrator
- Continuous agent loop with Claude CLI integration
- Rich TUI dashboard with live progress monitoring
- Parallel execution via tmux panes and git worktrees
- Task decomposer for oversized tasks
- PRD wizard for interactive requirement generation
- TRIZ analyzer for contradiction analysis
- State store for persistent task management
- GitHub Issues and Jira integration
- Workshop kit for HackSprint1

### Features
- JSON-based task planning and execution
- Budget monitoring and cost tracking  
- Deadlock detection and recovery
- Authentication error handling
- Workspace isolation and cleanup
- External task closing automation

---

## Release Links

- [3.1.0](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3.1.0) - Self-Healing System Release
- [3.0.0](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3.0.0) - Initial Release