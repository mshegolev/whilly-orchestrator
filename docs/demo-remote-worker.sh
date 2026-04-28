#!/usr/bin/env bash
#
# Whilly v4.0 — SC-3 Remote-Worker Demo (TASK-024b).
#
# Reproduces the PRD Success Criterion SC-3 ("Запустить второй процесс
# whilly-worker --connect URL --token X на другой VM, он claim'ит задачу
# через HTTP, выполняет, и завершает её") on a single host with one
# Postgres + one control plane + one worker process talking over the
# loopback HTTP socket.
#
# This script is the operator-facing companion to
# tests/integration/test_phase5_remote.py: same flow, no pytest scaffold
# — meant to be `bash docs/demo-remote-worker.sh` after a fresh checkout.
# A green run end-to-end demonstrates that the v4 distributed shape
# (control plane + remote worker + Postgres) actually composes, which is
# the load-bearing claim of the v4.0 release.
#
# What this script does, top-to-bottom:
#   1. Sanity-checks required env vars + binaries (whilly-worker, psql,
#      curl, fake claude stub).
#   2. Boots a Postgres container if WHILLY_DATABASE_URL is unset (uses
#      scripts/db-up.sh from TASK-003 — the same dev-stack everything
#      else uses).
#   3. Applies Alembic migrations so the schema matches the wire shape
#      RemoteWorkerClient expects.
#   4. Seeds one PENDING task + one workers row directly via psql (the
#      worker row's FK constraint on tasks.claimed_by means we can't
#      claim without a parent in workers; whilly-worker doesn't
#      register itself, see whilly/cli/worker.py docstring).
#   5. Starts uvicorn hosting the FastAPI control plane in the
#      background; waits for /health to flip to "ok".
#   6. Spawns whilly-worker --once, points it at the loopback control
#      plane, drains one task.
#   7. Verifies via psql that tasks.status='DONE' and the events table
#      has CLAIM + COMPLETE rows.
#   8. Cleans up the uvicorn pid (and the Postgres container if we
#      booted it ourselves — preserves an existing one).
#
# Why a separate script and not just `pytest tests/integration/test_phase5_remote.py`?
#   The PRD AC for SC-3 reads "Запустить второй процесс" — i.e. the
#   demo should run on a real second VM in the limit, not inside a test
#   harness. This script is the artefact an operator copies to that VM
#   to verify their deploy works without booting Python and pytest. It
#   is also the surface tests/integration/test_release_smoke.py
#   (TASK-034a) shells out to as part of the SC-3 release gate.
#
# Required env vars (with defaults if unset):
#   WHILLY_DATABASE_URL   — Postgres DSN. If unset, this script boots a
#                           postgres:15-alpine container via
#                           scripts/db-up.sh and exports the DSN itself.
#   WHILLY_CONTROL_URL    — control-plane base URL, default
#                           http://127.0.0.1:8000.
#   WHILLY_WORKER_TOKEN   — bearer token shared between the control
#                           plane (worker_token kwarg of create_app)
#                           and whilly-worker. Default "demo-bearer".
#   WHILLY_PLAN_ID        — plan id this worker drains. Default
#                           "demo-sc3-plan".
#   CLAUDE_BIN            — path to a Claude CLI binary (or stub). The
#                           script auto-discovers tests/fixtures/fake_claude.sh
#                           if unset, so you don't need a real Claude
#                           subscription to exercise SC-3.
#
# Exit codes:
#   0 — SC-3 demonstrated end-to-end; task DONE in the database.
#   1 — Sanity check failed (missing binary, broken env).
#   2 — Control plane never came up (uvicorn boot failed).
#   3 — Worker exited non-zero or task didn't transition to DONE.
#   4 — Audit-trail check failed (missing CLAIM or COMPLETE event).
#

set -euo pipefail

# Trace each command when DEBUG=1 — invaluable for diagnosing a hung
# step on a remote VM. Intentionally off by default so the happy path
# reads cleanly.
[[ "${DEBUG:-0}" == "1" ]] && set -x

# ─── 1. Constants + env defaults ──────────────────────────────────────────

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DEFAULT_CONTROL_URL="http://127.0.0.1:8000"
readonly DEFAULT_WORKER_TOKEN="demo-bearer"
readonly DEFAULT_BOOTSTRAP_TOKEN="demo-bootstrap"
readonly DEFAULT_PLAN_ID="demo-sc3-plan"
readonly DEFAULT_TASK_ID="T-DEMO-SC3-1"
readonly DEFAULT_WORKER_ID="w-demo-sc3"

WHILLY_CONTROL_URL="${WHILLY_CONTROL_URL:-$DEFAULT_CONTROL_URL}"
WHILLY_WORKER_TOKEN="${WHILLY_WORKER_TOKEN:-$DEFAULT_WORKER_TOKEN}"
WHILLY_PLAN_ID="${WHILLY_PLAN_ID:-$DEFAULT_PLAN_ID}"

# Auto-discover fake_claude.sh if CLAUDE_BIN is unset. Operators with a
# real Claude binary on $PATH would set CLAUDE_BIN themselves; the stub
# is for SC-3 demonstration and CI where we don't want to spend tokens.
if [[ -z "${CLAUDE_BIN:-}" ]]; then
    CLAUDE_BIN="$REPO_ROOT/tests/fixtures/fake_claude.sh"
fi
export CLAUDE_BIN

# ─── 2. Sanity checks ─────────────────────────────────────────────────────

log() { printf '\033[1;34m[demo-sc3]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[demo-sc3 ERROR]\033[0m %s\n' "$*" >&2; }

require_bin() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "missing required binary: $1"
        err "install with: $2"
        exit 1
    fi
}

log "Whilly v4.0 SC-3 remote-worker demo (TASK-024b)"
log "repo root: $REPO_ROOT"

require_bin whilly-worker "pip install -e . (or pip install whilly-orchestrator[worker])"
require_bin psql           "brew install libpq && brew link --force libpq  # macOS"
require_bin curl           "(comes with the OS on virtually every Linux/macOS)"
require_bin python3        "(comes with the OS, or use pyenv install 3.12)"

if [[ ! -x "$CLAUDE_BIN" ]]; then
    err "CLAUDE_BIN=$CLAUDE_BIN is not executable; chmod +x or point at a real Claude binary"
    exit 1
fi
log "CLAUDE_BIN=$CLAUDE_BIN (executable: ok)"

# ─── 3. Postgres bootstrap (if needed) ────────────────────────────────────

WE_BOOTED_POSTGRES=0
if [[ -z "${WHILLY_DATABASE_URL:-}" ]]; then
    log "WHILLY_DATABASE_URL unset; booting local Postgres via scripts/db-up.sh"
    bash "$REPO_ROOT/scripts/db-up.sh" up >&2
    WE_BOOTED_POSTGRES=1
    # scripts/db-up.sh exposes the canonical local DSN.
    export WHILLY_DATABASE_URL="postgresql://whilly:whilly@127.0.0.1:5432/whilly"
fi
log "WHILLY_DATABASE_URL=$WHILLY_DATABASE_URL"

log "Applying Alembic migrations..."
(cd "$REPO_ROOT" && python3 -m alembic upgrade head) >&2

# ─── 4. Seed plan + task + worker row ─────────────────────────────────────

log "Seeding plan $WHILLY_PLAN_ID + task $DEFAULT_TASK_ID + worker $DEFAULT_WORKER_ID"
psql "$WHILLY_DATABASE_URL" -v ON_ERROR_STOP=1 <<SQL >&2
-- Idempotent reset so re-running the demo doesn't trip unique constraints.
DELETE FROM events WHERE task_id = '$DEFAULT_TASK_ID';
DELETE FROM tasks  WHERE id = '$DEFAULT_TASK_ID';
DELETE FROM plans  WHERE id = '$WHILLY_PLAN_ID';
DELETE FROM workers WHERE worker_id = '$DEFAULT_WORKER_ID';

INSERT INTO plans (id, name) VALUES ('$WHILLY_PLAN_ID', 'SC-3 demo plan');

INSERT INTO workers (worker_id, hostname, token_hash, status)
VALUES ('$DEFAULT_WORKER_ID', 'demo-host', 'demo-placeholder-hash', 'online');

INSERT INTO tasks (
    id, plan_id, status, priority,
    description, key_files, acceptance_criteria, test_steps,
    prd_requirement, version, created_at, updated_at
) VALUES (
    '$DEFAULT_TASK_ID', '$WHILLY_PLAN_ID', 'PENDING', 'critical',
    'SC-3 demo task — drained by whilly-worker via HTTP transport.',
    '["whilly/cli/worker.py"]'::jsonb,
    '["worker exits 0", "task ends in DONE"]'::jsonb,
    '["bash docs/demo-remote-worker.sh"]'::jsonb,
    'SC-3', 1, NOW(), NOW()
);
SQL

# ─── 5. Boot the FastAPI control plane on $WHILLY_CONTROL_URL ─────────────

CONTROL_PORT="${WHILLY_CONTROL_URL##*:}"
CONTROL_HOST="${WHILLY_CONTROL_URL#*://}"
CONTROL_HOST="${CONTROL_HOST%%:*}"

log "Starting uvicorn on $CONTROL_HOST:$CONTROL_PORT (control plane)"
UVICORN_LOG="$(mktemp -t whilly-demo-uvicorn.XXXXXX.log)"
(
    cd "$REPO_ROOT"
    WHILLY_WORKER_TOKEN="$WHILLY_WORKER_TOKEN" \
    WHILLY_WORKER_BOOTSTRAP_TOKEN="$DEFAULT_BOOTSTRAP_TOKEN" \
    python3 -m uvicorn \
        "whilly.adapters.transport.server:create_app" \
        --factory \
        --host "$CONTROL_HOST" \
        --port "$CONTROL_PORT" \
        --log-level error \
        >"$UVICORN_LOG" 2>&1
) &
UVICORN_PID=$!

# Always tear down uvicorn on exit, even if a later step fails. Bash
# traps fire on EXIT regardless of the cause, so this is the canonical
# way to avoid a stranded uvicorn process bound to port 8000 between
# demo runs.
cleanup() {
    if kill -0 "$UVICORN_PID" 2>/dev/null; then
        log "Stopping uvicorn (pid=$UVICORN_PID)"
        kill "$UVICORN_PID" 2>/dev/null || true
        wait "$UVICORN_PID" 2>/dev/null || true
    fi
    if [[ "$WE_BOOTED_POSTGRES" == "1" ]]; then
        log "Stopping Postgres container we booted"
        bash "$REPO_ROOT/scripts/db-up.sh" down >&2 || true
    fi
}
trap cleanup EXIT

# Wait for /health to come up before spawning the worker. Bounded wait
# with a clear failure message so a hung uvicorn surfaces as code 2,
# not as a confusing worker-side connection refused.
log "Waiting for control plane /health (max 30s)..."
for _ in $(seq 1 60); do
    if curl -sf "$WHILLY_CONTROL_URL/health" >/dev/null 2>&1; then
        log "Control plane is up at $WHILLY_CONTROL_URL"
        break
    fi
    sleep 0.5
done
if ! curl -sf "$WHILLY_CONTROL_URL/health" >/dev/null 2>&1; then
    err "Control plane did not respond on $WHILLY_CONTROL_URL/health within 30s"
    err "uvicorn log:"
    cat "$UVICORN_LOG" >&2 || true
    exit 2
fi

# ─── 6. Run the worker (--once) ───────────────────────────────────────────

log "Spawning whilly-worker --once on plan $WHILLY_PLAN_ID"
WORKER_LOG="$(mktemp -t whilly-demo-worker.XXXXXX.log)"
if ! WHILLY_CONTROL_URL="$WHILLY_CONTROL_URL" \
     WHILLY_WORKER_TOKEN="$WHILLY_WORKER_TOKEN" \
     WHILLY_PLAN_ID="$WHILLY_PLAN_ID" \
     WHILLY_WORKER_ID="$DEFAULT_WORKER_ID" \
     CLAUDE_BIN="$CLAUDE_BIN" \
     PYTHONUNBUFFERED=1 \
     timeout 60 whilly-worker --once >"$WORKER_LOG" 2>&1; then
    err "whilly-worker exited non-zero or timed out"
    err "worker log:"
    cat "$WORKER_LOG" >&2 || true
    err "uvicorn log:"
    cat "$UVICORN_LOG" >&2 || true
    exit 3
fi
log "whilly-worker exited 0; reported summary:"
tail -1 "$WORKER_LOG" >&2 || true

# ─── 7. Verify task DONE + audit trail ────────────────────────────────────

log "Verifying task status + events in Postgres"
TASK_STATUS=$(psql "$WHILLY_DATABASE_URL" -tAc \
    "SELECT status FROM tasks WHERE id = '$DEFAULT_TASK_ID'")
TASK_STATUS="${TASK_STATUS//[[:space:]]/}"

if [[ "$TASK_STATUS" != "DONE" ]]; then
    err "expected tasks.status='DONE' but got '$TASK_STATUS'"
    err "worker log:"
    cat "$WORKER_LOG" >&2 || true
    exit 3
fi
log "tasks.status=DONE (expected: DONE) ✓"

EVENT_TYPES=$(psql "$WHILLY_DATABASE_URL" -tAc \
    "SELECT event_type FROM events WHERE task_id = '$DEFAULT_TASK_ID' ORDER BY id")
log "events for $DEFAULT_TASK_ID:"
printf '%s\n' "$EVENT_TYPES" | sed 's/^/  - /' >&2

if ! grep -qx "CLAIM" <<<"$EVENT_TYPES"; then
    err "missing CLAIM event for $DEFAULT_TASK_ID"
    exit 4
fi
if ! grep -qx "COMPLETE" <<<"$EVENT_TYPES"; then
    err "missing COMPLETE event for $DEFAULT_TASK_ID"
    exit 4
fi

# ─── 8. Done ──────────────────────────────────────────────────────────────

log "✅ SC-3 demonstrated end-to-end:"
log "    plan=$WHILLY_PLAN_ID task=$DEFAULT_TASK_ID worker=$DEFAULT_WORKER_ID"
log "    task transitioned PENDING → DONE via HTTP transport"
log "    audit trail: CLAIM + COMPLETE present"
log ""
log "(uvicorn + Postgres will be cleaned up by the EXIT trap)"
exit 0
