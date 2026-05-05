#!/usr/bin/env bash
# scripts/v6-baseline-vps-doctor.sh — pre-flight VPS doctor for the
# user-testing-validator-v6-baseline. Idempotently verifies and (if
# needed) brings up the v6-baseline VPS stack on
# root@213.159.6.155:23422, then writes machine-readable state for the
# validator to consume.
#
# What it does, in order:
#   1. Verify SSH reach to the VPS (BatchMode, ConnectTimeout=15).
#   2. Detect current stack state (running / down / partial) by
#      inspecting `docker ps --filter label=whilly=v6-baseline`
#      together with whilly-cp-postgres / whilly-cp-control-plane /
#      whilly-cp-funnel container health. "Partial" → treated as
#      "down" so the bringup path runs.
#   3. If stack is down/partial AND --no-bringup is NOT set, invoke
#      `scripts/v6-baseline-vps-up.sh --skip-smoke --skip-sync` to
#      bring the topology up idempotently. With --no-bringup, exit
#      non-zero with a clear single-line stderr.
#   4. Resolve the stable public URL from `LHR_HOSTNAME`
#      (default `whilly-orchestrator.lhr.rocks` — the paid
#      localhost.run plan pins this). For backwards-compat with v5.0
#      readers we still query postgres `funnel_url` to surface the
#      `updated_at` timestamp, which on the paid plan tracks
#      "last reconnect" rather than "URL changed at" — the URL
#      itself does NOT rotate.
#   5. Probe `<lhr_url>/health` 3 times within 15 seconds; declare
#      ready only if all 3 return HTTP 200 (tightened stability
#      window vs. the v5.0 free-tier single-shot probe).
#   6. Probe `<lhr_url>/metrics` with the WHILLY_METRICS_TOKEN bearer
#      discovered from the running whilly-cp-control-plane container
#      env. Verifies auth still gates correctly:
#        * if token configured: with-bearer → 200, without-bearer → 401
#        * if token absent:     without-bearer → 401 (fail-closed)
#   7. Inspect the off-limits openclaw-gateway container (read-only —
#      the doctor MUST NOT touch it). Status is recorded as
#      running|stopped|absent.
#   8. Write evidence to
#      $EVIDENCE_DIR/<timestamp>/state.json with fields:
#        ssh_ok, stack_state, lhr_url, lhr_url_age_seconds,
#        health_ok, health_response, metrics_ok,
#        control_plane_image_tag, openclaw_gateway_status
#   9. Exit 0 if every _ok field is true; non-zero with a single-line
#      stderr message per failed check.
#
# Idempotent: re-running over an already-healthy stack short-circuits
# the bringup path and reports the fresh lhr_url.
#
# Per the 2026-05-02 pivot, the deprecated out-of-band tunnel path is
# REMOVED — public exposure is via the localhost.run funnel sidecar
# only; no out-of-band binary, env, or domain is referenced here.
#
# Required env (defaults shown):
#   VPS_HOST=root@213.159.6.155
#   VPS_PORT=23422
#   VPS_DIR=/root/whilly
#   EVIDENCE_DIR=out/v6-baseline-vps-doctor
#
# Optional flags:
#   --json                      print state.json (single-line minified)
#                               to stdout in addition to writing it.
#   --no-bringup                skip step 3; exit non-zero if stack is
#                               down/partial.
#   --evidence-dir <path>       override the evidence directory root.
#   --help, -h                  print this docblock and exit 0.
#
# Exit codes:
#   0 — every check green.
#   1 — operator-level failure (ssh / health / metrics / lhr URL).
#   2 — environment misuse (missing tool, unknown flag, unwritable
#       evidence dir).
#   3 — stack down and --no-bringup set.
#
# Maintained alongside `tests/integration/test_v6_baseline_vps_doctor.py`.

set -euo pipefail

VPS_HOST="${VPS_HOST:-root@213.159.6.155}"
VPS_PORT="${VPS_PORT:-23422}"
VPS_DIR="${VPS_DIR:-/root/whilly}"
LHR_HOSTNAME="${LHR_HOSTNAME:-whilly-orchestrator.lhr.rocks}"
EVIDENCE_DIR="${EVIDENCE_DIR:-out/v6-baseline-vps-doctor}"
JSON_MODE=0
NO_BRINGUP=0
HEALTH_PROBE_COUNT=3
HEALTH_PROBE_WINDOW_SECONDS=15

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) JSON_MODE=1; shift ;;
        --no-bringup) NO_BRINGUP=1; shift ;;
        --evidence-dir)
            if [[ $# -lt 2 ]]; then
                echo "v6-baseline-vps-doctor.sh: --evidence-dir requires a value" >&2
                exit 2
            fi
            EVIDENCE_DIR="$2"; shift 2 ;;
        --help|-h)
            awk '/^set -euo/{exit} NR>1 {sub(/^# ?/, ""); print}' "$0"
            exit 0
            ;;
        *)
            echo "v6-baseline-vps-doctor.sh: unknown flag $1" >&2
            exit 2
            ;;
    esac
done

for tool in ssh curl python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "v6-baseline-vps-doctor.sh: missing required tool: $tool" >&2
        exit 2
    fi
done

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$EVIDENCE_DIR/$TIMESTAMP"
mkdir -p "$RUN_DIR"
if [[ ! -w "$RUN_DIR" ]]; then
    echo "v6-baseline-vps-doctor.sh: evidence dir $RUN_DIR is not writable" >&2
    exit 2
fi
STATE_FILE="$RUN_DIR/state.json"
LOG_FILE="$RUN_DIR/run.log"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UP_SCRIPT="$REPO_ROOT/scripts/v6-baseline-vps-up.sh"

ssh_run() {
    ssh -p "$VPS_PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=15 "$VPS_HOST" "$@"
}

log() {
    printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >>"$LOG_FILE"
}

ssh_ok=false
stack_state="unknown"
lhr_url=""
lhr_url_age_seconds=-1
health_ok=false
health_response=""
metrics_ok=false
control_plane_image_tag=""
openclaw_gateway_status="unknown"
FAILURES=()

record_failure() {
    local msg="$1"
    FAILURES+=("$msg")
    echo "$msg" >&2
    log "FAIL $msg"
}

write_state() {
    python3 - "$STATE_FILE" \
        "$ssh_ok" "$stack_state" "$lhr_url" "$lhr_url_age_seconds" \
        "$health_ok" "$health_response" "$metrics_ok" \
        "$control_plane_image_tag" "$openclaw_gateway_status" \
        "$JSON_MODE" <<'PY'
import json, sys
(path, ssh_ok, stack_state, lhr_url, lhr_age, health_ok, health_response,
 metrics_ok, image_tag, openclaw_status, json_mode) = sys.argv[1:]
def b(v):
    return v == "true"
state = {
    "ssh_ok": b(ssh_ok),
    "stack_state": stack_state,
    "lhr_url": lhr_url,
    "lhr_url_age_seconds": int(lhr_age) if lhr_age.lstrip("-").isdigit() else -1,
    "health_ok": b(health_ok),
    "health_response": health_response,
    "metrics_ok": b(metrics_ok),
    "control_plane_image_tag": image_tag,
    "openclaw_gateway_status": openclaw_status,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(state, fh, indent=2, sort_keys=True)
    fh.write("\n")
if json_mode == "1":
    sys.stdout.write(json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n")
PY
}

trap 'write_state || true' EXIT

# ── 1: SSH reach ────────────────────────────────────────────────────────
log "[1/8] verifying SSH reach to $VPS_HOST:$VPS_PORT"
if ssh_run 'true' >/dev/null 2>>"$LOG_FILE"; then
    ssh_ok=true
else
    record_failure "ERR ssh: cannot reach $VPS_HOST:$VPS_PORT (BatchMode + ConnectTimeout=15s)"
    write_state
    exit 1
fi

# ── 2: detect stack state ───────────────────────────────────────────────
log "[2/8] detecting v6-baseline stack state"
PG_STATUS=$(ssh_run "docker inspect --format '{{.State.Health.Status}}' whilly-cp-postgres 2>/dev/null" 2>>"$LOG_FILE" || echo "missing")
CP_STATUS=$(ssh_run "docker inspect --format '{{.State.Health.Status}}' whilly-cp-control-plane 2>/dev/null" 2>>"$LOG_FILE" || echo "missing")
FUNNEL_RUNNING=$(ssh_run "docker inspect --format '{{.State.Running}}' whilly-cp-funnel 2>/dev/null" 2>>"$LOG_FILE" || echo "false")
PG_STATUS="${PG_STATUS:-missing}"
CP_STATUS="${CP_STATUS:-missing}"
FUNNEL_RUNNING="${FUNNEL_RUNNING:-false}"
log "  postgres=$PG_STATUS control-plane=$CP_STATUS funnel_running=$FUNNEL_RUNNING"

if [[ "$PG_STATUS" == "healthy" && "$CP_STATUS" == "healthy" && "$FUNNEL_RUNNING" == "true" ]]; then
    stack_state="running"
elif [[ "$PG_STATUS" == "missing" && "$CP_STATUS" == "missing" && "$FUNNEL_RUNNING" != "true" ]]; then
    stack_state="down"
else
    stack_state="partial"
fi
log "  stack_state=$stack_state"

# ── 3: bringup if down/partial ──────────────────────────────────────────
if [[ "$stack_state" != "running" ]]; then
    if [[ "$NO_BRINGUP" -eq 1 ]]; then
        record_failure "ERR stack: stack down and --no-bringup set"
        write_state
        exit 3
    fi
    log "[3/8] invoking $UP_SCRIPT --skip-smoke --skip-sync"
    if [[ ! -x "$UP_SCRIPT" ]]; then
        record_failure "ERR bringup: $UP_SCRIPT missing or not executable"
        write_state
        exit 1
    fi
    BRINGUP_LOG="$RUN_DIR/bringup.log"
    if EVIDENCE_DIR="$RUN_DIR/bringup-evidence" \
        bash "$UP_SCRIPT" --skip-smoke --skip-sync >>"$BRINGUP_LOG" 2>&1; then
        log "  bringup ok"
        stack_state="running"
    else
        record_failure "ERR bringup: scripts/v6-baseline-vps-up.sh --skip-smoke --skip-sync failed (see $BRINGUP_LOG)"
        write_state
        exit 1
    fi
else
    log "[3/8] skipping bringup (stack already running — idempotent no-op)"
fi

# ── 4: stable public URL (env-pinned; postgres last-reconnect age) ─────
log "[4/8] resolving stable public URL from LHR_HOSTNAME (paid-plan pinning)"
lhr_url="https://${LHR_HOSTNAME}"
URL_ROW=$(ssh_run "docker exec whilly-cp-postgres psql -U whilly -d whilly -tAF '|' -c \"SELECT url, EXTRACT(EPOCH FROM (NOW() - updated_at))::int FROM funnel_url WHERE id = 1\" 2>/dev/null" 2>>"$LOG_FILE" || true)
URL_ROW="$(printf '%s' "$URL_ROW" | tr -d '\r' | head -n1)"
if [[ -n "$URL_ROW" && "$URL_ROW" == *"|"* ]]; then
    pg_url="${URL_ROW%%|*}"
    pg_age="${URL_ROW##*|}"
    pg_url="$(printf '%s' "$pg_url" | tr -d '[:space:]')"
    pg_age="$(printf '%s' "$pg_age" | tr -d '[:space:]')"
    if [[ -n "$pg_age" && "$pg_age" =~ ^-?[0-9]+$ ]]; then
        lhr_url_age_seconds="$pg_age"
    fi
    if [[ -n "$pg_url" && "$pg_url" != "$lhr_url" ]]; then
        log "  warn: postgres funnel_url=$pg_url differs from env-pinned $lhr_url (sidecar reconnect in flight?)"
    fi
fi
log "  lhr_url=$lhr_url last_reconnect_age=${lhr_url_age_seconds}s (semantics: time-since-last-reconnect; URL is constant)"

# ── 5: /health stability window (3 probes within 15s) ─────────────────
log "[5/8] probing $lhr_url/health — require ${HEALTH_PROBE_COUNT} successes within ${HEALTH_PROBE_WINDOW_SECONDS}s"
HEALTH_BODY_FILE="$RUN_DIR/health-body.json"
HEALTH_PROBE_INTERVAL=$(( HEALTH_PROBE_WINDOW_SECONDS / HEALTH_PROBE_COUNT ))
if [[ $HEALTH_PROBE_INTERVAL -lt 1 ]]; then
    HEALTH_PROBE_INTERVAL=1
fi
HEALTH_SUCCESSES=0
HEALTH_LAST_CODE="000"
for probe in $(seq 1 "$HEALTH_PROBE_COUNT"); do
    HEALTH_LAST_CODE=$(curl -sS -o "$HEALTH_BODY_FILE" -w '%{http_code}' --max-time 15 "$lhr_url/health" 2>>"$LOG_FILE" || echo "000")
    log "  probe $probe/${HEALTH_PROBE_COUNT}: HTTP $HEALTH_LAST_CODE"
    if [[ "$HEALTH_LAST_CODE" == "200" ]]; then
        HEALTH_SUCCESSES=$(( HEALTH_SUCCESSES + 1 ))
    fi
    if [[ "$probe" -lt "$HEALTH_PROBE_COUNT" ]]; then
        sleep "$HEALTH_PROBE_INTERVAL"
    fi
done
if [[ "$HEALTH_SUCCESSES" -eq "$HEALTH_PROBE_COUNT" ]]; then
    health_ok=true
    health_response="$(tr -d '\r\n' <"$HEALTH_BODY_FILE" | head -c 512)"
    log "  stability window passed: ${HEALTH_SUCCESSES}/${HEALTH_PROBE_COUNT} probes returned 200"
else
    record_failure "ERR health: ${HEALTH_SUCCESSES}/${HEALTH_PROBE_COUNT} probes succeeded within ${HEALTH_PROBE_WINDOW_SECONDS}s window (last HTTP $HEALTH_LAST_CODE)"
    health_response="HTTP $HEALTH_LAST_CODE (${HEALTH_SUCCESSES}/${HEALTH_PROBE_COUNT})"
fi

# ── 6: /metrics probe (auth gating) ─────────────────────────────────────
log "[6/8] probing $lhr_url/metrics with bearer (auth-gating verification)"
METRICS_TOKEN=$(ssh_run "docker exec whilly-cp-control-plane sh -c 'printf %s \"\${WHILLY_METRICS_TOKEN:-}\"'" 2>>"$LOG_FILE" || true)
METRICS_TOKEN="${METRICS_TOKEN//$'\r'/}"
METRICS_TOKEN="${METRICS_TOKEN//$'\n'/}"
NOAUTH_CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$lhr_url/metrics" 2>>"$LOG_FILE" || echo "000")
log "  no-bearer HTTP=$NOAUTH_CODE token_configured=$([[ -n "$METRICS_TOKEN" ]] && echo yes || echo no)"
if [[ -n "$METRICS_TOKEN" ]]; then
    AUTH_CODE=$(curl -sS -o "$RUN_DIR/metrics-body.txt" -w '%{http_code}' --max-time 15 \
        -H "Authorization: Bearer $METRICS_TOKEN" "$lhr_url/metrics" 2>>"$LOG_FILE" || echo "000")
    log "  with-bearer HTTP=$AUTH_CODE"
    if [[ "$AUTH_CODE" == "200" && "$NOAUTH_CODE" == "401" ]]; then
        metrics_ok=true
    else
        record_failure "ERR metrics: auth-gating mismatch (with_bearer=$AUTH_CODE, no_bearer=$NOAUTH_CODE; expected 200/401)"
    fi
else
    if [[ "$NOAUTH_CODE" == "401" ]]; then
        metrics_ok=true
        log "  WHILLY_METRICS_TOKEN unset — fail-closed verified (401)"
    else
        record_failure "ERR metrics: WHILLY_METRICS_TOKEN unset but /metrics returned HTTP $NOAUTH_CODE (expected 401 fail-closed)"
    fi
fi

# ── 7: control-plane image tag ──────────────────────────────────────────
log "[7/8] reading control-plane image tag"
control_plane_image_tag=$(ssh_run "docker inspect --format '{{index .Config.Image}}' whilly-cp-control-plane 2>/dev/null" 2>>"$LOG_FILE" || true)
control_plane_image_tag="$(printf '%s' "$control_plane_image_tag" | tr -d '[:space:]')"
if [[ -z "$control_plane_image_tag" ]]; then
    record_failure "ERR image_tag: could not read control-plane image tag"
fi

# ── 8: openclaw-gateway invariant (read-only) ───────────────────────────
log "[8/8] checking openclaw-gateway container (read-only — MUST NOT be touched)"
OPENCLAW_RAW=$(ssh_run "docker inspect --format '{{.State.Running}}' openclaw-gateway 2>/dev/null" 2>>"$LOG_FILE" || true)
OPENCLAW_RAW="$(printf '%s' "$OPENCLAW_RAW" | tr -d '[:space:]')"
if [[ -z "$OPENCLAW_RAW" ]]; then
    openclaw_gateway_status="absent"
elif [[ "$OPENCLAW_RAW" == "true" ]]; then
    openclaw_gateway_status="running"
else
    openclaw_gateway_status="stopped"
fi
log "  openclaw_gateway_status=$openclaw_gateway_status"

write_state
trap - EXIT

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    log "doctor: ${#FAILURES[@]} check(s) failed"
    exit 1
fi

log "doctor: all checks green"
exit 0
