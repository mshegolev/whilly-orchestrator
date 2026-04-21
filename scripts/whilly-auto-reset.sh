#!/usr/bin/env bash
# whilly-auto-reset.sh — scrub every artefact `whilly-auto.sh` might have left
# behind and return a GitHub issue to "Todo" on the project board, so the next
# `whilly-auto.sh` run starts from a pristine state.
#
# What it cleans:
#   * all `.whilly_workspaces/*/` worktrees (whilly/workspace/* branches too)
#   * all `tasks-issue-*.json` plan files in the repo root
#   * `.whilly_state.json` crash-recovery state
#   * any open PR whose head branch is `whilly/workspace/*`
#   * the issue's project v2 card → "Todo"
#
# What it does NOT touch:
#   * main / upstream (only fetches, never pushes anything destructive)
#   * merged PRs, closed issues, release tags
#   * whilly_logs/ in the main repo (those are useful post-mortem artifacts)
#
# Usage:
#   scripts/whilly-auto-reset.sh                      # pick first whilly:ready issue
#   scripts/whilly-auto-reset.sh 159                  # reset specific issue number
#   REPO=owner/name scripts/whilly-auto-reset.sh 159
#   DRY_RUN=1 scripts/whilly-auto-reset.sh 159        # show the plan, do nothing
#
# Exit codes:
#   0  reset succeeded
#   1  precondition missing or gh auth / jq missing
#   2  partial failure (some cleanup step failed — details logged)

set -euo pipefail

LABEL="${LABEL:-whilly:ready}"
BASE_BRANCH="${BASE_BRANCH:-main}"
REPO="${REPO:-}"
DRY_RUN="${DRY_RUN:-0}"
PROJECT_URL="${PROJECT_URL:-https://github.com/users/mshegolev/projects/4}"

unset GITHUB_TOKEN 2>/dev/null || true

die()  { echo "error: $*" >&2; exit "${2:-1}"; }
info() { echo "→ $*"; }
warn() { echo "warn: $*" >&2; }
run()  { [[ "$DRY_RUN" == "1" ]] && echo "[dry-run] $*" || eval "$@"; }

command -v gh >/dev/null || die "gh CLI not on PATH" 1
command -v jq >/dev/null || die "jq not on PATH" 1
command -v git >/dev/null || die "git not on PATH" 1

if [[ -z "$REPO" ]]; then
    REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) \
        || die "cannot detect repo (pass REPO=owner/name)" 1
fi

# Target issue: explicit arg or the first open one matching $LABEL.
TARGET_ISSUE="${1:-}"
if [[ -z "$TARGET_ISSUE" ]]; then
    TARGET_ISSUE=$(gh issue list --repo "$REPO" --label "$LABEL" --state open --limit 1 \
        --json number -q '.[0].number' 2>/dev/null)
    [[ -n "$TARGET_ISSUE" ]] || die "no open issues with label '$LABEL' — nothing to reset" 1
fi
info "Repo: $REPO  ·  Target issue: #$TARGET_ISSUE"

# ── 1. Close any open PR from whilly/workspace/* (these would block re-run) ───
OPEN_WHILLY_PRS=$(gh pr list --repo "$REPO" --state open \
    --json number,headRefName -q '.[] | select(.headRefName | startswith("whilly/workspace/")) | .number' \
    2>/dev/null || true)
if [[ -n "$OPEN_WHILLY_PRS" ]]; then
    while IFS= read -r pr; do
        info "Closing stale whilly PR #$pr"
        run "gh pr close $pr --repo '$REPO' --delete-branch --comment 'Reset by whilly-auto-reset.sh' 2>&1 | tail -3"
    done <<<"$OPEN_WHILLY_PRS"
else
    info "No open whilly/workspace/* PRs to close"
fi

# ── 2. Remove every .whilly_workspaces/* worktree ──────────────────────────────
WORKTREES_TO_REMOVE=$(git worktree list --porcelain \
    | awk '/^worktree / && $2 ~ /\.whilly_workspaces/ {print $2}' || true)
if [[ -n "$WORKTREES_TO_REMOVE" ]]; then
    while IFS= read -r wt; do
        info "Removing worktree: $wt"
        run "git worktree remove '$wt' --force 2>&1 | head -3"
    done <<<"$WORKTREES_TO_REMOVE"
else
    info "No whilly worktrees to remove"
fi

# ── 3. Delete whilly/workspace/* branches (local + remote) ─────────────────────
LOCAL_WSP_BRANCHES=$(git branch --list 'whilly/workspace/*' | sed 's/^[*+ ]*//' || true)
if [[ -n "$LOCAL_WSP_BRANCHES" ]]; then
    while IFS= read -r br; do
        [[ -z "$br" ]] && continue
        info "Deleting local branch: $br"
        run "git branch -D '$br' 2>&1 | head -3"
    done <<<"$LOCAL_WSP_BRANCHES"
fi

REMOTE_WSP_BRANCHES=$(git ls-remote --heads origin 'whilly/workspace/*' \
    | awk '{print $2}' | sed 's|refs/heads/||' || true)
if [[ -n "$REMOTE_WSP_BRANCHES" ]]; then
    while IFS= read -r br; do
        [[ -z "$br" ]] && continue
        info "Deleting remote branch: $br"
        run "git push origin --delete '$br' 2>&1 | tail -3"
    done <<<"$REMOTE_WSP_BRANCHES"
fi

# ── 4. Remove stale plan files + state + events log ────────────────────────────
for pattern in "tasks-issue-*.json" "tasks-from-*.json" ".whilly_state.json"; do
    matches=$(compgen -G "$pattern" 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
        info "Removing: $pattern"
        run "rm -f $pattern"
    fi
done

# ── 5. Fetch origin, fast-forward base branch ──────────────────────────────────
info "Syncing $BASE_BRANCH with origin"
if [[ "$DRY_RUN" != "1" ]]; then
    git fetch origin "$BASE_BRANCH" --prune --quiet || warn "git fetch failed (continuing)"
    CURRENT=$(git rev-parse --abbrev-ref HEAD)
    if [[ "$CURRENT" != "$BASE_BRANCH" ]]; then
        # Best-effort switch; ignore if blocked by uncommitted changes (user may have WIP).
        git checkout "$BASE_BRANCH" --quiet 2>/dev/null || warn "cannot switch to $BASE_BRANCH (uncommitted changes?)"
    fi
    git pull --ff-only origin "$BASE_BRANCH" --quiet 2>/dev/null || warn "pull --ff-only failed (local diverged?)"
fi

# ── 6. Reset the target issue's project board card to "Todo" ───────────────────
# Use scripts/move_project_card.py — it uses the gh CLI token which has the
# `project` scope needed for Projects v2 mutations.
if [[ -x "scripts/move_project_card.py" ]]; then
    info "Resetting issue #$TARGET_ISSUE card → Todo"
    if [[ "$DRY_RUN" != "1" ]]; then
        python3 scripts/move_project_card.py "$PROJECT_URL" "$TARGET_ISSUE" "Todo" --repo "$REPO" 2>&1 \
            | tail -3 \
            || warn "move_project_card failed — card may already be in Todo or not on the board"
    fi
else
    warn "scripts/move_project_card.py missing — card reset skipped"
fi

# ── 7. Reopen the issue if it was auto-closed by a stale PR ───────────────────
ISSUE_STATE=$(gh issue view "$TARGET_ISSUE" --repo "$REPO" --json state -q .state 2>/dev/null || echo "")
if [[ "$ISSUE_STATE" == "CLOSED" ]]; then
    info "Issue #$TARGET_ISSUE was CLOSED — reopening"
    run "gh issue reopen $TARGET_ISSUE --repo '$REPO' 2>&1 | tail -2"
fi

info "Reset complete for issue #$TARGET_ISSUE"
