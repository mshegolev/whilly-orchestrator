---
title: План — OpenCode backend + Docker multi-container
type: implementation-plan
created: 2026-04-20
status: draft v1 · awaiting review
audience: RU only
related:
  - adr/ADR-004-claude-cli-subprocess-vs-sdk.md
  - ROADMAP.md
  - INDEX.md
---

# Имплементационный план: OpenCode backend + Docker контейнеризация

**Статус:** draft v1 · pending implementation
**Целевые milestone:** v3.2.0 (Phase 1 OpenCode) → v3.3.0 (Phase 2 Docker multi-container)
**Estimated total effort:** ~24 hours (Phase 1: 10h, Phase 2: 14h)
**Зафиксированные пользователем решения:**
- Default backend = `claude`, OpenCode = opt-in via `--agent opencode`.
- Shared state для multi-container = file lock на shared volume (MVP).
- Permissions OpenCode = оба варианта: `--dangerously-skip-permissions` флаг в CI/Docker + `.opencode/opencode.json` для команды.
- Docker images публикуются в **ghcr.io/mshegolev/whilly**.

---

## Section A — Research summary

### A.1 OpenCode (выбранный кандидат)

Канонический проект — **`sst/opencode`** (домен `opencode.ai`, ранее инкубирован командой SST/Serverless Stack, ныне самостоятельный open-source). По данным апреля 2026: ~95K-140K GitHub stars, 850+ контрибьюторов, активный релизный цикл. Безусловный лидер среди open-source terminal coding-агентов (для сравнения: OpenHands ~68K, Cline ~59K).

**Ключевые свойства, релевантные whilly:**

| Свойство | Значение | Маппинг на текущий Claude flow |
|---|---|---|
| Non-interactive subcommand | `opencode run "<prompt>"` | Аналог `claude -p "<prompt>"` |
| JSON output | `--format json` (raw JSON events) | Аналог `claude --output-format json`, **но** формат событийный, а не финальный summary — нужен парсер на стороне whilly |
| Auto-approve permissions | `--dangerously-skip-permissions` | **Совпадает 1:1** с Claude CLI |
| Granular permissions | `permission.{edit,bash,webfetch}: allow|ask|deny` + glob (`"git *": "ask"`) в `opencode.json` | У Claude нет — `opencode.json` per-project можно коммитить рядом с `tasks.json` |
| Model selection | `--model provider/model` (например `anthropic/claude-sonnet-4-5`) | Требует **префикс провайдера** |
| Agent selection | `--agent <name>` (built-in: `Build`, `Plan`, `General`, `Explore`; custom — markdown в `~/.config/opencode/agents/`) | Уникальная фича — `Build` для исполнения, `Plan` для Decision Gate |
| MCP support | Полный (`opencode mcp add`) | Совпадает с Claude |
| Sessions / continue | `--continue`, `--session <id>`, `--fork` | У Claude нет (whilly не использует) |
| Headless server | `opencode serve` + `opencode run --attach http://localhost:4096` | Уникальная фича — устраняет cold-start MCP overhead, важна для Docker |
| Cost / token reporting в JSON | Событийные данные, точная схема не задокументирована | **Главный риск Phase 1** |
| Working directory | Не задокументирован отдельный флаг | Через `cwd=` в `subprocess.run` |
| Передача длинного prompt | Не задокументирован file-based вход | Позиционный аргумент / stdin / `--file` |

**Completion signal:** OpenCode не имеет эквивалента `<promise>COMPLETE</promise>`. Маркер сохраняем тот же — это уже наш контракт, не Claude-specific.

### A.2 Альтернативы (НЕ выбираются для MVP)

| Кандидат | Stars | Почему не сейчас |
|---|---|---|
| **OpenHands** (ex-OpenDevin) | ~68K | Heavyweight (Docker-only runtime), сложный API |
| **Aider** | ~30K+ | Не headless-friendly (REPL-first), JSON output слабый |
| **Cline** | ~59K | VSCode-extension-first, headless mode сырой |
| **Forgecode / Continue / Goose** | <30K | Нишевые |
| **Codex CLI (OpenAI)** | n/a | Не open-source |

### A.3 Compatibility verdict

**Совместимость высокая, но не bit-perfect.** Drop-in заменить **не** получится — нужен абстрактный слой `AgentBackend` поверх обоих CLI.

Основные различия требующие work:
1. `--format json` у OpenCode стримит события (не финальный JSON) → нужен новый парсер.
2. Cost/token поля документированы слабо → defensive extraction + fallback на «cost=0».
3. Model id формат отличается (`provider/model` vs голое имя) → нормализация в config.
4. Permissions в `.opencode/opencode.json` — отдельный конфиг-файл.

---

## Section B — Architecture changes

### B.1 Текущее состояние (что меняем)

В коде есть **три места**, где Claude CLI вызывается напрямую:
1. `whilly/agent_runner.py` — `run_agent`, `run_agent_async`, `_parse_claude_output`, `_claude_bin`, `_claude_permission_args`.
2. `whilly/tmux_runner.py::launch_agent` — формирует tmux wrapper с `claude_cmd ...`.
3. `whilly/decision_gate.py::_default_runner` — использует `run_agent`.

Model id жёстко вшит как `claude-opus-4-6[1m]` в `whilly/config.py` и `whilly/decision_gate.py::DEFAULT_MODEL`.

### B.2 Целевая архитектура — пакет `whilly/agents/`

```
whilly/
  agents/
    __init__.py        # фабрика get_backend(name) -> AgentBackend
    base.py            # абстрактный класс AgentBackend + AgentResult/AgentUsage
    claude.py          # ClaudeBackend (рефакторинг текущего agent_runner.py)
    opencode.py        # OpenCodeBackend (новый)
  agent_runner.py      # тонкий compat-shim: re-export AgentResult/AgentUsage + run_agent()
                       # с диспатчем по WHILLY_AGENT_BACKEND. Сохраняем чтобы НЕ ломать импорты.
```

**Контракт `AgentBackend`** (минимальный набор методов):

```
class AgentBackend(Protocol):
    name: str                                     # "claude" | "opencode"
    def build_command(prompt, model, **kw) -> list[str]: ...
    def parse_output(raw: str) -> tuple[str, AgentUsage]: ...
    def is_complete(text: str) -> bool: ...       # default: <promise>COMPLETE</promise>
    def run(prompt, model, timeout=None, cwd=None) -> AgentResult: ...
    def run_async(prompt, model, log_file=None, cwd=None) -> Popen: ...
    def collect_result(proc, log_file=None, start_time=0) -> AgentResult: ...
    def collect_result_from_file(log_file, start_time=0) -> AgentResult: ...
    def env_for_subprocess(base: dict) -> dict: ...
    def default_model() -> str: ...
```

`agent_runner.py` остаётся как фасад — все существующие импорты работают.

### B.3 Изменения в `cli.py`

1. Новый флаг `--agent {claude,opencode}` с дефолтом `claude` (env override `WHILLY_AGENT_BACKEND`).
2. Парсинг → запись в `WhillyConfig.AGENT_BACKEND`.
3. Прокидывание выбранного backend во все места, где сейчас вызывается `run_agent` / `run_agent_async`.
4. Если `--agent opencode` и `opencode` бинарник не найден → fail-fast.
5. **Prompt builder** не меняется — `<promise>COMPLETE</promise>` остаётся универсальным маркером.

### B.4 Изменения в `tmux_runner.py`

Заменить hard-coded `claude_cmd ... -p ...` на `backend.build_command(...)`. Минимально-инвазивно: добавить параметр `backend: AgentBackend` в `launch_agent`.

### B.5 Изменения в `config.py`

Новые поля:
- `AGENT_BACKEND: str = "claude"` (env `WHILLY_AGENT_BACKEND`).
- `OPENCODE_BIN: str = "opencode"` (env `WHILLY_OPENCODE_BIN`).
- `OPENCODE_SAFE: bool = False` (env `WHILLY_OPENCODE_SAFE`).
- `OPENCODE_SERVER_URL: str = ""` (env `WHILLY_OPENCODE_SERVER_URL`, опциональный shared headless server).

`MODEL` остаётся, но при `AGENT_BACKEND=opencode` интерпретируется как `provider/model` — добавить нормализатор.

### B.6 Новые / обновлённые ADR

| ADR | Действие | Содержание |
|---|---|---|
| ADR-004 | **Update** (статус: superseded-in-part) | Добавить блок «See also: ADR-013» |
| ADR-013 | **New: OpenCode backend support** | `AgentBackend` интерфейс, маппинг команд, default=claude / opt-in opencode |
| ADR-014 | **New: Docker packaging** | Multi-stage build, base image, secrets/volumes, image size targets |
| ADR-015 | **New: Multi-container coordination** | Выбор file-lock для MVP, миграция к Redis в будущем |

### B.7 Migration story

- **Default backend остаётся `claude`** во всех release-каналах. `WHILLY_AGENT_BACKEND=opencode` — opt-in.
- Старые тесты структурно не трогаем — мокают `subprocess.run`. Добавляем **новые файлы** для backend-specific тестов.
- Документация: README + README-RU обновить с разделом «Backends», TUTORIAL.md дополнить шагом «попробуй `--agent opencode`».

---

## Section C — Task decomposition (Phase 1: OpenCode swap)

| ID | Task | Files | Acceptance | Est | Deps |
|---|---|---|---|---|---|
| OC-101 | Создать пакет `whilly/agents/` (`__init__.py`, `base.py`) с `AgentBackend` Protocol | `whilly/agents/__init__.py`, `whilly/agents/base.py` | mypy/ruff clean, импорты живут | 30m | — |
| OC-102 | Извлечь Claude logic в `whilly/agents/claude.py` (рефакторинг) | `whilly/agents/claude.py` | unit-тесты Claude flow проходят | 60m | OC-101 |
| OC-103 | `whilly/agent_runner.py` переписать как compat-shim | `whilly/agent_runner.py` | `pytest -q` зелёный | 30m | OC-102 |
| OC-104 | Реализовать `OpenCodeBackend` в `whilly/agents/opencode.py`: `build_command` | `whilly/agents/opencode.py` | unit-тест с моком subprocess | 60m | OC-101 |
| OC-105 | Парсер event-stream JSON output OpenCode → `AgentResult` (defensive) | `whilly/agents/opencode.py` | unit-тест на 3 примера output | 90m | OC-104 |
| OC-106 | Эмпирически захватить sample outputs OpenCode (smoke-script) | `tests/fixtures/opencode/*.json` | 3 фикстуры, README | 45m | OC-104 |
| OC-107 | Permission CLI args mapping (`--dangerously-skip-permissions` ↔ `WHILLY_OPENCODE_SAFE`) | `whilly/agents/opencode.py` | unit-тест на оба значения env | 15m | OC-104 |
| OC-108 | Model id normalizer: `claude-opus-4-6` → `anthropic/claude-opus-4-6` | `whilly/agents/opencode.py` | unit-тест mapping | 20m | OC-104 |
| OC-109 | Добавить поля в `WhillyConfig` | `whilly/config.py` | env-loading тест | 20m | — |
| OC-110 | Фабрика `whilly.agents.get_backend(name)` | `whilly/agents/__init__.py` | unit-тест на unknown backend → ValueError | 15m | OC-101, OC-104 |
| OC-111 | CLI flag `--agent claude|opencode` в `cli.py` | `whilly/cli.py` | smoke-test выбора | 30m | OC-110 |
| OC-112 | `tmux_runner.launch_agent` принимает `backend: AgentBackend` параметром | `whilly/tmux_runner.py` | unit-тест через мок tmux | 45m | OC-110 |
| OC-113 | Decision Gate использует тот же backend (через фабрику) | `whilly/decision_gate.py` | unit-тест Decision Gate с OpenCodeBackend моком | 30m | OC-110 |
| OC-114 | Smoke-script `scripts/smoke_opencode.py` — оба backend на одной задаче | `scripts/smoke_opencode.py` | оба `is_complete=True` на простой prompt | 45m | OC-111 |
| OC-115 | ADR-004 update + ADR-013 new | `docs/workshop/adr/ADR-004-*.md`, `docs/workshop/adr/ADR-013-opencode-backend.md` | оба файла валидны | 30m | OC-111 |
| OC-116 | README + README-RU секция «Backends» | `README.md`, `README-RU.md` | bilingual coverage | 25m | OC-115 |
| OC-117 | Unit-тесты ClaudeBackend | `tests/test_agent_backend_claude.py` | ≥80% покрытие | 30m | OC-102 |
| OC-118 | Unit-тесты OpenCodeBackend | `tests/test_agent_backend_opencode.py` | ≥75% покрытие | 45m | OC-105 |
| OC-119 | Integration smoke в CI | `.github/workflows/ci.yml` | CI зелёный | 20m | OC-118 |

**Phase 1 total: ~10 hours.**

**Граф зависимостей Phase 1:**
```
OC-101 ──┬─▶ OC-102 ──▶ OC-103 ─────▶ OC-117
         │
         └─▶ OC-104 ─┬─▶ OC-105 ─▶ OC-118
                     ├─▶ OC-106
                     ├─▶ OC-107
                     └─▶ OC-108
                              │
        OC-109 ───────────────┤
                              ▼
                          OC-110 ─▶ OC-111 ─┬─▶ OC-112
                                            ├─▶ OC-113
                                            ├─▶ OC-114
                                            └─▶ OC-115 ─▶ OC-116 ─▶ OC-119
```

---

## Section D — Docker design

### D.1 Цели и not-goals

**Цели:** локальный `docker build`, запуск 2-3 контейнеров для проверки multi-worker сценария, стартовая точка для будущего деплоя.

**Not-goals:** production-ready образы (signing, vulnerability scanning), arm64+amd64 multi-arch, Kubernetes manifests, autoscaling.

### D.2 Структура образов

**Два образа** для разнесения cold/hot path:

| Image | Что внутри | Когда обновляется |
|---|---|---|
| `ghcr.io/mshegolev/whilly/runtime-base:0.1` | python:3.12-slim + git, gh, tmux, node:20, claude CLI, opencode, runtime libs | Редко (раз в месяц) |
| `ghcr.io/mshegolev/whilly/orchestrator:0.1` | FROM runtime-base + `pip install -e .` whilly + entrypoint | Каждый коммит whilly |

### D.3 Dockerfile structure (псевдо-структура)

**`docker/Dockerfile.runtime-base`** (multi-stage):

```
# stage 1: builder — node deps
FROM node:20-bookworm-slim AS node-builder
RUN npm install -g @anthropic-ai/claude-code opencode-ai

# stage 2: runtime
FROM python:3.12-slim-bookworm
- apt: git, tmux, gh, curl, ca-certificates, jq, gosu
- COPY --from=node-builder /usr/local/lib/node_modules + symlinks /usr/local/bin/{claude,opencode}
- create user `whilly` (uid 1000)
- ENTRYPOINT — пустой
```

**`docker/Dockerfile`** (orchestrator):

```
ARG BASE_TAG=0.1
FROM ghcr.io/mshegolev/whilly/runtime-base:${BASE_TAG}
WORKDIR /opt/whilly
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .
COPY whilly/ ./whilly/
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh /entrypoint.sh
USER whilly
WORKDIR /workspace                    # bind-mounted target repo
ENTRYPOINT ["/entrypoint.sh"]
CMD ["whilly"]
```

**Image size targets:** runtime-base ≤ 700 MB, orchestrator ≤ 750 MB.
**Build time:** ≤ 4 минут cold cache, ≤ 30s warm cache.

### D.4 Entrypoint поведение

- Проверить `/workspace` (target repo) и `/queue` (shared state).
- Установить `git config user.{name,email}` из env.
- Если `WHILLY_OPENCODE_SERVER_URL` пуст и `AGENT_BACKEND=opencode` — стартовать `opencode serve` в фоне.
- exec на argv (по умолчанию `whilly`).

### D.5 Volumes

| Mount | Источник | Назначение |
|---|---|---|
| `/workspace` | bind: путь к target git-репо | `tasks.json`, исходники, worktrees |
| `/queue` | named volume `whilly_queue` (shared) | Общий state, lock-файлы |
| `/logs` | named volume `whilly_logs` | Aggregated logs всех воркеров |
| `~/.config/opencode` | bind или secret | OpenCode auth + agents/skills |

### D.6 Permissions / secrets

- `ANTHROPIC_API_KEY` — env (local dev) или Docker secret.
- `GH_TOKEN` — env / bind mount `~/.config/gh/hosts.yml`.
- `OPENCODE_*` provider credentials — env / bind `~/.config/opencode/auth.json`.
- **Запрет на root** — `USER whilly`. Для bind-mount: `--user $(id -u):$(id -g)`.

### D.7 GitHub Container Registry

Workflow `.github/workflows/docker-publish.yml`:
- Триггер: push to main + tag `v*`.
- Build runtime-base + orchestrator.
- Push в `ghcr.io/mshegolev/whilly/{runtime-base,orchestrator}:{semver,latest}`.
- Permissions: `packages: write`, `contents: read`.
- Auth через built-in `GITHUB_TOKEN`.

### D.8 Какие ADR покрывают

ADR-014 (Docker packaging) — фиксирует выбор multi-stage, разделение base/orchestrator, политику secrets, ghcr.io.

---

## Section E — Multi-container coordination

### E.1 Проблема

`tasks.json` — single-writer source of truth с atomic `tempfile + os.replace`. При 2+ контейнерах с общим mount:

1. **Race на claim:** оба воркера читают `pending` и одновременно ставят `in_progress`.
2. **Конкурентные writes** перезаписывают изменения друг друга.
3. **Crash-recovery:** воркер умер с задачей `in_progress` → никто не вернёт её в `pending`.

### E.2 Решение для MVP — File lock

**Выбран file lock** (рекомендация Plan agent, подтверждена пользователем):

- Нулевая инфра — Docker Compose остаётся 3-сервисный.
- `tasks.json` остаётся single source of truth.
- Реализация: `whilly/coordination/file_lock.py` с context manager `claim_lock(plan_path, timeout=30, lease_ttl=300)`.
- TTL/lease: lock-файл `<plan_path>.lock` содержит `{"holder_id": "container-XYZ", "claimed_at": <ts>, "lease_ttl_s": 300}`. При попытке захвата проверяется `claimed_at + lease_ttl_s < now` → форсированный перехват с warning event.

### E.3 Lifecycle: как воркер берёт задачу

```
loop:
    1. acquire file lock (timeout 30s)
    2. reload TaskManager
    3. ready = get_ready_tasks()
    4. if not ready: release lock; sleep; continue
    5. task = ready[0]
    6. mark_status([task.id], "in_progress")
       — также пишем claimed_by=worker_id, claimed_at=now
    7. save() (атомарно)
    8. release lock
    9. run agent в worktree (без lock, долгий!)
   10. acquire lock; mark_status(done|failed); release lock
```

**Heartbeat:** воркер пишет timestamp в `<log_dir>/heartbeats/<worker_id>.json` каждые 30s. Sweeper при старте проверяет: задачи `in_progress` с `claimed_at + lease_ttl < now` И heartbeat файл устарел → возвращаются в `pending` с event `task.lease_expired`.

**Worker ID:** `f"{hostname}-{pid}"` или `WHILLY_WORKER_ID` env.

### E.4 Migration path к Redis (Future, НЕ в этом milestone)

Когда file lock перестанет хватать (5+ воркеров или multi-host):
1. Добавить `whilly/coordination/redis_lock.py` с тем же API (`claim_lock` context manager).
2. Фабрика `get_lock_backend(name)` (`file` | `redis`).
3. Новый ADR-016 documenting migration.
4. compose.yml добавляет `redis:` сервис.

### E.5 docker-compose.yml structure

```
services:
  whilly-worker-1:
    image: ghcr.io/mshegolev/whilly/orchestrator:0.1
    environment:
      WHILLY_WORKER_ID: worker-1
      WHILLY_AGENT_BACKEND: claude
      WHILLY_USE_TMUX: "0"
      WHILLY_USE_WORKSPACE: "1"
      WHILLY_HEADLESS: "1"
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      GH_TOKEN: ${GH_TOKEN}
      WHILLY_PLAN: /workspace/tasks.json
      WHILLY_LOG_DIR: /logs/worker-1
    volumes:
      - ${PWD}:/workspace
      - whilly_queue:/queue
      - whilly_logs:/logs

  whilly-worker-2: <copy with WHILLY_WORKER_ID=worker-2 + WHILLY_LOG_DIR=/logs/worker-2>
  whilly-worker-3: <copy with WHILLY_WORKER_ID=worker-3 + WHILLY_LOG_DIR=/logs/worker-3>

  # Опционально:
  whilly-dashboard:
    image: ghcr.io/mshegolev/whilly/orchestrator:0.1
    command: ["whilly-dashboard", "--aggregated", "/logs"]
    ports: ["8080:8080"]
    volumes:
      - whilly_logs:/logs:ro

volumes:
  whilly_queue:
  whilly_logs:
```

### E.6 Aggregated observability

- Каждый воркер пишет в `WHILLY_LOG_DIR=/logs/<worker_id>/whilly_events.jsonl`.
- Дашборд (новая команда `whilly-dashboard --aggregated /logs`) тянет JSONL из всех subdir, мерджит по timestamp.
- Альтернативно — `tail -F /logs/*/whilly_events.jsonl`.

---

## Section F — Task decomposition (Phase 2: Docker + multi-container)

| ID | Task | Files | Acceptance | Est | Deps |
|---|---|---|---|---|---|
| DK-201 | `docker/Dockerfile.runtime-base` (multi-stage, python+node+claude+opencode+gh+git+tmux) | `docker/Dockerfile.runtime-base` | `docker build` успешен; `docker run` проверяет `claude --version && opencode --version && gh --version` | 90m | Phase 1 done |
| DK-202 | `docker/Dockerfile` (orchestrator, FROM runtime-base) | `docker/Dockerfile` | `docker build` ≤ 30s warm cache | 30m | DK-201 |
| DK-203 | `docker/entrypoint.sh` | `docker/entrypoint.sh` | unit-проверка через `docker run` с разными CMD | 30m | DK-202 |
| DK-204 | `.dockerignore` (исключить worktrees, workspaces, logs, __pycache__, tests, docs) | `.dockerignore` | image size ≤ 750 MB | 10m | DK-202 |
| DK-205 | Модуль `whilly/coordination/__init__.py` + `file_lock.py` (claim_lock context manager с TTL) | `whilly/coordination/file_lock.py` | unit-test: 2 потока конкурируют, TTL exhausted → форс-захват | 90m | — |
| DK-206 | Hook `claim_lock` вокруг `TaskManager.save` / `mark_status` в `cli.py::run_plan` | `whilly/cli.py` | manual test: 2 параллельных процесса не дублируют claim | 60m | DK-205 |
| DK-207 | Поля `claimed_by`, `claimed_at`, `lease_ttl_s` в `Task` (+ migration: дефолты при загрузке старого `tasks.json`) | `whilly/task_manager.py` | backward-compat tests | 45m | — |
| DK-208 | Heartbeat writer (`whilly/coordination/heartbeat.py`) | `whilly/coordination/heartbeat.py` | unit-test: файл обновляется ≥ 1 раз за `interval`+1s | 45m | DK-205 |
| DK-209 | Lease-expiration sweeper | `whilly/cli.py`, `whilly/coordination/heartbeat.py` | unit-test: симулируем мёртвого воркера | 60m | DK-208 |
| DK-210 | `WHILLY_WORKER_ID` env (config + использование в claim/heartbeat) | `whilly/config.py`, `whilly/coordination/*` | env-test | 15m | DK-208 |
| DK-211 | `docker-compose.yml` с 3 worker сервисами + named volumes | `docker-compose.yml` | `docker compose up` поднимает 3 контейнера | 45m | DK-203, DK-210 |
| DK-212 | Aggregated dashboard mode `whilly-dashboard --aggregated <dir>` | `whilly/dashboard.py` или `whilly/aggregated_dashboard.py` | manual smoke на 3 контейнерах | 90m | DK-211 |
| DK-213 | Smoke-сценарий `examples/multi-container/` с `tasks.json` на 5 простых задач | `examples/multi-container/{tasks.json,README.md,run.sh}` | все 5 задач становятся `done`, ноль дубликатов | 60m | DK-211 |
| DK-214 | E2E test: `tests/test_multi_worker_coordination.py` | `tests/test_multi_worker_coordination.py` | green в CI на ubuntu-latest | 90m | DK-213 |
| DK-215 | ADR-014 (Docker packaging) | `docs/workshop/adr/ADR-014-docker-packaging.md` | валиден, ссылается на Dockerfile | 30m | DK-204 |
| DK-216 | ADR-015 (Multi-container coordination) | `docs/workshop/adr/ADR-015-multi-container-coordination.md` | описан выбор file-lock, миграция к Redis | 30m | DK-214 |
| DK-217 | TUTORIAL.md секция «Run whilly in Docker» (single + multi container) | `docs/workshop/TUTORIAL.md` | проходится за ≤15 минут на чистой машине | 45m | DK-216 |
| DK-218 | README/README-RU обновить с раздел «Docker» | `README.md`, `README-RU.md` | bilingual coverage | 15m | DK-217 |
| DK-219 | CI workflow `.github/workflows/docker-publish.yml` (build + push в ghcr.io) | `.github/workflows/docker-publish.yml` | CI зелёный, образы доступны на ghcr.io после tag | 45m | DK-204 |
| DK-220 | Documentation: `docs/workshop/INDEX.md` обновить | `docs/workshop/INDEX.md` | ссылки live | 10m | DK-217 |

**Phase 2 total: ~14 hours.**

**Граф зависимостей Phase 2:**
```
DK-201 ─▶ DK-202 ─▶ DK-203 ─┐
                  └─▶ DK-204 │
                             ▼
DK-205 ─┬─▶ DK-206           │
        ├─▶ DK-208 ─▶ DK-209 │
        └─▶ DK-207           │
                  └─▶ DK-210 ┤
                             ▼
                          DK-211 ─┬─▶ DK-212
                                  ├─▶ DK-213 ─▶ DK-214
                                  │
                          DK-215 ─┤
                          DK-216 ─┤
                          DK-217 ─▶ DK-218
                          DK-219 ─┤
                          DK-220 ─┘
```

---

## Section G — Risks & open questions

### G.1 Технические риски

| Риск | Вероятность | Импакт | Mitigation |
|---|---|---|---|
| **OpenCode `--format json` schema нестабилен или неполон** (cost не репортится) | Высокая | Средний | OC-106 — фикстуры реальных outputs; defensive parser с fallback; warning в JSONL когда cost не извлечён |
| OpenCode `--dangerously-skip-permissions` не покрывает MCP-tools | Средняя | Средний | Шаблон `.opencode/opencode.json` коммитим в `examples/multi-container/`; документируем в TUTORIAL |
| Model id mapping (`anthropic/...` префикс) ломает совместимость env `WHILLY_MODEL` | Высокая | Низкий | Normalizer (OC-108) с fallback логикой |
| Docker bind-mount worktrees ломаются на macOS (file watcher / permissions) | Средняя | Средний | Документируем `--user $(id -u):$(id -g)`; альтернатива — git clone внутри контейнера |
| File lock на NFS / сетевой ФС не атомарен (`fcntl.flock`) | Низкая (локально) / Высокая (на shared cloud volume) | Высокий для cloud | В ADR-015 явно: «MVP только локальный bind или single-host named volume; для multi-host — мигрировать на Redis» |
| Container overhead: cold start `opencode serve` MCP добавляет 5-15s | Высокая | Низкий | `opencode serve` в entrypoint, `opencode run --attach` в worker loop |
| 3 контейнера × `claude` CLI = 3 параллельных API-вызова → rate limit / billing surprise | Средняя | Высокий | Выставлять `WHILLY_BUDGET_USD` per-container строже; aggregated bumper (Phase 3) |
| Tmux недоступен внутри контейнера | Высокая | Низкий | В Docker `WHILLY_USE_TMUX=0` всегда |

### G.2 Open questions (RESOLVED после kickoff)

1. ✅ **Default backend.** `claude` остаётся default, `opencode` — opt-in.
2. ⏳ **OpenCode binary install в Docker.** `npm install -g opencode-ai` или официальный `curl -fsSL https://opencode.ai/install | bash`? Версионирование — отложено в Phase 2 (DK-201).
3. ✅ **Permissions для OpenCode.** Оба варианта: флаг в CI/Docker + `.opencode/opencode.json` для команды.
4. ⏳ **Agent override для Decision Gate.** Использовать встроенный OpenCode `Plan` agent? Отложено в OC-113.
5. ✅ **Shared state.** File-lock как MVP, миграция к Redis в ADR-015.
6. ⏳ **Один `tasks.json` на всех или per-worker сегментирование?** Один общий с claim-lock (план выше предполагает это).
7. ⏳ **Worktree base path внутри Docker.** Принимаем что lock закрывает race; подтверждается в DK-211.
8. ✅ **Регистрация образов.** `ghcr.io/mshegolev/whilly`, public.

### G.3 Что НЕ должно быть в первом milestone

- Kubernetes manifests / Helm chart.
- Distributed lock через сеть (Redlock, etcd, Consul).
- Autoscaling воркеров по очереди задач.
- Multi-arch Docker images (arm64 для Apple Silicon native).
- Аutomated cost aggregation across containers.
- Streaming JSONL events в shared store (Loki / OpenSearch).
- Container security hardening (signing/SBOM/scanning).
- OpenCode-only режим без Claude.
- Replacement `opencode` ↔ Claude на лету в одном run.

---

## Section H — Validation strategy

### H.1 Phase 1: подтверждение работы OpenCode backend

**Smoke-test протокол (manual, 15 минут):**

1. Установить OpenCode локально: `brew install sst/tap/opencode` либо `npm i -g opencode-ai`.
2. `opencode auth login anthropic` (проверить `ANTHROPIC_API_KEY`).
3. Создать минимальный `tasks.json` с 1 задачей: «Создать файл `hello.txt` со строкой 'whilly opencode smoke', выдать `<promise>COMPLETE</promise>`».
4. Запустить `WHILLY_AGENT_BACKEND=opencode whilly tasks.json`.
5. **Pass criteria:** задача → `done`, файл создан, JSONL содержит `agent.complete` event, cost > 0 (или явно `cost_unknown`-warning event).
6. Повторить с `WHILLY_AGENT_BACKEND=claude` и сравнить: оба доходят до `done`, разница в cost ≤ 30%.
7. Скрипт `scripts/smoke_opencode.py` (OC-114) автоматизирует пп. 3-6.

**Unit-tests (CI):** OC-117, OC-118 — мок subprocess, проверка command building + parsing на фикстурах.

### H.2 Phase 2: подтверждение multi-container coordination

**Test setup (10 минут):**

1. `docker compose up --build` — 3 worker'а стартуют.
2. Положить `examples/multi-container/tasks.json` с 5 задачами.
3. Наблюдать `tail -F /logs/*/whilly_events.jsonl`.

**Pass criteria:**

| Метрика | Целевое значение |
|---|---|
| Все 5 задач → `done` | 5/5 |
| Каждая задача claim'нута ровно одним воркером | exactly 1 за task.id |
| `task.dispatched` ≤ `task.completed` (нет дублей) | inv ≥ 1 |
| Воркеры распределили нагрузку (ни один >80%) | ≤ 0.8 × 5 = 4 |
| Симуляция краха: `docker kill whilly-worker-2` → задача возвращается в `pending` за ≤ `lease_ttl_s` | recovery ≤ 6 минут |

**Automated:** `tests/test_multi_worker_coordination.py` (DK-214).

### H.3 Phase 2: overhead замер (Docker vs native)

| Сценарий | Замер |
|---|---|
| Native: `whilly tasks.json` на host | T_native |
| Docker single: `docker run whilly/orchestrator whilly tasks.json` | T_docker_single |
| Docker triple: `docker compose up x3 + 1 task` | T_docker_triple |

**Acceptable overhead:** `T_docker_single - T_native ≤ 10s`, `T_docker_triple - T_docker_single ≤ 5s`.

---

## Section I — Финальный TaskList после approval

После approval плана пользователем — это финальный список (19 задач) для трекинга. Группировка по фазам, внутри — топологический порядок.

### Phase 1 — OpenCode backend (10h, 11 задач)

1. **OC-101** Создать `whilly/agents/{__init__.py, base.py}` с `AgentBackend` Protocol.
2. **OC-102** Извлечь Claude logic в `whilly/agents/claude.py`.
3. **OC-103** Переписать `whilly/agent_runner.py` как compat-shim.
4. **OC-104+105+106+107+108** OpenCode backend: command builder + event-stream JSON parser + permission mapping + model normalizer + фикстуры.
5. **OC-109** Поля `WhillyConfig` (`AGENT_BACKEND`, `OPENCODE_*`).
6. **OC-110+111** Фабрика `get_backend()` + CLI flag `--agent`.
7. **OC-112** `tmux_runner.launch_agent` принимает `backend` параметром.
8. **OC-114** Smoke-script `scripts/smoke_opencode.py`.
9. **OC-115** ADR-004 update + ADR-013 new.
10. **OC-116** README/README-RU секция «Backends».
11. **OC-117+118+119** Unit-тесты обоих backend'ов + CI green.

### Phase 2 — Docker + multi-container (14h, 8 задач)

12. **DK-201+202+203+204** Dockerfile.runtime-base + Dockerfile + entrypoint + `.dockerignore`.
13. **DK-205+207+210** File-lock module + `claimed_by`/`claimed_at`/`lease_ttl_s` поля + `WHILLY_WORKER_ID`.
14. **DK-206** Hook `claim_lock` вокруг `mark_status` в `cli.py::run_plan`.
15. **DK-208+209** Heartbeat writer + lease-expiration sweeper.
16. **DK-211+213** `docker-compose.yml` (3 worker'а) + `examples/multi-container/`.
17. **DK-212** Aggregated dashboard `whilly-dashboard --aggregated`.
18. **DK-214** E2E test `tests/test_multi_worker_coordination.py` + CI.
19. **DK-215+216+217+218+219+220** ADR-014 + ADR-015 + TUTORIAL Docker + README + CI docker-publish + INDEX.

---

## Главные development risks и блокирующие вопросы

1. **Schema OpenCode `--format json` не задокументирована** — самый высокий риск Phase 1. Нужно эмпирически снять 3-5 фикстур реальных runs до начала OC-105.
2. **Backwards-compat импорта `whilly.agent_runner`** — критично сохранить, иначе сломаются все тесты и `whilly_ci.py`.
3. **File lock на macOS Docker (osxfs/virtiofs) — не гарантированно атомарен.** Если тестировать кластер на Mac (вероятно, на darwin) — нужно проверить раньше, иначе MVP coordination развалится. Возможно потребуется fallback на SQLite даже для MVP.
4. **Open question #2 (installer OpenCode)** — влияет на Dockerfile.runtime-base. Решается на старте DK-201 экспериментально.
5. **Open question #4 (Decision Gate via OpenCode `Plan` agent)** — оптимизация cost, не блокирует.

---

## Sources (research)

- [OpenCode CLI documentation](https://opencode.ai/docs/cli/)
- [OpenCode Agents documentation](https://opencode.ai/docs/agents/)
- [OpenCode Configuration documentation](https://opencode.ai/docs/config/)
- [OpenCode SDK documentation](https://opencode.ai/docs/sdk/)
- [Open-Source AI Coding Agents 2026 comparison](https://wetheflywheel.com/en/guides/open-source-ai-coding-agents-2026/)
- [OpenCode vs Claude Code vs Cursor (NxCode 2026)](https://www.nxcode.io/resources/news/opencode-vs-claude-code-vs-cursor-2026)
- [Redis Distributed Locks documentation](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)

---

**Status:** draft v1 · 2026-04-20 · awaiting review and implementation in a separate work session.
