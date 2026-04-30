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
    : "${WHILLY_WORKER_TOKEN:?WHILLY_WORKER_TOKEN is required}"
    : "${WHILLY_WORKER_BOOTSTRAP_TOKEN:?WHILLY_WORKER_BOOTSTRAP_TOKEN is required}"

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

    # ─── Авто-регистрация воркера ─────────────────────────────────────────────
    # Если WHILLY_WORKER_TOKEN не задан — регистрируемся через bootstrap-token,
    # получаем уникальную пару (worker_id, per-worker bearer). Это критично
    # для `docker compose up --scale worker=N`: каждая реплика регистрируется
    # сама и получает свой worker_id, никаких PK-коллизий в таблице workers.
    #
    # Если WHILLY_WORKER_TOKEN задан явно (например, оператор уже сделал
    # `whilly worker register` снаружи и пробросил токен) — пропускаем
    # регистрацию и используем то, что дали.
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
