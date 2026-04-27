#!/usr/bin/env bash
# db-up.sh — boot the local Postgres for Whilly v4.0 development.
#
# Idempotent: safe to re-run. Detects `docker compose` (v2 plugin) vs the
# legacy `docker-compose` (v1) binary. Waits for the container's healthcheck
# to report `healthy` before exiting 0 so callers can immediately run
# `alembic upgrade head` or psql against localhost:5432.
#
# Usage:
#   ./scripts/db-up.sh             # start + wait for healthy
#   ./scripts/db-up.sh --recreate  # docker compose down -v, then start

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.yml"
SERVICE="postgres"
CONTAINER="whilly-postgres"
TIMEOUT_SECONDS="${WHILLY_DB_UP_TIMEOUT:-60}"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "error: ${COMPOSE_FILE} not found" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH" >&2
  echo "       install Docker Desktop (macOS) or docker.io (Linux) first" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "error: docker daemon is not reachable (is Docker Desktop running?)" >&2
  exit 1
fi

# Pick a compose CLI: prefer `docker compose` (v2 plugin), fall back to
# the standalone `docker-compose` v1/v2 binary.
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2
  exit 1
fi

if [[ "${1:-}" == "--recreate" ]]; then
  echo "==> Recreating: ${COMPOSE[*]} -f ${COMPOSE_FILE} down -v"
  "${COMPOSE[@]}" -f "${COMPOSE_FILE}" down -v
fi

echo "==> Starting Postgres: ${COMPOSE[*]} -f ${COMPOSE_FILE} up -d ${SERVICE}"
"${COMPOSE[@]}" -f "${COMPOSE_FILE}" up -d "${SERVICE}"

echo "==> Waiting up to ${TIMEOUT_SECONDS}s for ${CONTAINER} to report healthy..."
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
while :; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER}" 2>/dev/null || echo missing)"
  case "${status}" in
    healthy)
      echo "==> ${CONTAINER} is healthy."
      break
      ;;
    unhealthy)
      echo "error: ${CONTAINER} healthcheck reports unhealthy" >&2
      docker logs --tail 30 "${CONTAINER}" >&2 || true
      exit 1
      ;;
    missing)
      echo "error: container ${CONTAINER} not found after up -d" >&2
      exit 1
      ;;
  esac
  if (( $(date +%s) >= deadline )); then
    echo "error: timed out after ${TIMEOUT_SECONDS}s waiting for healthy (last status: ${status})" >&2
    docker logs --tail 30 "${CONTAINER}" >&2 || true
    exit 1
  fi
  sleep 1
done

cat <<EOF

Whilly Postgres is ready.
  DSN:     postgresql://whilly:whilly@localhost:5432/whilly
  psql:    psql -h localhost -p 5432 -U whilly -d whilly   # password: whilly
  stop:    ${COMPOSE[*]} -f docker-compose.yml down
  reset:   ${COMPOSE[*]} -f docker-compose.yml down -v     # drops the volume

Next: alembic upgrade head    (TASK-007 once migrations land)
EOF
