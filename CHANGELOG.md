# Changelog

All notable changes to Whilly Orchestrator will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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