#!/usr/bin/env bash
# Whilly funnel sidecar runtime loop (M2: m2-localhostrun-funnel-sidecar).
#
# Holds an outbound SSH reverse-tunnel against `nokey@localhost.run`
# (free anonymous tier), parses the assigned
# `https://<random>.lhr.life` URL from SSH stdout, and publishes it
# to:
#   1. Postgres `funnel_url` singleton table (primary; gated on
#      WHILLY_DATABASE_URL being set)
#   2. Shared-volume file `$FUNNEL_URL_FILE` (fallback; written via
#      tmp + atomic rename)
#
# On SSH exit (rotation / network blip / server-side close) the
# script sleeps `FUNNEL_RETRY_BACKOFF_SECONDS` and reconnects.
#
# Security:
#   * `WHILLY_DATABASE_URL` is parsed once into discrete
#     PG{HOST,PORT,USER,DATABASE} + PGPASSWORD env vars and is
#     NEVER passed to `psql` on the command line — that would leak
#     the password into `ps` output and the container's stdout
#     stream.
#   * `set -u` would trip on optional env reads; we use `set -eo
#     pipefail` instead and gate optional reads with `${VAR:-}`.
#
# Test hooks:
#   * `FUNNEL_FAKE_URL` — when set, the script bypasses real SSH
#     and emits a synthetic banner containing that URL. Used by
#     `tests/integration/test_funnel_sidecar_url_publish.py` to
#     exercise the publish path without depending on the public
#     localhost.run service.
#   * `FUNNEL_ONESHOT` — when truthy, exit after the first publish
#     instead of looping. Test fixtures set this so the container
#     terminates deterministically.

set -eo pipefail

FUNNEL_LOCAL_HOST="${FUNNEL_LOCAL_HOST:-control-plane}"
FUNNEL_LOCAL_PORT="${FUNNEL_LOCAL_PORT:-8000}"
FUNNEL_SERVER_ALIVE_INTERVAL="${FUNNEL_SERVER_ALIVE_INTERVAL:-60}"
FUNNEL_RETRY_BACKOFF_SECONDS="${FUNNEL_RETRY_BACKOFF_SECONDS:-5}"
FUNNEL_URL_FILE="${FUNNEL_URL_FILE:-/funnel/url.txt}"
FUNNEL_REMOTE_HOST="${FUNNEL_REMOTE_HOST:-localhost.run}"
FUNNEL_REMOTE_USER="${FUNNEL_REMOTE_USER:-nokey}"
FUNNEL_FAKE_URL="${FUNNEL_FAKE_URL:-}"
FUNNEL_ONESHOT="${FUNNEL_ONESHOT:-0}"

LHR_REGEX='https://[a-z0-9-]+\.lhr\.life'

log() {
    printf '[funnel %s] %s\n' "$(date -u +%FT%TZ)" "$*"
}

# Parse a postgres:// DSN into discrete PG* env vars so `psql` can
# authenticate without seeing the URL on argv.
#
# WHILLY_DATABASE_URL contains the password; passing it as
# `psql "$URL"` would leak the secret into `ps` output and into
# the stdout stream tee'd to docker logs (see SECURITY note above).
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
        log "wrote URL to postgres funnel_url table"
    else
        log "psql upsert failed (db unreachable or schema missing — migration 010 not yet applied?)"
    fi
    unset PGPASSWORD
}

publish_url() {
    local url="$1"
    log "discovered URL: $url"
    publish_to_file "$url"
    publish_to_postgres "$url"
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
    exec ssh \
        -o "ServerAliveInterval=${FUNNEL_SERVER_ALIVE_INTERVAL}" \
        -o ExitOnForwardFailure=yes \
        -o StrictHostKeyChecking=accept-new \
        -R "80:${FUNNEL_LOCAL_HOST}:${FUNNEL_LOCAL_PORT}" \
        "${FUNNEL_REMOTE_USER}@${FUNNEL_REMOTE_HOST}"
}

run_session_and_capture() {
    local published=0
    local line
    while IFS= read -r line; do
        printf '%s\n' "$line"
        if [ "$published" -eq 0 ]; then
            local match
            match=$(printf '%s' "$line" | grep -oE "$LHR_REGEX" | head -n1 || true)
            if [ -n "$match" ]; then
                publish_url "$match"
                published=1
                if [ "$FUNNEL_ONESHOT" = "1" ] || [ "$FUNNEL_ONESHOT" = "true" ]; then
                    return 0
                fi
            fi
        fi
    done < <(run_ssh_session 2>&1)
}

main() {
    log "starting funnel sidecar (local=${FUNNEL_LOCAL_HOST}:${FUNNEL_LOCAL_PORT}, remote=${FUNNEL_REMOTE_USER}@${FUNNEL_REMOTE_HOST})"
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
