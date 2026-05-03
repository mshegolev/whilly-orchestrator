#!/usr/bin/env bash
#
# workshop-demo.sh — one-shot runner для презентационного демо Whilly v4.
#
# Запускает полный сценарий из DEMO-CHECKLIST.md:
#   1. Собирает локальный образ whilly-demo:latest (через `docker build`,
#      чтобы не упереться в требование buildx >= 0.17 у `docker compose build`).
#   2. Поднимает postgres + control-plane, ждёт healthcheck.
#   3. Импортирует план examples/demo/parallel.json (2 независимых задачи).
#   4. Поднимает N реплик воркеров (по умолчанию N=2) с WHILLY_PLAN_ID=parallel.
#      Каждый воркер регистрируется через bootstrap-token и получает свой ID.
#   5. Ловит «money frame» — момент, когда обе задачи в CLAIMED у разных
#      воркеров (доказательство параллельности).
#   6. Ждёт, пока обе задачи перейдут в DONE.
#   7. Печатает audit-log из таблицы events.
#
# Скрипт идемпотентен: безопасно перезапускать. Если стек уже поднят —
# `docker compose up` это no-op для целевого состояния.
#
# Флаги:
#   --skip-build         не пересобирать образ (ускоряет re-run)
#   --keep-running       не тушить стек в конце (для ручного исследования)
#   --workers N          сколько реплик воркера запускать (default: 2)
#   --plan <slug>        какой план использовать (default: parallel)
#   --plan-file <path>   путь к JSON плана (default: examples/demo/parallel.json)
#   --cli <agent>        agentic CLI с sub-agents/skills/MCP (рекомендуется):
#                          stub          — fake_claude_demo.sh, без LLM (default)
#                          claude-code   — Anthropic CLI, нужен ANTHROPIC_API_KEY
#                          opencode      — open-source CLI, любой provider
#                          gemini        — Google CLI, free 1500 req/day
#                          codex         — OpenAI CLI (gpt-5.x), нужен OPENAI_API_KEY
#                        agentic CLI'и сами умеют read/write файлов, исполнять
#                        bash, держать sub-agents и skills (~/.claude/skills,
#                        .opencode/agent, ~/.codex/skills), MCP-серверы. Это
#                        «настоящие» кодинг-агенты в контейнере воркера.
#
#   --llm <provider>     raw OpenAI-API без agentic capabilities (fallback):
#                          stub       — alias to --cli stub
#                          groq       — нужен GROQ_API_KEY
#                          openrouter — нужен OPENROUTER_API_KEY
#                          cerebras   — нужен CEREBRAS_API_KEY
#                          gemini     — нужен GEMINI_API_KEY
#                          ollama     — нужен запущенный ollama на хосте
#                          claude     — нужен ANTHROPIC_API_KEY (платный)
#                        --llm даёт «модель в вакууме» — пишет ответ, но
#                        НЕ умеет file-tools / sub-agents / skills. Полезно
#                        чтобы быстро увидеть как whilly state-machine
#                        реагирует на реальные ответы LLM, без полной
#                        agentic-стеки. Для production workflow всегда
#                        предпочтительнее --cli.
#                        Модель подбирается автоматически под cgroup-лимиты
#                        контейнера (см. docker/llm_resource_picker.py).
#                        Override модели: LLM_MODEL=...
#   --tier <tier>        принудительный tier подбора модели (tiny|small|medium|large)
#   --no-color           отключить ANSI-цвета (для логов CI / pipe в файл)
#   --debug              `set -x`
#   -h | --help          справка
#
# Переменные окружения:
#   WHILLY_IMAGE_TAG=<x.y.z>
#                        Опт-ин: использовать published `mshegolev/whilly:<x.y.z>`
#                        вместо локальной сборки `whilly-demo:latest`. Скрипт
#                        проверит manifest в registry, сделает `docker pull`
#                        и пробросит образ в docker-compose.demo.yml через
#                        WHILLY_IMAGE_TAG_REF. Unset → прежнее поведение
#                        (`docker build` + локальный `whilly-demo:latest`).
#                        Пример: `WHILLY_IMAGE_TAG=4.4.1 bash workshop-demo.sh --cli stub`.

set -euo pipefail

# ─── Defaults / arg parsing ──────────────────────────────────────────────────
SKIP_BUILD=0
KEEP_RUNNING=0
WORKERS=2
PLAN_ID="parallel"
PLAN_FILE="examples/demo/parallel.json"
LLM_BACKEND=""    # выставляется через --llm; если выбран --cli, остаётся пустым
CLI_BACKEND=""    # выставляется через --cli
TIER_OVERRIDE=""
USE_COLOR=1
DEBUG=0

usage() {
  sed -n '1,/^set -euo/p' "$0" | grep -E '^# ?' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)    SKIP_BUILD=1; shift ;;
    --keep-running)  KEEP_RUNNING=1; shift ;;
    --workers)       WORKERS="${2:?--workers needs a number}"; shift 2 ;;
    --plan)          PLAN_ID="${2:?--plan needs a slug}"; shift 2 ;;
    --plan-file)     PLAN_FILE="${2:?--plan-file needs a path}"; shift 2 ;;
    --cli)           CLI_BACKEND="${2:?--cli needs an agent name}"; shift 2 ;;
    --llm)           LLM_BACKEND="${2:?--llm needs a provider}"; shift 2 ;;
    --tier)          TIER_OVERRIDE="${2:?--tier needs tiny|small|medium|large}"; shift 2 ;;
    --no-color)      USE_COLOR=0; shift ;;
    --debug)         DEBUG=1; shift ;;
    -h|--help)       usage 0 ;;
    *)               echo "unknown flag: $1" >&2; usage 1 ;;
  esac
done

# По умолчанию — stub. --cli и --llm взаимоисключающие; если оба заданы,
# --cli приоритетнее (agentic > raw model для real workflow).
if [[ -z "$CLI_BACKEND" && -z "$LLM_BACKEND" ]]; then
  CLI_BACKEND="stub"
elif [[ -n "$CLI_BACKEND" && -n "$LLM_BACKEND" ]]; then
  echo "Использовано и --cli и --llm — выбираю --cli $CLI_BACKEND (--llm игнорируется)" >&2
  LLM_BACKEND=""
fi

[[ "$DEBUG" == "1" ]] && set -x

# ─── CLI / LLM backend resolution ────────────────────────────────────────────
# --cli <agent> выставляет CLAUDE_BIN на cli_adapter.py + WHILLY_CLI=<name>.
# Adapter транслирует whilly's argv в native argv нужного CLI и парсит
# native output. Эти CLI'и тащат свои sub-agents, skills, MCP, file-tools.
configure_cli_backend() {
  case "$CLI_BACKEND" in
    stub)
      # alias на старый stub-режим
      export CLAUDE_BIN="/opt/whilly/tests/fixtures/fake_claude_demo.sh"
      ;;
    claude-code)
      : "${ANTHROPIC_API_KEY:?--cli claude-code нужен ANTHROPIC_API_KEY}"
      export CLAUDE_BIN="/opt/whilly/docker/cli_adapter.py"
      export WHILLY_CLI="claude-code"
      export ANTHROPIC_API_KEY
      # claude-code сам читает ANTHROPIC_API_KEY из env. Auth через
      # `claude login` тоже работает, но в demo проще через env.
      export LLM_PROVIDER="${LLM_PROVIDER:-claude}"  # для picker'а в entrypoint
      ;;
    opencode)
      # opencode умеет любых providers. По умолчанию — OpenRouter free
      # tier (DeepSeek, Llama-3.3-70b бесплатно).
      if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
        export OPENROUTER_API_KEY
        export OPENCODE_DEFAULT_PROVIDER="openrouter"
        export LLM_PROVIDER="${LLM_PROVIDER:-openrouter}"
      elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        export ANTHROPIC_API_KEY
        export LLM_PROVIDER="${LLM_PROVIDER:-claude}"
      elif [[ -n "${GROQ_API_KEY:-}" ]]; then
        export GROQ_API_KEY
        export LLM_PROVIDER="${LLM_PROVIDER:-groq}"
      else
        err "--cli opencode нужен один из: OPENROUTER_API_KEY (рекомендуется, free), ANTHROPIC_API_KEY, GROQ_API_KEY"
        exit 1
      fi
      export CLAUDE_BIN="/opt/whilly/docker/cli_adapter.py"
      export WHILLY_CLI="opencode"
      ;;
    gemini)
      : "${GEMINI_API_KEY:?--cli gemini нужен GEMINI_API_KEY (https://aistudio.google.com/apikey)}"
      export CLAUDE_BIN="/opt/whilly/docker/cli_adapter.py"
      export WHILLY_CLI="gemini"
      export GEMINI_API_KEY
      export LLM_PROVIDER="${LLM_PROVIDER:-gemini}"
      ;;
    codex)
      : "${OPENAI_API_KEY:?--cli codex нужен OPENAI_API_KEY (https://platform.openai.com/api-keys)}"
      export CLAUDE_BIN="/opt/whilly/docker/cli_adapter.py"
      export WHILLY_CLI="codex"
      export OPENAI_API_KEY
      export LLM_PROVIDER="${LLM_PROVIDER:-openai}"
      # Codex CLI persists session-state в ~/.codex/sessions; в demo-контейнере
      # мы выставляем --ephemeral в adapter'е, так что состояние не пишется,
      # но сам ~/.codex дир всё равно нужен (для config.toml + auth cache).
      # CODEX_HOME можно переопределить если нужен read-only mount.
      export CODEX_HOME="${CODEX_HOME:-/home/whilly/.codex}"
      ;;
    *)
      err "unknown --cli $CLI_BACKEND (expected: stub|claude-code|opencode|gemini|codex)"
      exit 1
      ;;
  esac
}

# --llm <provider> — старый raw shim (без agentic capabilities). Маппим
# в env vars, которые читает docker-compose.demo.yml и пробрасывает
# worker'у. Модель НЕ задаём — её подберёт docker/llm_resource_picker.py
# из entrypoint'а под cgroup-лимиты.
configure_llm_backend() {
  case "$LLM_BACKEND" in
    stub)
      # дефолт: fake_claude_demo.sh с задержкой 2.5s
      export CLAUDE_BIN="/opt/whilly/tests/fixtures/fake_claude_demo.sh"
      ;;
    groq)
      : "${GROQ_API_KEY:?--llm groq нужен GROQ_API_KEY (https://console.groq.com/keys)}"
      export CLAUDE_BIN="/opt/whilly/docker/llm_shim.py"
      export LLM_PROVIDER="groq"
      export LLM_BASE_URL="https://api.groq.com/openai/v1"
      export LLM_API_KEY="$GROQ_API_KEY"
      ;;
    openrouter)
      : "${OPENROUTER_API_KEY:?--llm openrouter нужен OPENROUTER_API_KEY (https://openrouter.ai/keys)}"
      export CLAUDE_BIN="/opt/whilly/docker/llm_shim.py"
      export LLM_PROVIDER="openrouter"
      export LLM_BASE_URL="https://openrouter.ai/api/v1"
      export LLM_API_KEY="$OPENROUTER_API_KEY"
      export LLM_HTTP_REFERER="${LLM_HTTP_REFERER:-https://github.com/mshegolev/whilly-orchestrator}"
      export LLM_X_TITLE="${LLM_X_TITLE:-Whilly Workshop Demo}"
      ;;
    cerebras)
      : "${CEREBRAS_API_KEY:?--llm cerebras нужен CEREBRAS_API_KEY (https://inference.cerebras.ai)}"
      export CLAUDE_BIN="/opt/whilly/docker/llm_shim.py"
      export LLM_PROVIDER="cerebras"
      export LLM_BASE_URL="https://api.cerebras.ai/v1"
      export LLM_API_KEY="$CEREBRAS_API_KEY"
      ;;
    gemini)
      : "${GEMINI_API_KEY:?--llm gemini нужен GEMINI_API_KEY (https://aistudio.google.com/apikey)}"
      export CLAUDE_BIN="/opt/whilly/docker/llm_shim.py"
      export LLM_PROVIDER="gemini"
      # Google AI Studio предоставляет OpenAI-compatible endpoint:
      export LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai"
      export LLM_API_KEY="$GEMINI_API_KEY"
      ;;
    ollama)
      # Локальная Ollama на хосте. Контейнер worker'а ходит через
      # host.docker.internal (на macOS / Docker Desktop работает из коробки;
      # на Linux нужен `--add-host=host.docker.internal:host-gateway`,
      # который docker-compose v2.6+ ставит автоматически).
      export CLAUDE_BIN="/opt/whilly/docker/llm_shim.py"
      export LLM_PROVIDER="ollama"
      export LLM_BASE_URL="${OLLAMA_BASE_URL:-http://host.docker.internal:11434/v1}"
      export LLM_API_KEY="ollama"  # Ollama не проверяет токен, но shim его требует
      export LLM_TIMEOUT="${OLLAMA_TIMEOUT:-300}"  # local inference медленнее
      ;;
    claude)
      : "${ANTHROPIC_API_KEY:?--llm claude нужен ANTHROPIC_API_KEY}"
      export CLAUDE_BIN="/opt/whilly/docker/llm_shim.py"
      export LLM_PROVIDER="claude"
      # Anthropic OpenAI-compatible endpoint:
      export LLM_BASE_URL="https://api.anthropic.com/v1"
      export LLM_API_KEY="$ANTHROPIC_API_KEY"
      ;;
    *)
      err "unknown --llm $LLM_BACKEND (expected: stub|groq|openrouter|cerebras|gemini|ollama|claude)"
      exit 1
      ;;
  esac

  if [[ -n "$TIER_OVERRIDE" ]]; then
    export LLM_TIER_OVERRIDE="$TIER_OVERRIDE"
  fi
}

# ─── Constants ───────────────────────────────────────────────────────────────
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_FILE="$REPO_ROOT/docker-compose.demo.yml"
readonly DOCKERFILE="$REPO_ROOT/Dockerfile.demo"
# WHILLY_IMAGE_TAG=<x.y.z> опт-ин на published `mshegolev/whilly:<x.y.z>`
# вместо локально-собранного `whilly-demo:latest`. WHILLY_IMAGE_TAG_REF
# экспортируется в env для docker-compose.demo.yml — там каждый
# сервис объявлен как `image: ${WHILLY_IMAGE_TAG_REF:-whilly-demo:latest}`.
# Unset → байт-в-байт прежнее поведение (`docker build` + local image).
if [[ -n "${WHILLY_IMAGE_TAG:-}" ]]; then
  IMAGE_TAG="mshegolev/whilly:${WHILLY_IMAGE_TAG}"
  export WHILLY_IMAGE_TAG_REF="$IMAGE_TAG"
else
  IMAGE_TAG="whilly-demo:latest"
fi
readonly IMAGE_TAG
# Все обращения к Postgres идут через `docker compose exec postgres psql -U ...`,
# поэтому жёсткой DSN-константы тут не держим — нет риска утечки кред в логи /
# в скриншот презентации. Если нужна host-side DSN — соберите её из env vars,
# которые задаёт docker-compose.demo.yml (POSTGRES_USER / POSTGRES_PASSWORD).
readonly CONTROL_HEALTH_URL="http://127.0.0.1:8000/health"
readonly READY_TIMEOUT=120

# ─── Pretty logging ──────────────────────────────────────────────────────────
if [[ "$USE_COLOR" == "1" && -t 1 ]]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_DIM=''; C_RESET=''
fi

step()  { printf '%s==>%s %s\n'   "$C_BLUE"   "$C_RESET" "$*"; }
ok()    { printf '%s ✓ %s%s\n'    "$C_GREEN"  "$*"       "$C_RESET"; }
warn()  { printf '%s ! %s%s\n'    "$C_YELLOW" "$*"       "$C_RESET" >&2; }
err()   { printf '%s ✗ %s%s\n'    "$C_RED"    "$*"       "$C_RESET" >&2; }
dim()   { printf '%s%s%s\n'       "$C_DIM"    "$*"       "$C_RESET"; }

# ─── Helpers ─────────────────────────────────────────────────────────────────
# Compose CLI detection: prefer `docker compose` (v2 plugin), fall back to
# legacy `docker-compose` v1 standalone binary. Same logic as scripts/db-up.sh.
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    err "neither 'docker compose' nor 'docker-compose' is available"
    exit 1
  fi
}

compose() {
  "${COMPOSE[@]}" -f "$COMPOSE_FILE" "$@"
}

compose_psql() {
  # Запустить psql внутри контейнера postgres — без необходимости иметь psql на хосте.
  compose exec -T postgres psql -U whilly -d whilly -t -A "$@"
}

require_bin() {
  command -v "$1" >/dev/null 2>&1 || { err "missing required binary: $1"; exit 1; }
}

cleanup_on_exit() {
  local rc=$?
  if (( KEEP_RUNNING )); then
    dim "(--keep-running set; стек оставлен в живых)"
    dim "Остановить вручную: ${COMPOSE[*]} -f $COMPOSE_FILE down -v"
  elif (( rc != 0 )); then
    warn "скрипт упал (rc=$rc); стек оставлен для диагностики"
    dim "Логи: ${COMPOSE[*]} -f $COMPOSE_FILE logs"
    dim "Остановить: ${COMPOSE[*]} -f $COMPOSE_FILE down -v"
  else
    step "тушим стек"
    compose down -v >/dev/null 2>&1 || true
    ok "стек остановлен"
  fi
}
trap cleanup_on_exit EXIT

# ─── 0. Pre-flight ───────────────────────────────────────────────────────────
step "pre-flight"
require_bin docker
detect_compose
ok "compose CLI: ${COMPOSE[*]}"

if [[ -n "$CLI_BACKEND" ]]; then
  step "конфигурируем agentic CLI: --cli $CLI_BACKEND"
  configure_cli_backend
  case "$CLI_BACKEND" in
    stub)    ok "stub Claude (fake_claude_demo.sh, sleep 2.5s) — без расхода токенов" ;;
    claude-code) ok "claude-code (Anthropic CLI с sub-agents, skills, MCP)" ;;
    opencode)    ok "opencode (open-source agentic CLI с sub-agents, skills, MCP)" ;;
    gemini)      ok "gemini-cli (Google CLI, sub-agents, skills, MCP, free-tier)" ;;
    codex)       ok "codex (OpenAI CLI, gpt-5.x, sub-agents, skills, MCP, plugins)" ;;
  esac
  [[ -n "$TIER_OVERRIDE" ]] && dim "  tier override: $TIER_OVERRIDE"
else
  step "конфигурируем raw LLM backend: --llm $LLM_BACKEND"
  configure_llm_backend
  if [[ "$LLM_BACKEND" == "stub" ]]; then
    ok "stub Claude (fake_claude_demo.sh, sleep 2.5s) — без расхода токенов"
  else
    ok "raw LLM: provider=$LLM_BACKEND (без sub-agents/skills — для real workflow используйте --cli)"
    [[ -n "$TIER_OVERRIDE" ]] && dim "  tier override: $TIER_OVERRIDE"
  fi
fi

if ! docker info >/dev/null 2>&1; then
  err "docker daemon не отвечает — запустите Docker Desktop / dockerd"
  exit 1
fi
ok "docker daemon отвечает"

[[ -f "$COMPOSE_FILE" ]] || { err "не найден $COMPOSE_FILE"; exit 1; }
[[ -f "$DOCKERFILE" ]]   || { err "не найден $DOCKERFILE"; exit 1; }
[[ -f "$REPO_ROOT/$PLAN_FILE" ]] || { err "не найден план $PLAN_FILE"; exit 1; }
ok "артефакты на месте"

# Снос предыдущего демо-стека: безопасно даже если ничего не было поднято.
step "сносим предыдущий demo-стек (если был)"
compose down -v >/dev/null 2>&1 || true
ok "чистая сцена"

# ─── 1. Build (или pull, если задан WHILLY_IMAGE_TAG) ────────────────────────
if [[ -n "${WHILLY_IMAGE_TAG:-}" ]]; then
  step "WHILLY_IMAGE_TAG=$WHILLY_IMAGE_TAG → используем $IMAGE_TAG (skip local build)"
  # Fail-fast: если тег опечатан, упасть ДО `compose up`, а не после.
  if ! docker manifest inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    err "образ $IMAGE_TAG недоступен в registry (docker manifest inspect failed)"
    err "проверьте https://hub.docker.com/r/mshegolev/whilly/tags"
    exit 1
  fi
  ok "manifest найден в registry"
  step "пуллим $IMAGE_TAG"
  docker pull "$IMAGE_TAG"
  ok "образ загружен: $(docker image inspect -f '{{.Id}}' "$IMAGE_TAG" | head -c 19)…"
else
  if (( SKIP_BUILD )); then
    if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
      warn "--skip-build, но $IMAGE_TAG не найден локально — собираю всё равно"
      SKIP_BUILD=0
    else
      step "пропускаем сборку (--skip-build)"
      ok "используем существующий $IMAGE_TAG"
    fi
  fi

  if (( ! SKIP_BUILD )); then
    step "собираем $IMAGE_TAG (через docker build, не compose build)"
    # Используем именно `docker build`, чтобы не упереться в требование
    # buildx >= 0.17, которое появляется в свежих compose v2.
    docker build -f "$DOCKERFILE" -t "$IMAGE_TAG" "$REPO_ROOT"
    ok "образ собран: $(docker image inspect -f '{{.Id}}' "$IMAGE_TAG" | head -c 19)…"
  fi
fi

# ─── 2. Postgres + control-plane ─────────────────────────────────────────────
step "поднимаем postgres + control-plane"
compose up -d postgres control-plane

step "ждём control-plane /health (до ${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
until curl -sf "$CONTROL_HEALTH_URL" >/dev/null 2>&1; do
  if (( $(date +%s) >= deadline )); then
    err "control-plane не поднялся за ${READY_TIMEOUT}s"
    compose logs --tail=30 control-plane >&2 || true
    exit 2
  fi
  sleep 1
done
ok "control-plane: $(curl -s "$CONTROL_HEALTH_URL")"

# ─── 3. Workers (СНАЧАЛА воркеры, потом план) ────────────────────────────────
# Стартуем воркеров ДО импорта плана: оба сразу начинают long-poll'ить
# /tasks/claim. Когда план появится в БД, оба воркера ровно в этот момент
# увидят PENDING-задачи — FOR UPDATE SKIP LOCKED разведёт их по разным
# задачам, и параллельность будет видна в "money frame".
step "поднимаем $WORKERS воркер(а/ов) с WHILLY_PLAN_ID=$PLAN_ID"
WHILLY_PLAN_ID="$PLAN_ID" compose up -d --scale "worker=$WORKERS" worker

# Ждём, пока все реплики зарегистрируются в таблице workers.
step "ждём регистрации воркеров (workers row count = $WORKERS)"
deadline=$(( $(date +%s) + 60 ))
while :; do
  count="$(compose_psql -c 'SELECT COUNT(*) FROM workers WHERE status='"'"'online'"'"';' 2>/dev/null | tr -d '[:space:]')"
  if [[ "$count" =~ ^[0-9]+$ ]] && (( count >= WORKERS )); then
    ok "зарегистрировано $count воркер(а/ов)"
    break
  fi
  if (( $(date +%s) >= deadline )); then
    err "воркеры не зарегистрировались за 60s (текущее: ${count:-?})"
    compose logs --tail=40 worker >&2 || true
    exit 3
  fi
  sleep 1
done

step "состав воркеров"
compose_psql -c "SELECT worker_id, hostname, status FROM workers ORDER BY worker_id;"

# ─── 4. Import plan (после того как воркеры стоят на long-poll'е) ────────────
step "импортируем план $PLAN_ID из $PLAN_FILE"
# Файл плана уже COPY'нут в /opt/whilly/examples/ внутри образа.
compose exec -T control-plane whilly plan import "$PLAN_FILE"
ok "план импортирован — воркеры мгновенно подхватят PENDING задачи"

step "DAG плана"
compose exec -T control-plane whilly plan show "$PLAN_ID" || true

# ─── 5. «Money frame»: ловим момент, когда обе задачи в CLAIMED ──────────────
step "ждём claim'ы (доказательство параллельности)"
deadline=$(( $(date +%s) + 60 ))
caught_parallel=0
while :; do
  # Считаем задачи в активных статусах (CLAIMED / IN_PROGRESS) и количество
  # уникальных claimed_by по плану. Если >=2 уникальных claimed_by при
  # >=2 активных задач — параллель поймана.
  read -r active uniq < <(compose_psql -c "
    SELECT COUNT(*) FILTER (WHERE status IN ('CLAIMED','IN_PROGRESS')),
           COUNT(DISTINCT claimed_by) FILTER (WHERE status IN ('CLAIMED','IN_PROGRESS'))
    FROM tasks WHERE plan_id='$PLAN_ID';" 2>/dev/null | tr '|' ' ')
  if [[ "${active:-0}" -ge 2 && "${uniq:-0}" -ge 2 ]]; then
    caught_parallel=1
    ok "поймано: $active активных задач у $uniq разных воркеров — параллель!"
    break
  fi
  # Если все уже DONE — параллель пролетела слишком быстро (stub Claude
  # отрабатывает за ~50ms). Покажем итоговый расклад.
  done_count="$(compose_psql -c "SELECT COUNT(*) FROM tasks WHERE plan_id='$PLAN_ID' AND status='DONE';" 2>/dev/null | tr -d '[:space:]')"
  if [[ "${done_count:-0}" -ge 2 ]]; then
    warn "параллель пролетела слишком быстро (stub Claude молниеносный) — показываю итоговое распределение"
    break
  fi
  if (( $(date +%s) >= deadline )); then
    warn "не дождались параллельного claim'а за 60s; показываю текущий снимок"
    break
  fi
  sleep 0.5
done

step "снимок таблицы tasks"
compose_psql -c "SELECT id, status, claimed_by, claimed_at FROM tasks WHERE plan_id='$PLAN_ID' ORDER BY id;"

# ─── 6. Wait for DONE ────────────────────────────────────────────────────────
step "ждём DONE для всех задач (до ${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
while :; do
  pending="$(compose_psql -c "SELECT COUNT(*) FROM tasks WHERE plan_id='$PLAN_ID' AND status NOT IN ('DONE','FAILED','SKIPPED');" 2>/dev/null | tr -d '[:space:]')"
  if [[ "${pending:-1}" -eq 0 ]]; then
    ok "все задачи в терминальных статусах"
    break
  fi
  if (( $(date +%s) >= deadline )); then
    warn "за ${READY_TIMEOUT}s ${pending} задач(а/и) не доехали до терминала"
    break
  fi
  sleep 1
done

# ─── 7. Final report ─────────────────────────────────────────────────────────
step "итоговый план"
compose exec -T control-plane whilly plan show "$PLAN_ID" || true

step "audit log из events"
compose_psql -c "
  SELECT task_id, event_type, created_at AS ts,
         COALESCE(detail->>'worker_id','') AS worker_id
    FROM events
   WHERE plan_id='$PLAN_ID'
   ORDER BY id;"

# ─── 7.5. Terminal-state guard (additive — fixes silent-success bug) ─────────
# Previously the demo script unconditionally exited 0 when the wait-for-DONE
# loop finished, even if the loop itself bailed on the timeout warning with
# tasks still in PENDING / CLAIMED / IN_PROGRESS. That hid worker-disabled
# regressions during cross-host runs (M1 user-testing finding
# VAL-CROSS-BACKCOMPAT-002 / VAL-M1-ENTRYPOINT-003 / VAL-M1-DEMO-008).
#
# Now: query the tasks table for anything outside the terminal set and pipe
# the result through scripts/check_demo_tasks_terminal.sh. Empty result =>
# exit 0 (backwards-compatible green CI). Non-empty => the helper prints
# each offending `<id> (status=...)` to stderr and we exit 4.
step "проверяем терминальные статусы всех seeded задач"
non_terminal_rows="$(
  compose_psql -c "
    SELECT id || '|' || status
      FROM tasks
     WHERE plan_id='$PLAN_ID'
       AND status NOT IN ('DONE','FAILED','SKIPPED')
     ORDER BY id;
  " 2>/dev/null || true
)"
if ! printf '%s\n' "$non_terminal_rows" \
     | bash "$REPO_ROOT/scripts/check_demo_tasks_terminal.sh"; then
  err "demo aborted: see stuck task list above"
  exit 4
fi
ok "все seeded задачи в терминальном статусе"

# ─── 7.6. DONE-count guard for VAL-CROSS-BACKCOMPAT-005 ─────────────────────
# Round-4 finding: even with the terminal-state guard the demo could exit 0
# with only 2/5 DONE (when the seeded plan was undersized OR tasks were
# marked FAILED/SKIPPED). VAL-CROSS-BACKCOMPAT-005 explicitly requires the
# `--cli stub` demo to drain 5 tasks DONE within 5 minutes. We re-query the
# tasks table for ALL rows and pipe into the same helper with --min-done 5;
# the helper prints a `DONE=N PENDING=N ...` summary on stdout (asserted by
# tests/integration/test_workshop_demo_drains_5_tasks.py) and exits non-zero
# if DONE < 5 even when every row is terminal.
step "проверяем что >= 5 задач DONE (VAL-CROSS-BACKCOMPAT-005)"
all_status_rows="$(
  compose_psql -c "
    SELECT id || '|' || status
      FROM tasks
     WHERE plan_id='$PLAN_ID'
     ORDER BY id;
  " 2>/dev/null || true
)"
if ! printf '%s\n' "$all_status_rows" \
     | bash "$REPO_ROOT/scripts/check_demo_tasks_terminal.sh" \
            --min-done 5 --plan "$PLAN_ID"; then
  err "demo aborted: DONE-count below 5 (see breakdown above)"
  exit 5
fi
ok "плановое количество DONE достигнуто (>= 5)"

# ─── 8. Done ─────────────────────────────────────────────────────────────────
echo
ok "демо завершено"
if (( caught_parallel )); then
  echo "${C_GREEN}Параллельность подтверждена:${C_RESET} обе задачи были у разных воркеров одновременно."
else
  echo "${C_YELLOW}Параллельность не зафиксирована «вживую»${C_RESET} — задачи короткие, stub-Claude отдаёт ответ за ~50ms."
  echo "Сделайте паузу в fake_claude.sh (sleep 3) или используйте реальный Claude, чтобы поймать middle frame на сцене."
fi

if (( KEEP_RUNNING )); then
  echo
  dim "Стек оставлен поднятым (--keep-running). Полезные команды:"
  dim "  ${COMPOSE[*]} -f $COMPOSE_FILE ps"
  dim "  ${COMPOSE[*]} -f $COMPOSE_FILE logs -f worker"
  dim "  ${COMPOSE[*]} -f $COMPOSE_FILE exec control-plane whilly dashboard --plan $PLAN_ID"
  dim "  ${COMPOSE[*]} -f $COMPOSE_FILE down -v   # когда наиграетесь"
fi
