#!/usr/bin/env bash
# whilly-auto.sh — "First issue → merged to main" end-to-end pipeline.
#
# Picks the first open GitHub issue matching $LABEL, runs whilly on it, pushes
# the workspace branch, opens a PR, merges into $BASE_BRANCH, then runs the
# whilly post-merge hook so the Projects v2 card lands in Done.
#
# Prerequisites:
#   * whilly installed and on $PATH (pip install whilly-orchestrator)
#   * gh authenticated (gh auth login) with repo + project scopes
#   * CLAUDE_BIN reachable (whilly spawns the Claude CLI for the actual work)
#
# Usage:
#   scripts/whilly-auto.sh                          # first whilly:ready issue in current repo
#   LABEL=bug scripts/whilly-auto.sh                # first issue with label "bug"
#   REPO=owner/name scripts/whilly-auto.sh          # override repo
#   MERGE_METHOD=merge scripts/whilly-auto.sh       # merge commit instead of squash
#   DRY_RUN=1 scripts/whilly-auto.sh                # print the plan, make no changes
#
# Exit codes:
#   0  merged
#   1  no matching issue / precondition missing
#   2  whilly run failed (task not completed)
#   3  PR create / push / merge failed
#
# Status transitions handled automatically:
#   Todo → In Progress → In Review   (live sync during `whilly --from-issue --go`)
#   In Review → Done                 (this script, after `gh pr merge` succeeds)

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────

LABEL="${LABEL:-whilly:ready}"
BASE_BRANCH="${BASE_BRANCH:-main}"
MERGE_METHOD="${MERGE_METHOD:-squash}"   # squash | merge | rebase
REPO="${REPO:-}"
DRY_RUN="${DRY_RUN:-0}"

# gh often fights GITHUB_TOKEN when it's set; prefer the authenticated user.
unset GITHUB_TOKEN 2>/dev/null || true

die()  { echo "error: $*" >&2; exit "${2:-1}"; }
info() { echo "→ $*"; }
run()  { [[ "$DRY_RUN" == "1" ]] && echo "[dry-run] $*" || eval "$@"; }

# ── 0. Preconditions ───────────────────────────────────────────────────────────

command -v whilly >/dev/null || die "whilly not on PATH — pip install whilly-orchestrator" 1
command -v gh >/dev/null     || die "gh CLI not on PATH" 1
command -v jq >/dev/null     || die "jq not on PATH (used to parse gh output)" 1

if [[ -z "$REPO" ]]; then
    REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) \
        || die "cannot detect repo (pass REPO=owner/name)" 1
fi

# ── 0.1. Sync base branch with origin ──────────────────────────────────────────
# Whilly's workspace worktree branches from the current HEAD of the base branch.
# If the local checkout is stale, the agent works against outdated code and the
# PR diff is wrong. Skip with SKIP_SYNC=1 for air-gapped / offline use.

if [[ "${SKIP_SYNC:-0}" != "1" ]]; then
    info "Syncing $BASE_BRANCH with origin"
    if [[ "$DRY_RUN" != "1" ]]; then
        # Stash any uncommitted changes on the current branch before switching.
        STASHED=0
        if [[ -n "$(git status --porcelain)" ]]; then
            git stash push -u -m "whilly-auto autosync $(date +%s)" >/dev/null \
                && STASHED=1
        fi
        CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
        git fetch origin "$BASE_BRANCH" --quiet \
            || die "git fetch origin $BASE_BRANCH failed" 1
        git checkout "$BASE_BRANCH" --quiet \
            || die "git checkout $BASE_BRANCH failed (uncommitted changes?)" 1
        git pull --ff-only origin "$BASE_BRANCH" --quiet \
            || die "git pull --ff-only origin $BASE_BRANCH failed (local diverged)" 1
        info "On $BASE_BRANCH @ $(git rev-parse --short HEAD)"
        # Best-effort restore: switch back + pop stash only if we moved.
        if [[ "$CURRENT_BRANCH" != "$BASE_BRANCH" ]]; then
            git checkout "$CURRENT_BRANCH" --quiet 2>/dev/null || true
        fi
        [[ "$STASHED" == "1" ]] && git stash pop --quiet 2>/dev/null || true
    fi
fi

# ── 1. Pick first ready issue ──────────────────────────────────────────────────

FIRST=$(gh issue list --repo "$REPO" --label "$LABEL" --state open --limit 1 \
        --json number,title,url 2>/dev/null)
NUMBER=$(jq -r '.[0].number // empty' <<<"$FIRST")
TITLE=$(jq -r  '.[0].title  // empty' <<<"$FIRST")
URL=$(jq -r    '.[0].url    // empty' <<<"$FIRST")

[[ -n "$NUMBER" ]] || die "no open issues with label '$LABEL' in $REPO" 1

info "Picked #$NUMBER: $TITLE"
info "        $URL"

# ── 2. Run whilly (live status sync Todo → In Progress → In Review) ────────────

# Snapshot existing worktrees so we can identify the one whilly creates.
BEFORE=$(git worktree list --porcelain | awk '/^worktree /{print $2}' | sort)

PLAN="tasks-issue-${REPO//\//-}-${NUMBER}.json"
info "Running: whilly --from-issue ${REPO}#${NUMBER} --go --headless"
if [[ "$DRY_RUN" != "1" ]]; then
    whilly --from-issue "${REPO}#${NUMBER}" --go --headless \
        || die "whilly run failed — check whilly_logs/ and $PLAN" 2
fi

# ── 3. Locate the workspace worktree and its branch ────────────────────────────

AFTER=$(git worktree list --porcelain | awk '/^worktree /{print $2}' | sort)
NEW_WT=$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | head -1 || true)

if [[ -z "${NEW_WT:-}" ]]; then
    # Workspace was reused — fall back to the canonical whilly/workspace/* match.
    NEW_WT=$(git worktree list --porcelain \
        | awk '
            /^worktree / {wt=$2}
            /^branch refs\/heads\/whilly\/workspace\// {print wt; exit}
          ')
fi
[[ -n "${NEW_WT:-}" && -d "$NEW_WT" ]] \
    || die "could not locate whilly workspace worktree" 3

BRANCH=$(git -C "$NEW_WT" rev-parse --abbrev-ref HEAD)
info "Workspace: $NEW_WT (branch $BRANCH)"

if ! git -C "$NEW_WT" log -1 --format='%H' "${BASE_BRANCH}..HEAD" -- >/dev/null 2>&1 \
        || [[ -z "$(git -C "$NEW_WT" log "${BASE_BRANCH}..HEAD" --oneline 2>/dev/null)" ]]; then
    die "branch $BRANCH has no commits ahead of $BASE_BRANCH — nothing to merge" 2
fi

# ── 4. Push & open PR ──────────────────────────────────────────────────────────

run "git -C '$NEW_WT' push -u origin '$BRANCH'" \
    || die "git push failed for $BRANCH" 3

PR_BODY=$(cat <<EOF
Closes #${NUMBER}

Automated run by Whilly Orchestrator.
Plan: \`$PLAN\`
Workspace: \`$NEW_WT\`

🤖 Generated with [Whilly](https://github.com/mshegolev/whilly-orchestrator)
EOF
)

if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] gh pr create --base $BASE_BRANCH --head $BRANCH --title \"$TITLE\""
    PR_URL="https://example.invalid/dry-run"
else
    PR_URL=$(gh pr create \
        --repo "$REPO" \
        --base "$BASE_BRANCH" \
        --head "$BRANCH" \
        --title "$TITLE" \
        --body "$PR_BODY" 2>&1) \
        || die "gh pr create failed: $PR_URL" 3
fi
info "PR: $PR_URL"

# ── 5. Merge (waits for required checks when branch protection is on) ──────────

# --auto is a no-op when branch protection is off (merges immediately) and the
# right thing to do when it is on (merges once all required checks pass).
if [[ "$DRY_RUN" != "1" ]]; then
    gh pr merge "$PR_URL" --"$MERGE_METHOD" --delete-branch --auto \
        || die "gh pr merge failed" 3

    # Poll until actually merged so the post-merge hook runs after the card
    # is truly ready to move to Done. Cap at 30 minutes to avoid hanging CI.
    info "Waiting for merge..."
    for _ in $(seq 1 180); do
        STATE=$(gh pr view "$PR_URL" --json state -q .state 2>/dev/null || echo "")
        [[ "$STATE" == "MERGED" ]] && break
        [[ "$STATE" == "CLOSED" ]] && die "PR closed without merge" 3
        sleep 10
    done
    [[ "$STATE" == "MERGED" ]] || die "PR not merged after 30 minutes" 3
    info "Merged"
fi

# ── 6. Post-merge: In Review → Done on the Projects v2 board ──────────────────

if [[ -f "$PLAN" ]]; then
    info "Running: whilly --post-merge $PLAN"
    run "whilly --post-merge '$PLAN'" || echo "warn: post-merge hook non-zero (board sync is advisory)"
else
    echo "warn: plan file '$PLAN' missing — skipping post-merge board sync"
fi

info "Done · issue #$NUMBER merged into $BASE_BRANCH"
