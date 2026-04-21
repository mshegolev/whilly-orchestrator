#!/usr/bin/env bash
# whilly-auto-loop.sh — autonomous retry loop for whilly-auto.sh.
#
# Flow (per iteration):
#   1. whilly-auto-reset.sh <issue>      # scrub workspace, reset board card
#   2. whilly-auto.sh                    # run the full pipeline
#   3. On success: exit 0 (issue merged to main, card in Done)
#   4. On failure: capture log, inspect, wait $BACKOFF_SEC, try again
#
# Usage:
#   scripts/whilly-auto-loop.sh                   # first whilly:ready issue, 10 tries
#   scripts/whilly-auto-loop.sh 159               # specific issue
#   MAX_ATTEMPTS=3 scripts/whilly-auto-loop.sh    # cap retries
#   BACKOFF_SEC=60 scripts/whilly-auto-loop.sh    # delay between attempts
#
# Environment:
#   MAX_ATTEMPTS   — cap on retries (default: 10)
#   BACKOFF_SEC    — seconds to wait after a failure before retrying (default: 30)
#   LOG_DIR        — where iteration logs land (default: whilly-auto-runs/)
#
# Exit codes:
#   0  one of the attempts succeeded
#   2  all attempts exhausted without success

set -uo pipefail

MAX_ATTEMPTS="${MAX_ATTEMPTS:-10}"
BACKOFF_SEC="${BACKOFF_SEC:-30}"
LOG_DIR="${LOG_DIR:-whilly-auto-runs}"
TARGET_ISSUE="${1:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESET="${SCRIPT_DIR}/whilly-auto-reset.sh"
AUTO="${SCRIPT_DIR}/whilly-auto.sh"

mkdir -p "$LOG_DIR"

for i in $(seq 1 "$MAX_ATTEMPTS"); do
    ts=$(date -u +'%Y-%m-%dT%H-%M-%SZ')
    log="${LOG_DIR}/iter-${i}-${ts}.log"

    echo "============================================================"
    echo "  Attempt $i/$MAX_ATTEMPTS  ·  $(date -u +%FT%TZ)  ·  log: $log"
    echo "============================================================"

    # Reset state (never fatal — log warnings and continue)
    echo "== reset ==" | tee -a "$log"
    if [[ -n "$TARGET_ISSUE" ]]; then
        "$RESET" "$TARGET_ISSUE" >>"$log" 2>&1 || echo "reset warning (non-fatal)" | tee -a "$log"
    else
        "$RESET" >>"$log" 2>&1 || echo "reset warning (non-fatal)" | tee -a "$log"
    fi

    # Run the pipeline
    echo "== whilly-auto ==" | tee -a "$log"
    if "$AUTO" >>"$log" 2>&1; then
        echo
        echo "✓ SUCCESS on attempt $i — see $log"
        grep -E "^(→|✓)" "$log" | tail -20
        exit 0
    fi

    rc=$?
    echo
    echo "✗ attempt $i failed (exit $rc)"
    echo "== failure context (last 40 lines of $log) =="
    tail -40 "$log"
    echo

    if (( i < MAX_ATTEMPTS )); then
        echo "waiting ${BACKOFF_SEC}s before retry..."
        sleep "$BACKOFF_SEC"
    fi
done

echo
echo "✗ all $MAX_ATTEMPTS attempts exhausted — logs in $LOG_DIR"
exit 2
