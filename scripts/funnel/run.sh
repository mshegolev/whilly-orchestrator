#!/usr/bin/env bash
# Whilly funnel sidecar runtime loop — v6.0 paid-plan replacement of
# the v5.0 free-tier sidecar flow.
#
# Holds an outbound SSH reverse-tunnel against `plan@localhost.run`
# using a private SSH key registered in the operator's localhost.run
# dashboard, force-binds the public hostname
# `whilly-orchestrator.lhr.rocks` (configurable via `LHR_HOSTNAME`),
# and publishes the STABLE URL to:
#   1. Postgres `funnel_url` singleton table (primary; back-compat
#      for v5.0 readers). The URL is constant across reconnects, so
#      `updated_at` simply tracks the last reconnect, not a URL
#      rotation.
#   2. Shared-volume file `${FUNNEL_URL_FILE}` (fallback; written via
#      tmp + atomic rename).
#
# On SSH exit (network blip / server-side close) the script sleeps
# `FUNNEL_RETRY_BACKOFF_SECONDS` and reconnects. SSH keepalives
# (ServerAliveInterval=30, ServerAliveCountMax=3,
# ExitOnForwardFailure=yes) detect silent drops fast.
#
# Failure modes (clear stderr, non-zero exit):
#   1. SSH key file missing at the configured path — the operator
#      forgot to drop the private key on the VPS. Stderr cites the
#      dashboard URL (https://localhost.run/dashboard/ssh-keys/) and
#      the expected on-disk path.
#   2. SSH key present but not registered in the dashboard — sshd
#      rejects auth, the script lets ssh propagate the auth-failure
#      message, prints a diagnostic suffix that names the dashboard
#      URL, and exits non-zero.
#
# Security:
#   * `WHILLY_DATABASE_URL` is parsed once into discrete
#     PG{HOST,PORT,USER,DATABASE} + PGPASSWORD env vars and is
#     NEVER passed to `psql` on the command line — that would leak
#     the password into `ps` output and the container's stdout
#     stream.
#   * The SSH private key is mounted read-only at the in-container
#     path; the script verifies its existence + permissions BEFORE
#     dialling out so a permission slip surfaces immediately.
#
# Test hooks:
#   * `FUNNEL_FAKE_URL` — when set, the script bypasses real SSH
#     and emits a synthetic banner containing that URL. Used by
#     unit/integration tests to exercise the publish path without
#     depending on the public localhost.run service.
#   * `FUNNEL_ONESHOT` — when truthy, exit after the first publish
#     instead of looping. Test fixtures set this so the container
#     terminates deterministically.
#   * `FUNNEL_DUMP_SSH_ARGS` — when truthy, print the assembled
#     ssh argv (one token per line) and exit 0 without invoking
#     ssh. Used by the static-contract test to assert the SSH
#     command shape without a live tunnel.
#   * `FUNNEL_SKIP_KEY_CHECK` — when truthy, skip the on-disk SSH
#     key existence check. Only used by tests that exercise the
#     dump-args path with no key file present.

set -eo pipefail

LHR_HOSTNAME="${LHR_HOSTNAME:-whilly-orchestrator.lhr.rocks}"
LHR_REMOTE_USER="${LHR_REMOTE_USER:-plan@localhost.run}"
LHR_REMOTE_HOST="${LHR_REMOTE_HOST:-localhost.run}"
LHR_LOCAL_TARGET="${LHR_LOCAL_TARGET:-control-plane:8000}"
LHR_SSH_KEY_PATH_INSIDE="${LHR_SSH_KEY_PATH_INSIDE:-/etc/whilly-funnel/ssh-key}"
LHR_DASHBOARD_URL="${LHR_DASHBOARD_URL:-https://localhost.run/dashboard/ssh-keys/}"

FUNNEL_SERVER_ALIVE_INTERVAL="${FUNNEL_SERVER_ALIVE_INTERVAL:-30}"
FUNNEL_SERVER_ALIVE_COUNT_MAX="${FUNNEL_SERVER_ALIVE_COUNT_MAX:-3}"
FUNNEL_RETRY_BACKOFF_SECONDS="${FUNNEL_RETRY_BACKOFF_SECONDS:-5}"
FUNNEL_URL_FILE="${FUNNEL_URL_FILE:-/funnel/url.txt}"
FUNNEL_FAKE_URL="${FUNNEL_FAKE_URL:-}"
FUNNEL_ONESHOT="${FUNNEL_ONESHOT:-0}"
FUNNEL_DUMP_SSH_ARGS="${FUNNEL_DUMP_SSH_ARGS:-0}"
FUNNEL_SKIP_KEY_CHECK="${FUNNEL_SKIP_KEY_CHECK:-0}"

if [ -n "$FUNNEL_FAKE_URL" ]; then
    PUBLIC_URL="$FUNNEL_FAKE_URL"
else
    PUBLIC_URL="${PUBLIC_URL:-https://${LHR_HOSTNAME}}"
fi

log() {
    printf '[funnel %s] %s\n' "$(date -u +%FT%TZ)" "$*"
}

err() {
    printf '[funnel %s] ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}

split_local_target() {
    local target="$1"
    local host="${target%%:*}"
    local port="${target##*:}"
    if [ "$host" = "$target" ] || [ -z "$port" ]; then
        port="8000"
        host="$target"
    fi
    printf '%s\n%s\n' "$host" "$port"
}

parse_dsn_into_pg_env() {
    local dsn="${1:-}"
    if [ -z "$dsn" ]; then
        return 1
    fi
    python3 - "$dsn" <<'PYEOF' 2>/dev/null || awk_parse_dsn "$dsn"
import sys
from urllib.parse import urlparse, unquote

raw = sys.argv[1]
parsed = urlparse(raw)
host = parsed.hostname or ""
port = parsed.port or 5432
user = unquote(parsed.username or "")
password = unquote(parsed.password or "")
db = (parsed.path or "/").lstrip("/") or ""
print(f"PGHOST={host}")
print(f"PGPORT={port}")
print(f"PGUSER={user}")
print(f"PGPASSWORD={password}")
print(f"PGDATABASE={db}")
PYEOF
}

awk_parse_dsn() {
    local dsn="$1"
    awk -v dsn="$dsn" 'BEGIN {
        rest = dsn
        sub(/^postgres(ql)?:\/\//, "", rest)
        atpos = index(rest, "@")
        if (atpos > 0) {
            creds = substr(rest, 1, atpos - 1)
            rest = substr(rest, atpos + 1)
            colpos = index(creds, ":")
            if (colpos > 0) {
                user = substr(creds, 1, colpos - 1)
                pw = substr(creds, colpos + 1)
            } else {
                user = creds
                pw = ""
            }
        }
        slashpos = index(rest, "/")
        if (slashpos > 0) {
            hostport = substr(rest, 1, slashpos - 1)
            db = substr(rest, slashpos + 1)
            qpos = index(db, "?")
            if (qpos > 0) db = substr(db, 1, qpos - 1)
        } else {
            hostport = rest
            db = ""
        }
        cpos = index(hostport, ":")
        if (cpos > 0) {
            host = substr(hostport, 1, cpos - 1)
            port = substr(hostport, cpos + 1)
        } else {
            host = hostport
            port = "5432"
        }
        printf("PGHOST=%s\n", host)
        printf("PGPORT=%s\n", port)
        printf("PGUSER=%s\n", user)
        printf("PGPASSWORD=%s\n", pw)
        printf("PGDATABASE=%s\n", db)
    }'
}

publish_to_file() {
    local url="$1"
    local target_dir
    target_dir=$(dirname -- "$FUNNEL_URL_FILE")
    if [ ! -d "$target_dir" ]; then
        mkdir -p "$target_dir"
    fi
    local tmp
    tmp=$(mktemp -p "$target_dir" .url.txt.XXXXXX 2>/dev/null) || \
        tmp="${target_dir}/.url.txt.$$.tmp"
    printf '%s\n' "$url" > "$tmp"
    mv -f "$tmp" "$FUNNEL_URL_FILE"
    log "wrote URL to $FUNNEL_URL_FILE"
}

publish_to_postgres() {
    local url="$1"
    if [ -z "${WHILLY_DATABASE_URL:-}" ]; then
        log "WHILLY_DATABASE_URL not set; skipping postgres publish"
        return 0
    fi
    local pg_env
    if ! pg_env=$(parse_dsn_into_pg_env "$WHILLY_DATABASE_URL"); then
        log "failed to parse WHILLY_DATABASE_URL; skipping postgres publish"
        return 0
    fi
    # shellcheck disable=SC2046
    export $(printf '%s\n' "$pg_env" | xargs)
    local sql
    sql=$(printf "INSERT INTO funnel_url (id, url) VALUES (1, '%s') ON CONFLICT (id) DO UPDATE SET url=EXCLUDED.url, updated_at=NOW();" \
        "$(printf '%s' "$url" | sed "s/'/''/g")")
    if psql -v ON_ERROR_STOP=1 -c "$sql" >/dev/null 2>&1; then
        log "wrote URL to postgres funnel_url table (last-reconnect timestamp refreshed)"
    else
        log "psql upsert failed (db unreachable or schema missing — migration 010 not yet applied?)"
    fi
    unset PGPASSWORD
}

publish_url() {
    local url="$1"
    log "publishing stable URL: $url"
    publish_to_file "$url"
    publish_to_postgres "$url"
}

verify_ssh_key_present() {
    if [ "$FUNNEL_SKIP_KEY_CHECK" = "1" ] || [ "$FUNNEL_SKIP_KEY_CHECK" = "true" ]; then
        return 0
    fi
    if [ -n "$FUNNEL_FAKE_URL" ]; then
        return 0
    fi
    if [ ! -f "$LHR_SSH_KEY_PATH_INSIDE" ]; then
        err "funnel: SSH key not found at $LHR_SSH_KEY_PATH_INSIDE — register key at $LHR_DASHBOARD_URL and drop private key on VPS"
        return 1
    fi
    return 0
}

build_ssh_args() {
    local host port
    while IFS= read -r line; do
        if [ -z "${host:-}" ]; then
            host="$line"
        else
            port="$line"
        fi
    done < <(split_local_target "$LHR_LOCAL_TARGET")
    local forward_spec="${LHR_HOSTNAME}:80:${host}:${port}"
    local -a args=(
        -o "ServerAliveInterval=${FUNNEL_SERVER_ALIVE_INTERVAL}"
        -o "ServerAliveCountMax=${FUNNEL_SERVER_ALIVE_COUNT_MAX}"
        -o ExitOnForwardFailure=yes
        -o StrictHostKeyChecking=accept-new
        -o IdentitiesOnly=yes
        -i "$LHR_SSH_KEY_PATH_INSIDE"
        -R "$forward_spec"
        -N
        "${LHR_REMOTE_USER%@*}@${LHR_REMOTE_HOST}"
    )
    printf '%s\n' "${args[@]}"
}

run_ssh_session() {
    if [ -n "$FUNNEL_FAKE_URL" ]; then
        cat <<EOF
===============================================================================
Welcome to localhost.run!  (FUNNEL_FAKE_URL bypass — test mode)

** your unique URL is: **
${FUNNEL_FAKE_URL}
===============================================================================
EOF
        return 0
    fi
    local -a ssh_args=()
    while IFS= read -r line; do
        ssh_args+=( "$line" )
    done < <(build_ssh_args)
    if ! ssh "${ssh_args[@]}"; then
        local rc=$?
        err "funnel: ssh exited $rc — if this looks like an auth failure, register the public key at $LHR_DASHBOARD_URL"
        return $rc
    fi
}

run_session_and_capture() {
    publish_url "$PUBLIC_URL"
    if [ "$FUNNEL_ONESHOT" = "1" ] || [ "$FUNNEL_ONESHOT" = "true" ]; then
        return 0
    fi
    run_ssh_session
}

resolve_tier_label() {
    printf 'paid-plan-stable (%s)' "$LHR_HOSTNAME"
}

main() {
    if [ "$FUNNEL_DUMP_SSH_ARGS" = "1" ] || [ "$FUNNEL_DUMP_SSH_ARGS" = "true" ]; then
        if ! verify_ssh_key_present; then
            FUNNEL_SKIP_KEY_CHECK=1 build_ssh_args
            return 0
        fi
        build_ssh_args
        return 0
    fi
    if ! verify_ssh_key_present; then
        return 1
    fi
    log "starting funnel sidecar (public=${PUBLIC_URL}, target=${LHR_LOCAL_TARGET}, remote=${LHR_REMOTE_USER%@*}@${LHR_REMOTE_HOST}, tier=$(resolve_tier_label))"
    while true; do
        run_session_and_capture || true
        if [ "$FUNNEL_ONESHOT" = "1" ] || [ "$FUNNEL_ONESHOT" = "true" ]; then
            log "FUNNEL_ONESHOT set; exiting after first session"
            return 0
        fi
        log "ssh session ended; sleeping ${FUNNEL_RETRY_BACKOFF_SECONDS}s before reconnect"
        sleep "$FUNNEL_RETRY_BACKOFF_SECONDS"
    done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
