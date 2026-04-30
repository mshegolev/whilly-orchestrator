# Whilly Orchestrator v4.1.0 — v4.1 Cleanup Release 🧹

## What's New

**v4.1 builds on the v4.0 distributed orchestrator** (Postgres-backed task
queue + FastAPI control plane + remote workers) with seven targeted
cleanups, plus the legacy v3 CLI removal:

### 🧠 Pure Decision Gate (TASK-104c)
- New `whilly/core/gates.py` — gate logic stays in the pure domain layer.
- New `whilly plan apply --strict` rejects plans that contain skip-flagged
  tasks; non-strict mode warns and continues.
- `repo.skip_task` emits `task.skipped` events scoped to the current
  `plan_id` so audit trails stay plan-local.

### 🔬 Per-task TRIZ analyzer (TASK-104b)
- New `whilly/core/triz.py` runs a TRIZ contradiction pass per task.
- New `events.detail jsonb` column carries the analyzer payload.
- Gated by `WHILLY_TRIZ_ENABLED`; subprocess timeout 25 s; fail-open on
  missing/timeout/malformed JSON (a `triz.error` event with
  `detail.reason="timeout"` is still emitted on timeout).

### 🔑 Per-worker bearer auth (TASK-101)
- Migration `004_per_worker_bearer` — `workers.token_hash` nullable +
  partial UNIQUE on non-NULL.
- Global `WHILLY_WORKER_TOKEN` deprecated (one-shot warning, suppress
  with `WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`).
- New `whilly worker register` CLI mints per-worker tokens.
- Bearer-token identity now bound to the request `worker_id`
  (cross-worker mismatch → 403). `POST /workers/register` stays
  bootstrap-gated by `WHILLY_WORKER_BOOTSTRAP_TOKEN`.

### 💸 Plan budget guard (TASK-102)
- Migration `005_plan_budget` — `plans.budget_usd`, `plans.spent_usd`;
  `events.plan_id NOT NULL`; `events.task_id` made nullable.
- New `whilly plan create --budget USD` flag.
- Atomic spend accumulator via `_INCREMENT_SPEND_SQL` with
  `FOR UPDATE OF t SKIP LOCKED`.
- `plan.budget_exceeded` sentinel emitted exactly once per crossing with
  payload `{plan_id, budget_usd, spent_usd, crossing_task_id, reason:
  "budget_threshold", threshold_pct: 100}`.

### 🚰 Lifespan-managed event flusher (TASK-106)
- New `whilly/api/event_flusher.py` runs as a FastAPI lifespan task.
- Bounded `asyncio.Queue`; flushes on (100 ms timer OR 500-row threshold)
  whichever-first via an `asyncio.Event` wake.
- Tempfile + `os.replace` checkpoint persistence; SIGTERM/SIGINT trigger
  a graceful drain.

### 🔥 Forge intake (TASK-108a)
- Migrations `006_plan_github_ref` (`plans.github_issue_ref text NULL` +
  partial UNIQUE) and `007_plan_prd_file` (`plans.prd_file text NULL`).
- New `whilly forge intake owner/repo/N` shells out to `gh` via
  `gh_subprocess_env()`.
- Idempotent re-run via the partial UNIQUE; concurrent intake at-most-once
  `gh issue edit` via creator-vs-loser flag.
- `plan.created` event with payload `{github_issue_ref, name, tasks_count,
  prd_file}`.
- Label transition `whilly-pending` → `whilly-in-progress`.
- `GET /api/v1/plans/{id}` exposes `github_issue_ref` + `prd_file`.

### 🪝 `whilly init` PRD pipeline (TASK-104a)
- New `whilly init "<idea>"` combines the v3 PRD-wizard flow with v4
  Postgres-backed plan storage. Produces `docs/PRD-<slug>.md` via Claude
  (interactive in TTY, single-shot outside) and imports the resulting
  task plan straight into Postgres — no `tasks.json` materialised on disk.
- Flags: `--slug`, `--interactive` / `--headless`, `--no-import`,
  `--force`, `--model`, `--output-dir`.

### 🌐 Claude HTTPS_PROXY support (TASK-109)
- New `WHILLY_CLAUDE_PROXY_URL` env var injects `HTTPS_PROXY` + `NO_PROXY`
  into the **spawned** Claude env only, never into Whilly's own process
  env. New `--claude-proxy URL` and `--no-claude-proxy` flags on
  `whilly init`. Pre-flight TCP probe surfaces "tunnel not up" sub-second
  with an actionable hint.

### 🧹 Removed (TASK-107)
- `whilly/cli_legacy.py` removed (one release after v4.0 deprecation).
- `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` env vars are silent no-ops.

### 🧱 Cross-area events
- `task.created` event per inserted task row.
- `plan.applied` event per `whilly plan apply` invocation with payload
  `{tasks_count, skipped_count, warned_count, strict}`.

### 🗄️ Migration chain (final state)
`001 → 002 → 003_events_detail → 004_per_worker_bearer →
005_plan_budget → 006_plan_github_ref → 007_plan_prd_file`

## Quick Start

```bash
# Install / upgrade (pick your shape)
pip install --upgrade whilly-orchestrator              # base + CLI
pip install --upgrade 'whilly-orchestrator[server]'    # control plane
pip install --upgrade 'whilly-orchestrator[worker]'    # remote worker
pip install --upgrade 'whilly-orchestrator[all]'       # both shapes

# Bootstrap + apply a plan
whilly plan apply --strict path/to/tasks.json

# GitHub-issue intake
whilly forge intake owner/repo/123

# PRD pipeline (TTY-aware Claude wizard)
whilly init "your idea here"
```

## Internal Quality

- 1530+ tests passing.
- `mypy --strict whilly/core/` clean.
- `ruff check whilly tests` + `ruff format --check whilly tests` clean.
- `lint-imports` green (`whilly.core` purity contract enforced).
- CI parity enforced via `pip install -e '.[dev]'` at session start.

## Migration from 4.0.x

No runtime migration required beyond running `alembic upgrade head` to apply
migrations 003 → 007. Existing v4.0 plans, workers, and events keep working.

If you set `WHILLY_WORKER_TOKEN` globally, you'll see a one-shot deprecation
warning — switch to per-worker tokens via `whilly worker register`, or
suppress the warning with `WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1` while
you migrate.

If you set `WHILLY_WORKTREE` or `WHILLY_USE_WORKSPACE`, those vars are now
silent no-ops — leaving them set is harmless.

---

*Whilly Orchestrator — Ralph Wiggum's smarter brother, now with a pure
decision gate, TRIZ contradiction analysis, per-worker bearer auth, plan
budget guard, lifespan-managed event flusher, and GitHub-issue Forge intake.*
