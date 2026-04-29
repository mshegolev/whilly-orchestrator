#!/usr/bin/env bash
# Launch the v4.0 refactoring run via Whilly headless.
# Designed to be backgrounded and monitored via whilly_logs/whilly_events.jsonl.
#
# Usage:
#   bash scripts/run-v4-refactor.sh                  # foreground
#   nohup bash scripts/run-v4-refactor.sh > whilly_logs/run.out 2>&1 &
#
# Resume after a crash:
#   nohup bash scripts/run-v4-refactor.sh --resume > whilly_logs/run.out 2>&1 &

set -uo pipefail

cd "$(dirname "$0")/.."
mkdir -p whilly_logs

# Activate Python 3.12 venv (PRD TC-1)
# shellcheck disable=SC1091
source .venv-v4/bin/activate

# Load env (CLAUDE_BIN wrapper, budget, parallelism, workspace flags)
set -a
# shellcheck disable=SC1091
source .env-v4
set +a

PLAN=".planning/refactoring-1_tasks.json"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
echo "[$(date -u +%H:%M:%SZ)] starting whilly headless on $PLAN" | tee -a whilly_logs/run.out

# --workspace already on via WHILLY_USE_WORKSPACE=1, but pass explicitly so a
# future env-file edit cannot accidentally turn it off.
exec whilly --headless --workspace "$PLAN" "$@"
