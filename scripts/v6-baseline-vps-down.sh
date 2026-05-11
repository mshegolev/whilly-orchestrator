#!/usr/bin/env bash
# scripts/v6-baseline-vps-down.sh — rollback / teardown of the v6.0-baseline
# VPS topology produced by `scripts/v6-baseline-vps-up.sh`.
#
# What it does:
#   1. SSH to the VPS, run `docker compose -f docker-compose.control-plane.yml
#      --profile funnel down` so the postgres + control-plane + funnel
#      sidecar containers stop and the docker network is removed.
#   2. Optionally remove the named pgdata volume (`--volumes`) for a fully
#      clean state — by default pgdata is preserved so a re-up keeps the
#      bootstrap-token / workers / events history.
#   3. Optionally cleanup local backup-tag references (`--prune-backup-tags`)
#      under `whilly-backup/*` (operator-facing d1 cleanup playbook).
#   4. Confirm the off-limits openclaw-gateway container is still running
#      (the down script must NEVER touch it — sanity-check at the end).
#
# Idempotent: re-running with the stack already down exits 0 with
# `compose down` reporting nothing to stop.
#
# Required env (defaults shown):
#   VPS_HOST=root@213.159.6.155
#   VPS_PORT=23422
#   VPS_DIR=/root/whilly
#   EVIDENCE_DIR=out/v6-baseline-vps-down
#
# Optional flags:
#   --volumes              also remove named pgdata + funnel-url volumes
#                          (DESTRUCTIVE — clears bootstrap tokens, events).
#   --prune-backup-tags    also `git tag -d whilly-backup/*` locally and
#                          `git push --delete origin` for each remote tag.
#                          Off by default; opt-in operator playbook.
#   --keep-images          keep `mshegolev/whilly:*` and `whilly-funnel:latest`
#                          images on the VPS even when --volumes is set.
#                          Default: keep them. Use `docker image prune` from
#                          a separate tool when disk pressure warrants it.
#
# Exit codes:
#   0 — teardown successful (or already torn down).
#   1 — teardown failed (compose down errored).
#   2 — environment misuse (missing tool / unwritable evidence dir).

set -euo pipefail

VPS_HOST="${VPS_HOST:-root@213.159.6.155}"
VPS_PORT="${VPS_PORT:-23422}"
VPS_DIR="${VPS_DIR:-/root/whilly}"
EVIDENCE_DIR="${EVIDENCE_DIR:-out/v6-baseline-vps-down}"
DO_VOLUMES=0
DO_PRUNE_TAGS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --volumes) DO_VOLUMES=1; shift ;;
        --prune-backup-tags) DO_PRUNE_TAGS=1; shift ;;
        --keep-images) shift ;;
        --help|-h)
            awk '/^set -euo/{exit} NR>1 {sub(/^# ?/, ""); print}' "$0"
            exit 0
            ;;
        *)
            echo "v6-baseline-vps-down.sh: unknown flag $1" >&2
            exit 2
            ;;
    esac
done

mkdir -p "$EVIDENCE_DIR"
if [[ ! -w "$EVIDENCE_DIR" ]]; then
    echo "v6-baseline-vps-down.sh: evidence dir $EVIDENCE_DIR is not writable" >&2
    exit 2
fi
LOG_FILE="$EVIDENCE_DIR/run.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "── v6-baseline VPS teardown ──"
echo "VPS_HOST=$VPS_HOST  VPS_PORT=$VPS_PORT  VPS_DIR=$VPS_DIR"
echo "evidence=$EVIDENCE_DIR  volumes=$DO_VOLUMES  prune_tags=$DO_PRUNE_TAGS"
echo "──────────────────────────────"

ssh_run() {
    ssh -p "$VPS_PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=15 "$VPS_HOST" "$@"
}

# ── 1: compose down ─────────────────────────────────────────────────────
echo "[1/4] docker compose down (with --profile funnel) …"
DOWN_FLAGS=""
if [[ "$DO_VOLUMES" -eq 1 ]]; then
    DOWN_FLAGS="-v"
    echo "  --volumes: pgdata + funnel-url volumes WILL be removed"
fi
ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml --profile funnel down $DOWN_FLAGS" \
    | tee "$EVIDENCE_DIR/vps-compose-down.txt"

# ── 2: confirm whilly-cp-* containers are gone ──────────────────────────
echo "[2/4] confirming no whilly-cp-* containers remain …"
REMAINING=$(ssh_run 'docker ps -a --filter name=whilly-cp- --format "{{.Names}} {{.Status}}"' || true)
if [[ -n "$REMAINING" ]]; then
    echo "  warning: residual containers found:"
    echo "$REMAINING"
    # We don't fail the script here — `docker compose down` is the source of
    # truth. Operators with stuck containers can `docker rm -f <name>` manually.
else
    echo "  none"
fi
echo "${REMAINING:-none}" > "$EVIDENCE_DIR/vps-residual-containers.txt"

# ── 3: optional local backup-tag cleanup (d1 operator playbook) ─────────
if [[ "$DO_PRUNE_TAGS" -eq 1 ]]; then
    echo "[3/4] pruning local + remote whilly-backup/* git tags …"
    # Operate on the orchestrator host (the v6.0 d1 backup tags live in the
    # operator's repo, not on the VPS). Local-first; remote prune is best-effort.
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
    cd "$REPO_ROOT"
    LOCAL_TAGS=$(git tag --list 'whilly-backup/*' 2>/dev/null || true)
    if [[ -n "$LOCAL_TAGS" ]]; then
        echo "  local tags to prune:"
        echo "$LOCAL_TAGS" | sed 's/^/    /'
        echo "$LOCAL_TAGS" | xargs -r git tag -d \
            | tee "$EVIDENCE_DIR/local-tag-delete.txt"
        # Remote prune is best-effort — operator may be offline / unauthenticated.
        if git remote get-url origin >/dev/null 2>&1; then
            echo "$LOCAL_TAGS" | sed 's|^|:|' | xargs -r git push origin \
                2>"$EVIDENCE_DIR/remote-tag-delete-stderr.txt" \
                | tee "$EVIDENCE_DIR/remote-tag-delete.txt" \
                || echo "  warn: remote tag delete had errors (see remote-tag-delete-stderr.txt)"
        fi
    else
        echo "  no whilly-backup/* tags found locally"
    fi
else
    echo "[3/4] skipping backup-tag prune (use --prune-backup-tags to enable)"
fi

# ── 4: openclaw-gateway invariant ───────────────────────────────────────
echo "[4/4] confirming openclaw-gateway is still running (off-limits invariant) …"
ssh_run 'docker ps --filter name=openclaw-gateway --format "{{.Names}} {{.Status}}"' \
    | tee "$EVIDENCE_DIR/vps-openclaw-status.txt"

echo
echo "✓ v6-baseline VPS teardown complete"
echo "  evidence: $EVIDENCE_DIR/"
