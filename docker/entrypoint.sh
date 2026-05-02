#!/usr/bin/env bash
# Whilly demo image entrypoint.
#
# Один образ — две роли. Первый аргумент выбирает что запустить.
#
#   whilly-entrypoint control-plane   # FastAPI + alembic upgrade head
#   whilly-entrypoint worker          # whilly-worker (HTTP-клиент)
#   whilly-entrypoint migrate         # only `alembic upgrade head`
#   whilly-entrypoint shell           # bash для отладки внутри контейнера
#   whilly-entrypoint <other>         # exec <other> $@ — чтобы можно было `docker run … whilly --version`
set -euo pipefail

ROLE="${1:-control-plane}"
shift || true

log() { printf '\033[1;34m[entrypoint]\033[0m %s\n' "$*" >&2; }

# ─── Truthiness helper for env-var feature flags ─────────────────────────────
# Recognises 1 / true / yes / on (case-insensitive) as truthy. Empty / unset
# and everything else (including 0 / false / no / off) are falsy. Centralised
# so future flags (WHILLY_INSECURE, WHILLY_USE_CONNECT_FLOW, ...) share the
# exact same rules — VAL-M1-ENTRYPOINT-901 asserts the rule set explicitly.
is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_db() {
  # asyncpg сам ретраится, но alembic — нет. Подождём, пока БД станет доступной,
  # прежде чем гонять миграции.
  python - <<'PY'
import os, sys, time, asyncio
import asyncpg

dsn = os.environ.get("WHILLY_DATABASE_URL")
if not dsn:
    sys.stderr.write("WHILLY_DATABASE_URL is not set\n")
    sys.exit(2)

async def main():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn)
            await conn.execute("SELECT 1")
            await conn.close()
            return 0
        except Exception as exc:
            sys.stderr.write(f"db not ready: {exc}\n")
            await asyncio.sleep(1)
    sys.stderr.write("timed out waiting for postgres\n")
    return 1

sys.exit(asyncio.run(main()))
PY
}

case "$ROLE" in
  control-plane)
    log "role=control-plane"
    : "${WHILLY_DATABASE_URL:?WHILLY_DATABASE_URL is required}"
    : "${WHILLY_WORKER_BOOTSTRAP_TOKEN:?WHILLY_WORKER_BOOTSTRAP_TOKEN is required}"
    # NOTE: WHILLY_WORKER_TOKEN is intentionally NOT required for the
    # control-plane role anymore. It is the legacy *worker* bearer (FR-1.2,
    # see whilly/adapters/transport/auth.py) and the control-plane only
    # falls back to it when no per-worker bearer authenticates a request —
    # i.e. it's optional from the server's point of view. Requiring it here
    # forced m1-compose-control-plane to default the variable to a meaning-
    # less placeholder just to satisfy the gate (see docker-compose.control-
    # plane.yml). Worker-role keeps its own explicit gate below.

    log "waiting for postgres at $WHILLY_DATABASE_URL"
    wait_for_db

    log "applying alembic migrations"
    alembic upgrade head

    log "starting control plane on 0.0.0.0:8000"
    # Don't use `uvicorn --factory` directly: create_app(pool) requires the
    # pool to be passed in, and uvicorn factory-mode can't inject async-
    # constructed args. control_plane.py opens the pool, calls create_app,
    # and runs uvicorn.Server in-process — same shape as the v4 integration
    # tests, but production-shaped (no testcontainers).
    exec python /opt/whilly/docker/control_plane.py "$@"
    ;;

  worker)
    log "role=worker"
    : "${WHILLY_CONTROL_URL:?WHILLY_CONTROL_URL is required}"
    : "${WHILLY_PLAN_ID:?WHILLY_PLAN_ID is required}"

    if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
      printf 'whilly worker: TAILSCALE_AUTHKEY is no longer used (Tailscale was removed in the localhost.run pivot 2026-05-02). See docs/Distributed-Setup.md for the localhost.run replacement.\n' >&2
    fi

    # ─── Fail fast: explicit groq path requires GROQ_API_KEY (v4.4.2) ────
    # Default since feature m1-opencode-big-pickle-default: WHILLY_CLI=opencode
    # + WHILLY_MODEL=opencode/big-pickle (zero-key, anonymous, free on
    # OpenCode Zen). Empty / unset WHILLY_MODEL is the zero-key path and
    # MUST NOT trigger this guard. Only when the operator explicitly opts
    # into Groq via WHILLY_MODEL=groq/... does a missing GROQ_API_KEY
    # become a fail-fast condition.
    # Mirrors whilly.cli.worker.check_opencode_groq_credentials.
    if [[ "$(printf '%s' "${WHILLY_CLI:-}" | tr '[:upper:]' '[:lower:]')" == "opencode" ]]; then
      _whilly_model_norm="${WHILLY_MODEL:-}"
      _is_groq=0
      if [[ -n "${_whilly_model_norm}" ]] \
         && [[ "${_whilly_model_norm}" == groq/* || "${_whilly_model_norm}" == GROQ/* ]]; then
        _is_groq=1
      fi
      if [[ "${_is_groq}" == "1" && -z "${GROQ_API_KEY:-}" ]]; then
        printf 'whilly worker: GROQ_API_KEY is required when WHILLY_MODEL=groq/... (or unset WHILLY_MODEL to use the zero-key opencode/big-pickle default). See https://console.groq.com to obtain a free key.\n' >&2
        exit 2
      fi
      unset _whilly_model_norm _is_groq
    fi

    # ─── Auto-pick LLM model based on container resources ────────────────
    # Если оператор задал LLM_PROVIDER (groq/openrouter/cerebras/gemini/
    # ollama/claude) но НЕ зафиксировал LLM_MODEL — подбираем модель под
    # cgroup-лимиты текущего контейнера. На больших машинах включается
    # тяжёлая модель, на тонких — лёгкая. Для cloud-провайдеров это
    # экономит free-tier rate-limit'ы; для локальной Ollama —
    # принципиально, иначе OOM.
    #
    # Жёсткий escape-hatch: если LLM_MODEL уже выставлена — picker не
    # вызывается, пропускаем этот блок целиком.
    if [[ -n "${LLM_PROVIDER:-}" && -z "${LLM_MODEL:-}" ]]; then
      log "auto-picking LLM_MODEL for provider=$LLM_PROVIDER"
      if picked="$(python /opt/whilly/docker/llm_resource_picker.py "$LLM_PROVIDER" --verbose 2>&1 1>/tmp/.picked_model)"; then
        LLM_MODEL="$(cat /tmp/.picked_model)"
        export LLM_MODEL
        # diagnostics из picker'а попадают в наш stderr через --verbose
        printf '%s\n' "$picked" >&2
        log "auto-picked LLM_MODEL=$LLM_MODEL"
      else
        log "WARNING: llm_resource_picker failed, shim will use its own fallback"
        printf '%s\n' "$picked" >&2
      fi
    fi

    # Дадим control-plane время подняться. compose `depends_on: condition: service_healthy`
    # обычно справляется, но если control-plane стартует с длинной миграцией —
    # лучше подождать /health тут тоже.
    log "waiting for control plane at $WHILLY_CONTROL_URL/health"
    deadline=$(( $(date +%s) + 60 ))
    until curl -sf "$WHILLY_CONTROL_URL/health" >/dev/null 2>&1; do
      if (( $(date +%s) >= deadline )); then
        log "control plane did not become ready in 60s"
        exit 2
      fi
      sleep 1
    done
    log "control plane is up"

    # ─── M1: opt-in `whilly worker connect` register-then-exec flow ──────────
    # When WHILLY_USE_CONNECT_FLOW is truthy (1/true/yes/on, case-insensitive),
    # delegate the entire register-and-launch dance to `whilly worker connect`.
    # That subcommand validates the URL (scheme guard incl. --insecure rule),
    # registers via bootstrap, persists the bearer in the OS keychain (or
    # chmod-600 fallback), and execvp's into `whilly-worker` — so we just
    # `exec` it and let the kernel propagate the exit code on any failure
    # (VAL-M1-ENTRYPOINT-002 / -005 / -006 / -901 / -902).
    #
    # Default (unset / empty / 0 / false / no / off): keep the legacy
    # bash-awk register path so v4.3.1-shaped containers stay byte-equivalent
    # on stderr/stdout up to timestamps (VAL-M1-ENTRYPOINT-001 / -003).
    #
    # Connect-CLI vs worker-runtime arg split (POSIX `--` sentinel)
    # -------------------------------------------------------------
    # `whilly worker connect`'s argparse only accepts connect-CLI args
    # (URL, --bootstrap-token, --plan, --hostname, --insecure,
    # --no-keychain, --keychain-service). Worker-runtime args
    # (--once, --worker-id, --heartbeat-interval, --max-iterations)
    # belong to the *exec'd* `whilly-worker` binary, not to connect's
    # argparse. We construct connect-CLI args ourselves from the
    # WHILLY_* env vars and forward any positional `"$@"` extras after
    # a literal `--` so they land on the worker loop, not on connect.
    # See `whilly.cli.worker.run_connect_command` for the matching
    # parser-side handling.
    if is_truthy "${WHILLY_USE_CONNECT_FLOW:-}"; then
      : "${WHILLY_WORKER_BOOTSTRAP_TOKEN:?WHILLY_WORKER_BOOTSTRAP_TOKEN is required when WHILLY_USE_CONNECT_FLOW is enabled}"
      log "using connect flow (WHILLY_USE_CONNECT_FLOW=${WHILLY_USE_CONNECT_FLOW})"
      connect_argv=(
        whilly worker connect "$WHILLY_CONTROL_URL"
        --bootstrap-token "$WHILLY_WORKER_BOOTSTRAP_TOKEN"
        --plan "$WHILLY_PLAN_ID"
        --hostname "$(hostname)"
      )
      # WHILLY_INSECURE follows the same truthiness rules and forwards
      # --insecure to the connect command. Without it, plain HTTP to a
      # non-loopback control-plane URL is rejected before any HTTP call —
      # that's the explicit assertion in VAL-M1-ENTRYPOINT-006.
      if is_truthy "${WHILLY_INSECURE:-}"; then
        connect_argv+=(--insecure)
      fi
      # NB: positional `"$@"` extras pass through to the post-exec
      # `whilly-worker` argv via the `--` sentinel — connect.py splits
      # on the first `--` and appends everything after it to the worker
      # invocation. `whilly worker connect` ultimately execvp's into
      # `whilly-worker`, so we lose the entrypoint shell here
      # intentionally — failures propagate as the kernel-level exit code
      # of the child, never swallowed by us.
      exec "${connect_argv[@]}" -- "$@"
    fi

    # ─── Legacy register-then-exec path (default, v4.3.1 behavior) ───────────
    # Worker role still requires WHILLY_WORKER_TOKEN unless the connect-flow
    # branch above handled identity acquisition. The legacy path also accepts
    # WHILLY_WORKER_BOOTSTRAP_TOKEN as a fallback (auto-register at startup):
    #
    #   * WHILLY_WORKER_TOKEN set     → use it, skip register.
    #   * WHILLY_WORKER_TOKEN unset   → register via bootstrap, parse bearer
    #     out of the `key: value` stdout shape (the same shape the new connect
    #     flow uses — see whilly/cli/worker.py::run_register_command).
    #
    # `docker compose up --scale worker=N` relies on the bootstrap branch so
    # each replica gets its own worker_id without PK collisions in the workers
    # table.
    if [[ -z "${WHILLY_WORKER_TOKEN:-}" ]]; then
      : "${WHILLY_WORKER_BOOTSTRAP_TOKEN:?need WHILLY_WORKER_TOKEN or WHILLY_WORKER_BOOTSTRAP_TOKEN to bootstrap}"
      log "registering via bootstrap token (hostname=$(hostname))"
      register_out="$(whilly worker register \
          --connect "$WHILLY_CONTROL_URL" \
          --bootstrap-token "$WHILLY_WORKER_BOOTSTRAP_TOKEN" \
          --hostname "$(hostname)")"
      WHILLY_WORKER_ID="$(printf '%s\n' "$register_out" | awk -F': ' '/^worker_id:/ {print $2}')"
      WHILLY_WORKER_TOKEN="$(printf '%s\n' "$register_out" | awk -F': ' '/^token:/ {print $2}')"
      if [[ -z "$WHILLY_WORKER_ID" || -z "$WHILLY_WORKER_TOKEN" ]]; then
        log "failed to parse register output:"
        printf '%s\n' "$register_out" >&2
        exit 2
      fi
      export WHILLY_WORKER_ID WHILLY_WORKER_TOKEN
      log "registered worker_id=$WHILLY_WORKER_ID"
    else
      log "using pre-supplied WHILLY_WORKER_TOKEN (skipping register)"
    fi

    exec whilly-worker \
      --connect "$WHILLY_CONTROL_URL" \
      --token "$WHILLY_WORKER_TOKEN" \
      --plan "$WHILLY_PLAN_ID" \
      "$@"
    ;;

  migrate)
    log "role=migrate (alembic upgrade head only)"
    : "${WHILLY_DATABASE_URL:?WHILLY_DATABASE_URL is required}"
    wait_for_db
    exec alembic upgrade head
    ;;

  shell)
    exec bash "$@"
    ;;

  *)
    # Любая другая команда — просто exec'нем её. Полезно для:
    #   docker compose run control-plane whilly --version
    #   docker compose run control-plane whilly plan show demo
    exec "$ROLE" "$@"
    ;;
esac
