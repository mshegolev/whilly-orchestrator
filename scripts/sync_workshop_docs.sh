#!/usr/bin/env bash
# Sync workshop docs from this repo into the Obsidian vault.
#
# Source: docs/workshop/        (canonical, lives with the code)
# Dest:   <vault>/02-Projects/ai/workshop/whilly/
#
# Usage:
#   scripts/sync_workshop_docs.sh             # uses default vault path
#   scripts/sync_workshop_docs.sh --dry-run   # show what would be copied
#   VAULT_PATH=/path scripts/sync_workshop_docs.sh
#
# rsync semantics:
#   --delete to keep the destination in lockstep with the source
#   --exclude .DS_Store, *.swp, .git
#
# Idempotent. Safe to re-run.

set -euo pipefail

# ── Defaults ───────────────────────────────────────────────
DEFAULT_VAULT="${HOME}/Documents/Obsidian Vault"
VAULT_PATH="${VAULT_PATH:-$DEFAULT_VAULT}"
SUBPATH="02-Projects/ai/workshop/whilly"

DRY_RUN=""
if [ "${1:-}" = "--dry-run" ] || [ "${1:-}" = "-n" ]; then
    DRY_RUN="--dry-run"
fi

# ── Resolve paths ──────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/docs/workshop"
DEST_DIR="${VAULT_PATH}/${SUBPATH}"

if [ ! -d "$SRC_DIR" ]; then
    echo "✗ Source directory not found: $SRC_DIR" >&2
    exit 1
fi
if [ ! -d "$VAULT_PATH" ]; then
    echo "✗ Obsidian vault not found: $VAULT_PATH" >&2
    echo "  Set VAULT_PATH to your vault location." >&2
    exit 1
fi

mkdir -p "$DEST_DIR"

echo "Sync workshop docs"
echo "  src:  $SRC_DIR"
echo "  dest: $DEST_DIR"
[ -n "$DRY_RUN" ] && echo "  mode: DRY RUN (no files copied)"
echo ""

# ── rsync ──────────────────────────────────────────────────
rsync -av $DRY_RUN \
    --delete \
    --exclude='.DS_Store' \
    --exclude='*.swp' \
    --exclude='.git/' \
    "${SRC_DIR}/" \
    "${DEST_DIR}/"

if [ -z "$DRY_RUN" ]; then
    echo ""
    echo "✓ Sync complete."
    echo "  Files in destination:"
    ls -la "$DEST_DIR" | tail -n +2
fi
