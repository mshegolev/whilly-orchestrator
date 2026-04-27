#!/usr/bin/env bash
# Wrapper around Claude CLI that injects the proxy and `--dangerously-skip-permissions`.
#
# Why this exists:
#   The interactive `claude` is a shell alias to `claudeproxy` (zsh function) that:
#     1. ensures an SSH tunnel to gpt-proxy on localhost:11112
#     2. exports HTTP_PROXY / HTTPS_PROXY pointing at it
#     3. invokes the real binary with `--dangerously-skip-permissions`
#   Subprocesses spawned by Whilly do NOT inherit shell aliases or zsh functions,
#   so we need an actual executable on disk to point CLAUDE_BIN at.
#
# Tunnel is assumed pre-existing (start it once via `claudeproxy` in your
# interactive shell). This wrapper does NOT spin one up — Whilly is launching
# many subprocesses in parallel and that would race.

set -euo pipefail

CLAUDE_REAL="${CLAUDE_REAL:-$HOME/.reflex/.nvm/versions/node/v20.19.6/bin/claude}"
PROXY_URL="${WHILLY_PROXY_URL:-http://127.0.0.1:11112}"

if [[ ! -x "$CLAUDE_REAL" ]]; then
  echo "claude-wrapper: real binary not found at $CLAUDE_REAL" >&2
  exit 127
fi

export HTTP_PROXY="$PROXY_URL"
export HTTPS_PROXY="$PROXY_URL"

exec "$CLAUDE_REAL" --dangerously-skip-permissions "$@"
