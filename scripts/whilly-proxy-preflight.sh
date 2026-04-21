#!/usr/bin/env bash
# whilly-proxy-preflight.sh — source-able helper that activates the SSH-based
# HTTP proxy for the current shell session (same thing `zshp` does, but
# callable from bash instead of an interactive zsh).
#
# Usage:
#   source scripts/whilly-proxy-preflight.sh   # from another bash script
#   scripts/whilly-proxy-preflight.sh          # standalone (prints env to stdout)
#
# What it does (idempotent):
#   1. If ALL_PROXY / HTTPS_PROXY / https_proxy already set — no-op (assumes the
#      caller already ran `zshp` in the parent zsh shell).
#   2. Otherwise: calls the user's `_ensure_tunnel gpt-proxy 11112 8888` (via a
#      zsh subshell that sources ~/.zshrc), waits until the local listener on
#      11112 is up, then exports HTTP(S)_PROXY / NO_PROXY matching what `zshp`
#      http-mode would set.
#   3. Verifies the tunnel is actually forwarding traffic via curl against
#      https://api.anthropic.com. On failure: exits 1 with a clear hint.
#
# Env override:
#   WHILLY_PROXY_SKIP=1         — skip entirely (for networks w/o need for proxy)
#   WHILLY_PROXY_HOST=gpt-proxy — ssh host alias (default: gpt-proxy)
#   WHILLY_PROXY_LOCAL=11112    — local port for HTTP tunnel
#   WHILLY_PROXY_REMOTE=8888    — remote port on the proxy host
#
# Exit codes (when run standalone):
#   0  proxy is active (either was already set, or we brought it up)
#   1  tunnel could not be brought up or verified

# Detect if sourced vs. executed — both paths work.
_whilly_proxy_was_sourced=0
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    _whilly_proxy_was_sourced=1
fi

_whilly_proxy_info() { echo "→ [proxy] $*" >&2; }
_whilly_proxy_die()  { echo "error: [proxy] $*" >&2; return 1; }

_whilly_activate_proxy() {
    if [[ "${WHILLY_PROXY_SKIP:-0}" == "1" ]]; then
        _whilly_proxy_info "WHILLY_PROXY_SKIP=1 — skipping proxy setup"
        return 0
    fi

    # Already active in parent shell? (user ran `zshp` themselves)
    if [[ -n "${ALL_PROXY:-}${HTTPS_PROXY:-}${https_proxy:-}" ]]; then
        _whilly_proxy_info "proxy already active: ${HTTPS_PROXY:-${ALL_PROXY:-${https_proxy}}}"
        return 0
    fi

    local host="${WHILLY_PROXY_HOST:-gpt-proxy}"
    local local_port="${WHILLY_PROXY_LOCAL:-11112}"
    local remote_port="${WHILLY_PROXY_REMOTE:-8888}"
    local proxy_url="http://127.0.0.1:${local_port}"

    # Mirror zshp http-mode no_proxy list so traffic to internal hosts
    # bypasses the tunnel.
    local no_proxy_list="localhost,127.0.0.1,*.mts.ru,gitlab.services.mts.ru,10.*,11.*"

    # Is the tunnel already listening? (idempotent — `zshp` may have been
    # called earlier in a different shell that's since closed)
    if ! lsof -iTCP:"${local_port}" -sTCP:LISTEN >/dev/null 2>&1; then
        _whilly_proxy_info "bringing up SSH tunnel: ${host} -> 127.0.0.1:${local_port}"
        # Call the user's _ensure_tunnel helper via zsh. It's defined in their
        # .zshrc (or shell snapshot) — the only reliable way to reuse it.
        if ! zsh -c "source ~/.zshrc >/dev/null 2>&1; typeset -f _ensure_tunnel >/dev/null" 2>/dev/null; then
            _whilly_proxy_die "no _ensure_tunnel helper in your zsh — run 'zshp' manually once"
            return 1
        fi
        zsh -c "source ~/.zshrc >/dev/null 2>&1; _ensure_tunnel ${host} ${local_port} ${remote_port}" \
            || { _whilly_proxy_die "_ensure_tunnel ${host} failed — check 'ssh ${host}' works"; return 1; }
    else
        _whilly_proxy_info "tunnel already listening on :${local_port}"
    fi

    # Export proxy env for this process tree (bash-side, no `exec zsh`).
    export HTTP_PROXY="${proxy_url}"
    export HTTPS_PROXY="${proxy_url}"
    export http_proxy="${proxy_url}"
    export https_proxy="${proxy_url}"
    export ALL_PROXY="${proxy_url}"
    export all_proxy="${proxy_url}"
    export NO_PROXY="${no_proxy_list}"
    export no_proxy="${no_proxy_list}"

    # Smoke test — don't proceed if the proxy can't reach Anthropic.
    if ! curl -sSf --proxy "${proxy_url}" --max-time 10 \
            -o /dev/null -w "%{http_code}" \
            https://api.anthropic.com/v1/messages -X POST 2>/dev/null \
            | grep -qE "^(401|400|405)$"; then
        # 401/400/405 means we reached the API (auth error is fine — we just
        # verify the path is open). Anything else (000 = no connection, 5xx)
        # means the tunnel is broken.
        local code
        code=$(curl -s --proxy "${proxy_url}" --max-time 10 \
            -o /dev/null -w "%{http_code}" \
            https://api.anthropic.com 2>/dev/null || echo "000")
        if [[ "$code" == "000" ]]; then
            _whilly_proxy_die "tunnel up but no traffic — 'ssh ${host}' works? run 'zshp' manually"
            return 1
        fi
        # Anything else (200, 403, …) is fine — we reached the endpoint.
    fi
    _whilly_proxy_info "proxy active: ${proxy_url}"
    return 0
}

# Main
if ! _whilly_activate_proxy; then
    # When sourced: return non-zero so caller can decide. When executed: exit.
    if (( _whilly_proxy_was_sourced == 1 )); then
        return 1
    else
        exit 1
    fi
fi

# When executed standalone, emit the env in a form the caller can eval.
if (( _whilly_proxy_was_sourced == 0 )); then
    for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy NO_PROXY no_proxy; do
        val="${!v-}"
        [[ -n "$val" ]] && printf 'export %s=%q\n' "$v" "$val"
    done
fi
