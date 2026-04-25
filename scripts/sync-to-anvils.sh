#!/usr/bin/env bash
#
# sync-to-anvils.sh — push a release ref from this repo to
# podlodka-ai-club/the-anvils. Run locally, on demand.
#
# Usage:
#   scripts/sync-to-anvils.sh                 # push latest v* tag
#   scripts/sync-to-anvils.sh v3.2.1          # push a specific tag
#   scripts/sync-to-anvils.sh release/3.x     # push a release branch
#   scripts/sync-to-anvils.sh --list          # show what would be synced
#
# Env overrides:
#   ANVILS_REMOTE_URL    (default: git@github.com:podlodka-ai-club/the-anvils.git)
#   ANVILS_REMOTE_NAME   (default: anvils)
#   ANVILS_TARGET_BRANCH (default: main) — target branch when pushing a branch/sha
#   NO_TAGS=1            skip pushing v* tags alongside a branch push
#   NO_MAIN_UPDATE=1     when syncing a tag, skip advancing ANVILS_TARGET_BRANCH
#                        to that tag's commit (by default we advance main so the
#                        overview shows the latest release)
#
# Auth: uses your local git credentials (SSH key for the default URL).
# Requires write access to podlodka-ai-club/the-anvils.

set -euo pipefail

ANVILS_REMOTE_NAME="${ANVILS_REMOTE_NAME:-anvils}"
ANVILS_REMOTE_URL="${ANVILS_REMOTE_URL:-git@github.com:podlodka-ai-club/the-anvils.git}"
ANVILS_TARGET_BRANCH="${ANVILS_TARGET_BRANCH:-main}"

REF="${1:-}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

# Must run from repo root (or anywhere inside the working tree).
git rev-parse --show-toplevel >/dev/null 2>&1 \
  || die "not inside a git repo"

# --list mode: show candidates and exit
if [[ "${REF}" == "--list" ]]; then
  log "Release tags (most recent first):"
  git tag --list 'v*' --sort=-creatordate | head -20
  log "Release branches (local + remote):"
  git branch --all --list '*release/*' || true
  exit 0
fi

# Default: latest v* tag
if [[ -z "${REF}" ]]; then
  REF="$(git tag --list 'v*' --sort=-creatordate | head -n1 || true)"
  [[ -n "${REF}" ]] || die "no v* tag found — pass a tag or branch explicitly"
  log "No ref given — defaulting to latest tag: ${REF}"
fi

# Resolve ref to a commit
if ! RESOLVED_SHA="$(git rev-parse --verify "${REF}^{commit}" 2>/dev/null)"; then
  die "ref '${REF}' does not resolve to a commit"
fi

# Ensure remote exists / is correct
if git remote | grep -qx "${ANVILS_REMOTE_NAME}"; then
  current_url="$(git remote get-url "${ANVILS_REMOTE_NAME}")"
  if [[ "${current_url}" != "${ANVILS_REMOTE_URL}" ]]; then
    log "Updating remote ${ANVILS_REMOTE_NAME} URL: ${current_url} -> ${ANVILS_REMOTE_URL}"
    git remote set-url "${ANVILS_REMOTE_NAME}" "${ANVILS_REMOTE_URL}"
  fi
else
  log "Adding remote ${ANVILS_REMOTE_NAME} -> ${ANVILS_REMOTE_URL}"
  git remote add "${ANVILS_REMOTE_NAME}" "${ANVILS_REMOTE_URL}"
fi

# Probe connectivity early with a friendly error
if ! git ls-remote --heads "${ANVILS_REMOTE_NAME}" >/dev/null 2>&1; then
  die "cannot reach ${ANVILS_REMOTE_URL} — check SSH key / write access"
fi

log "Syncing ${REF} (${RESOLVED_SHA:0:12}) -> ${ANVILS_REMOTE_URL}"

# Is the ref a tag? Push as tag, and (by default) advance the target branch.
if git show-ref --verify --quiet "refs/tags/${REF}"; then
  log "Pushing tag ${REF}"
  git push "${ANVILS_REMOTE_NAME}" "refs/tags/${REF}"

  if [[ -z "${NO_MAIN_UPDATE:-}" ]]; then
    TAG_COMMIT="$(git rev-parse --verify "${REF}^{commit}")"
    if git ls-remote --exit-code --heads "${ANVILS_REMOTE_NAME}" "${ANVILS_TARGET_BRANCH}" >/dev/null 2>&1; then
      log "Advancing ${ANVILS_REMOTE_NAME}/${ANVILS_TARGET_BRANCH} -> ${TAG_COMMIT:0:12} (force-with-lease)"
      git push "${ANVILS_REMOTE_NAME}" "${TAG_COMMIT}:refs/heads/${ANVILS_TARGET_BRANCH}" --force-with-lease
    else
      warn "${ANVILS_REMOTE_NAME}/${ANVILS_TARGET_BRANCH} does not exist — initial force push"
      git push "${ANVILS_REMOTE_NAME}" "${TAG_COMMIT}:refs/heads/${ANVILS_TARGET_BRANCH}" --force
    fi
  else
    log "NO_MAIN_UPDATE=1 — leaving ${ANVILS_REMOTE_NAME}/${ANVILS_TARGET_BRANCH} unchanged"
  fi

  log "Done — tag ${REF} is on the-anvils."
  exit 0
fi

# Otherwise: branch or raw sha. Push to ANVILS_TARGET_BRANCH.
if git ls-remote --exit-code --heads "${ANVILS_REMOTE_NAME}" "${ANVILS_TARGET_BRANCH}" >/dev/null 2>&1; then
  log "Pushing ${RESOLVED_SHA:0:12} -> ${ANVILS_REMOTE_NAME}/${ANVILS_TARGET_BRANCH} (force-with-lease)"
  git push "${ANVILS_REMOTE_NAME}" "${RESOLVED_SHA}:refs/heads/${ANVILS_TARGET_BRANCH}" --force-with-lease
else
  warn "${ANVILS_REMOTE_NAME}/${ANVILS_TARGET_BRANCH} does not exist — initial force push"
  git push "${ANVILS_REMOTE_NAME}" "${RESOLVED_SHA}:refs/heads/${ANVILS_TARGET_BRANCH}" --force
fi

# Also push v* tags so consumers can `pip install git+...@v3.2.1`
if [[ -z "${NO_TAGS:-}" ]]; then
  log "Pushing v* tags"
  # shellcheck disable=SC2046
  git push "${ANVILS_REMOTE_NAME}" $(git tag --list 'v*' | sed 's|^|refs/tags/|') 2>/dev/null || true
fi

log "Done — ${REF} is on ${ANVILS_REMOTE_NAME}/${ANVILS_TARGET_BRANCH}."
