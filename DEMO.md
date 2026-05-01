# Whilly Demo — локально и в Docker

> Гайд для презентаций и самообучения. Показывает Whilly v4 в двух режимах:
> на хост-машине одной командой и в распределённой схеме из двух
> application-контейнеров (плюс контейнер с Postgres).
>
> Английская краткая версия: [`DEMO.en.md`](DEMO.en.md).

В v4 Whilly состоит из трёх компонентов:

```
┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  Postgres 15+        │ ◄─ │  control-plane       │ ◄─ │  whilly-worker       │
│  plans/tasks/        │    │  FastAPI + asyncpg   │    │  httpx + Claude CLI  │
│  workers/events      │    │  (uvicorn :8000)     │    │  (отдельный VM)      │
└──────────────────────┘    └──────────────────────┘    └──────────────────────┘
                            │     application       │  │     application      │
                            │     контейнер №1      │  │     контейнер №2     │
                            └───────────────────────┘  └──────────────────────┘
```

В демо:

- **БД** — `postgres:15-alpine` (служебный контейнер, в логике приложения не считаем).
- **Application контейнер №1** — control-plane (FastAPI), единственный, кто знает про SQL.
- **Application контейнер №2** — `whilly-worker` (HTTP-клиент к control-plane, без SQL и без FastAPI).

---

## Содержание

1. [Подготовка](#подготовка)
2. [Сценарий A — локально на хост-машине](#сценарий-a--локально-на-хост-машине)
3. [Сценарий B — в Docker (2 application-контейнера + БД)](#сценарий-b--в-docker-2-application-контейнера--бд)
4. [Agentic CLI workflow (рекомендуемый production-режим)](#agentic-cli-workflow-рекомендуемый-production-режим)
5. [Real LLM modes (raw, без agentic capabilities)](#real-llm-modes-raw-без-agentic-capabilities)
6. [Что показывать на презентации](#что-показывать-на-презентации)
7. [FAQ и траблшутинг](#faq-и-траблшутинг)

---

## Подготовка

### Что должно быть установлено

| Что          | Локально (Сценарий A)         | Docker (Сценарий B) |
|--------------|-------------------------------|---------------------|
| Python       | 3.12+                         | не нужен (всё в образе) |
| Docker       | для Postgres                  | Docker Desktop / docker-engine + compose |
| Claude CLI   | для реальных задач (опционально) | не нужен — внутри stub |
| `psql`       | удобно для отладки            | удобно для отладки  |

> **Подсказка для презентации.** Чтобы не тратить токены реального Claude
> на сцене, демо использует stub из `tests/fixtures/fake_claude.sh`. Он
> возвращает синтетический ответ с `<promise>COMPLETE</promise>` —
> state-machine и audit-log полностью отрабатывают, но реального LLM-вызова
> нет. Для реального запуска просто переопределите `CLAUDE_BIN`.

### Артефакты, которые использует демо

| Файл                                 | Назначение |
|--------------------------------------|------------|
| `Dockerfile.demo`                    | один образ для control-plane и worker (две роли через CMD) |
| `docker/entrypoint.sh`               | dispatcher: `control-plane` / `worker` / `migrate` / `shell` |
| `docker-compose.demo.yml`            | postgres + control-plane + worker (+ опциональный seed) |
| `examples/demo/tasks.json`           | план из 4 простых задач |
| `examples/demo/PRD-demo.md`          | PRD-описание для контекста на слайде |
| `tests/fixtures/fake_claude.sh`      | stub Claude (уже в репо) |

---

## Сценарий A — локально на хост-машине

Один Postgres-контейнер + Whilly-CLI на хосте. Самый быстрый путь —
для собственных смок-тестов и первого знакомства.

### A.1 — Установка

```bash
# Из корня репозитория
python -m venv .venv
source .venv/bin/activate
pip install -e '.[all]'
```

### A.2 — Поднимаем БД

```bash
# Стартует postgres:15-alpine из существующего docker-compose.yml
./scripts/db-up.sh
# Демо-креды postgres'а — whilly/whilly (см. docker-compose.yml).
# Подставьте реальные значения если переопределяли POSTGRES_USER/POSTGRES_PASSWORD.
export WHILLY_DATABASE_URL="postgresql://${POSTGRES_USER:-whilly}:${POSTGRES_PASSWORD:-whilly}@localhost:5432/whilly"
alembic upgrade head
```

`./scripts/db-up.sh` идемпотентен: если контейнер уже поднят и healthy,
ничего не сломает.

### A.3 — Импортируем демо-план

```bash
whilly plan import examples/demo/tasks.json
whilly plan show demo
```

`whilly plan show demo` выведет ASCII-граф из 4 задач со статусами
`PENDING`. Зависимости (DEMO-003 → DEMO-001, DEMO-004 → DEMO-001+002)
будут видны в графе — это полезный «живой кадр» для слайда.

### A.4 — Запускаем «всё-в-одном»

```bash
# Worker, control-plane embedded — один процесс, asyncpg напрямую к Postgres.
# CLAUDE_BIN указывает на stub чтобы не дёргать реальный Claude.
CLAUDE_BIN="$PWD/tests/fixtures/fake_claude.sh" whilly run --plan demo --max-iterations 10
```

После завершения:

```bash
whilly plan show demo                  # все задачи в DONE
whilly dashboard --plan demo           # Rich Live TUI (q — выход)

# Audit log:
psql "$WHILLY_DATABASE_URL" -c \
  "SELECT task_id, event_type, ts FROM events WHERE plan_id='demo' ORDER BY id;"
```

### A.5 — Распределённый режим на одной машине

Чтобы прорепетировать «как в Docker, только на хосте» — поднимаем
control-plane и worker отдельно:

```bash
# Терминал 1 — control-plane (FastAPI)
export WHILLY_DATABASE_URL="postgresql://${POSTGRES_USER:-whilly}:${POSTGRES_PASSWORD:-whilly}@localhost:5432/whilly"
export WHILLY_WORKER_TOKEN=demo-bearer
export WHILLY_WORKER_BOOTSTRAP_TOKEN=demo-bootstrap
uvicorn 'whilly.adapters.transport.server:create_app' --factory --port 8000

# Терминал 2 — worker
export WHILLY_CONTROL_URL=http://127.0.0.1:8000
export WHILLY_WORKER_TOKEN=demo-bearer
export WHILLY_PLAN_ID=demo
CLAUDE_BIN="$PWD/tests/fixtures/fake_claude.sh" \
  whilly-worker --connect "$WHILLY_CONTROL_URL" \
                --token  "$WHILLY_WORKER_TOKEN" \
                --plan   "$WHILLY_PLAN_ID"
```

Это эквивалент сценария B, но без Docker. Полезно как «откатной»
вариант, если на сцене упал docker engine.

> Готовый скрипт-обёртка: [`docs/demo-remote-worker.sh`](docs/demo-remote-worker.sh)
> — делает то же самое одной командой, проверяет наличие нужных бинарников
> и в конце подтверждает запись в `events`.

---

## Сценарий B — в Docker (2 application-контейнера + БД)

Полная распределённая схема — то, что красивее всего смотрится на слайде.
**Один compose, три сервиса**: один с Postgres и два с whilly (control-plane
и worker). Логически — «БД + 2 application-контейнера».

### B.1 — Сборка образа

```bash
docker compose -f docker-compose.demo.yml build
```

Образ один (`whilly-demo:latest`), но в compose он используется в двух
сервисах с разными CMD (`control-plane` / `worker`). Это намеренно:
для презентации проще показать «один образ, две роли».

### B.2 — Старт стека

```bash
docker compose -f docker-compose.demo.yml up -d

# Проверить health:
docker compose -f docker-compose.demo.yml ps
docker compose -f docker-compose.demo.yml logs -f control-plane
```

После `up -d`:

| Сервис          | Что внутри                        | Порт хоста |
|-----------------|-----------------------------------|------------|
| `postgres`      | postgres:15-alpine                | `127.0.0.1:5432` |
| `control-plane` | uvicorn + FastAPI + alembic       | `127.0.0.1:8000` |
| `worker`        | whilly-worker, stub Claude        | —          |

`control-plane` сам прогоняет `alembic upgrade head` при старте —
никаких ручных миграций не нужно.

### B.3 — Импортируем план в БД

Самый простой способ — выполнить `whilly plan import` внутри control-plane:

```bash
docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan import examples/demo/tasks.json

docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan show demo
```

Альтернатива — поднять seed-сервис из compose (он отключён по умолчанию
через `profiles: [seed]`):

```bash
docker compose -f docker-compose.demo.yml --profile seed run --rm seed
```

### B.4 — Смотрим, как worker разбирает задачи

`worker` уже запущен и в фоне long-poll'ит control-plane по
`POST /tasks/claim`. После импорта он начнёт claim'ить задачи одну за
другой — в логах будет видно живьём:

```bash
docker compose -f docker-compose.demo.yml logs -f worker
```

Параллельно с хоста можно проверить статус через psql:

```bash
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c \
  "SELECT id, status, claimed_by FROM tasks WHERE plan_id='demo' ORDER BY id;"
```

Или (то же самое) — изнутри control-plane:

```bash
docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan show demo
```

### B.5 — Audit log в `events`

Append-only log — ключевая фича v4. Для каждой задачи будут как минимум
`CLAIM` и `COMPLETE` (или `FAIL`):

```bash
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c "
    SELECT task_id, event_type, ts
    FROM events
    WHERE plan_id='demo'
    ORDER BY id;"
```

Это часть SC-3 («Запустить второй процесс whilly-worker и увидеть полную
цепочку аудита») — и именно тот кадр, который выглядит на слайде убедительно.

### B.6 — Тушим стек

```bash
# Остановить, тома сохранить (быстрый перезапуск)
docker compose -f docker-compose.demo.yml down

# Снести вместе с томом (полный сброс БД)
docker compose -f docker-compose.demo.yml down -v
```

---

## Сценарий M1 — two-host demo (control-plane на VPS + воркер на ноутбуке)

> **Что нового в v4.4 (M1).** К существующему `docker-compose.demo.yml`
> добавлены два *additive* compose-файла: `docker-compose.control-plane.yml`
> (postgres + control-plane на VPS) и `docker-compose.worker.yml` (только
> worker, который смотрит на удалённый control-plane). Плюс новая команда
> `whilly worker connect <url>` — однострочный bootstrap для ноутбука.
> Сценарий A/B (single-host) полностью сохранён — байт-в-байт.
>
> Полный пошаговый walkthrough — в [`docs/Distributed-Setup.md`](docs/Distributed-Setup.md).
> Ниже — **минимальный demo-путь** для презентации.

### M1.1 — Поднимаем control-plane на VPS

```bash
ssh root@vps.example.com
cd /root/whilly
git checkout v4.4.0
export WHILLY_WORKER_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"

# Loopback-only (по умолчанию — Tailscale / VPN-friendly):
docker-compose -f docker-compose.control-plane.yml up -d

# ИЛИ: открыть API наружу для laptop'ов через публичный IP / LAN:
WHILLY_BIND_HOST=0.0.0.0 docker-compose -f docker-compose.control-plane.yml up -d

curl -fsS http://127.0.0.1:8000/health        # с самого VPS
curl -fsS http://vps.example.com:8000/health         # с ноутбука (если 0.0.0.0)
```

Импортируем демо-план прямо внутри control-plane:

```bash
docker-compose -f docker-compose.control-plane.yml exec control-plane \
    whilly plan import examples/demo/tasks.json
```

### M1.2 — Подключаем macbook как worker (одной командой)

На ноутбуке (с установленным `whilly-orchestrator[worker]`):

```bash
whilly worker connect http://vps.example.com:8000 \
    --bootstrap-token "$WHILLY_WORKER_BOOTSTRAP_TOKEN" \
    --plan demo \
    --hostname "$(hostname)"
```

stdout покажет две строки `key: value` (грепабельные, без баннеров между):

```
worker_id: w-XXXXXXXX
token: <plaintext bearer>
```

После этого процесс `execvp`'ает в `whilly-worker` — операторский PID 1
становится воркером. Bearer хранится в OS keychain (на macOS — Keychain
Access; на headless Linux — `~/.config/whilly/credentials.json`, mode
0600).

> Plain HTTP к не-loopback хосту требует `--insecure`. HTTPS (через
> Caddy / Tailscale Funnel в M2) — рекомендуемый путь.

### M1.3 — Подключаем второй worker на самом VPS

Чтобы продемонстрировать «разные `worker_id` обрабатывают одну очередь»,
подключим второй worker — компании на VPS, через `docker-compose.worker.yml`:

```bash
ssh root@vps.example.com
cd /root/whilly

cp .env.worker.example .env.worker
cat >> .env.worker <<EOF
WHILLY_CONTROL_URL=http://control-plane:8000
WHILLY_WORKER_BOOTSTRAP_TOKEN=$(cat /root/whilly/secrets/bootstrap.token)
WHILLY_PLAN_ID=demo
WHILLY_USE_CONNECT_FLOW=1
EOF

docker-compose -f docker-compose.worker.yml --env-file .env.worker up -d
docker logs whilly-worker
```

`WHILLY_USE_CONNECT_FLOW=1` переключает entrypoint с legacy bash-awk
register-пути на `whilly worker connect`. Default (unset / `0` /
`false` / `no` / `off`) сохраняет байт-в-байт поведение v4.3.1 — это
важно для backwards-compat workshop demo.

### M1.4 — Проверяем audit log

С VPS:

```bash
docker-compose -f docker-compose.control-plane.yml exec postgres \
    psql -U whilly -d whilly -c \
    "SELECT DISTINCT worker_id FROM events
     WHERE event_type='CLAIM' AND plan_id='demo';"
```

Должны увидеть **два разных** `worker_id` — один с macbook, второй с
VPS. Это и есть «полная цепочка аудита распределённой работы»,
которая хорошо смотрится на слайде.

### M1.5 — Backwards compat smoke (обязательная проверка)

После M1-демо **всегда** прогоняем legacy single-host smoke на той же
машине, чтобы убедиться что v4.3.1 demo-путь не сломался:

```bash
bash workshop-demo.sh --cli claude       # exit 0, все demo-задачи DONE
```

Если этот шаг сфейлился — M1-фичи не приняты до фикса.

### M1.6 — Тушим стек

```bash
# На ноутбуке: Ctrl-C на whilly-worker (graceful release)

# На VPS:
docker-compose -f docker-compose.worker.yml --env-file .env.worker down
docker-compose -f docker-compose.control-plane.yml down
```

> 📚 Дальше — M2 (TLS + per-user trust через Caddy / Tailscale Funnel)
> и M3 (web-dashboard + Prometheus). M1 — только deployment-артефакты.

---

## Agentic CLI workflow (рекомендуемый production-режим)

Whilly worker внутри контейнера зовёт `$CLAUDE_BIN` — это может быть
**stub** (для архитектурного демо), **agentic CLI** (рекомендуется для
real workflow) или **raw LLM shim** (для случая «нужно быстро/дёшево, без
file-tools»).

Agentic CLI'и — это полноценные кодинг-агенты со своими **sub-agents**,
**skills**, **MCP-серверами**, file-операциями и tool-use. В отличие от
голого OpenAI-API call'а, они умеют:

- Читать/писать файлы в рабочей директории контейнера
- Запускать bash-команды (тесты, линтеры, билды)
- Делегировать суб-задачи специализированным sub-agent'ам
- Подгружать skills (`.claude/skills/*.md` — markdown-инструкции для агента)
- Подключать MCP-серверы (database access, browser, filesystem, etc.)

В образе `mshegolev/whilly:4.3.1+` встроены **четыре** agentic CLI:
**claude-code**, **gemini-cli**, **opencode**, **codex**. Образ работает на
Node 22 LTS (раньше bookworm-ный node18 ронял gemini-cli и был
несовместим с codex). Выбор через `--cli` флаг:

```bash
# Claude Code (Anthropic) — best agentic capabilities, платно:
export ANTHROPIC_API_KEY=YOUR_ANTHROPIC_KEY_HERE
./workshop-demo.sh --workers 2 --cli claude-code

# OpenCode (open source) — любой provider, бесплатно через OpenRouter free:
export OPENROUTER_API_KEY=YOUR_OPENROUTER_KEY_HERE   # https://openrouter.ai/keys
./workshop-demo.sh --workers 2 --cli opencode

# Gemini CLI (Google) — free 1500 req/day на gemini-2.0-flash:
export GEMINI_API_KEY=YOUR_GEMINI_KEY_HERE   # https://aistudio.google.com/apikey
./workshop-demo.sh --workers 2 --cli gemini

# OpenAI Codex (gpt-5.x, sub-agents/skills/MCP/plugins) — paid OpenAI API:
export OPENAI_API_KEY=YOUR_OPENAI_KEY_HERE   # https://platform.openai.com/api-keys
./workshop-demo.sh --workers 2 --cli codex
```

### Сравнение CLI

| CLI            | Provider lock      | Sub-agents | Skills | MCP | File-tools | Free path                       |
|----------------|--------------------|----------:|-------:|----:|-----------:|---------------------------------|
| **claude-code**| Anthropic only     | yes       | yes    | yes | yes        | нет (paid Anthropic API)        |
| **gemini-cli** | Google Gemini only | yes       | yes    | yes | yes        | 1500 req/day Gemini 2.0 Flash   |
| **opencode**   | любой через models.dev | yes   | yes    | yes | yes        | OpenRouter `:free` модели       |
| **codex**      | OpenAI only        | yes       | yes    | yes | yes        | нет (paid OpenAI API; gpt-5.5 — ChatGPT Pro/Plus OAuth) |

### Как настраивать sub-agents и skills

Все четыре CLI читают конфигурацию из стандартизированных директорий
(совместимость заложена claude-code'ом):

- **`~/.claude/CLAUDE.md`** — глобальный системный промпт для всех агентов
  (правила code style, любимые библиотеки, политика комментариев).
- **`~/.claude/agents/<name>.md`** — определение sub-agent'а
  (system prompt + permissions + tools list).
- **`~/.claude/skills/<name>.md`** — переиспользуемая skill (markdown
  инструкция вида «как делать X»).

В Docker-контейнере воркера эти файлы пробрасываются через volume mount.
Пример docker-compose.demo.yml:

```yaml
worker:
  volumes:
    - ~/.claude:/home/whilly/.claude:ro          # claude-code agents/skills
    - ./.opencode:/home/whilly/.opencode:ro      # opencode-specific overrides
    - ./.gemini:/home/whilly/.gemini:ro          # gemini-specific overrides
    - ~/.codex:/home/whilly/.codex:ro            # codex agents/skills/plugins/MCP
```

OpenCode дополнительно понимает `.opencode/agent/<name>.md` (см.
`opencode agent create`); gemini-cli — `.gemini/AGENTS.md` и
`~/.gemini/`; codex — `~/.codex/agents/`, `~/.codex/skills/`,
`~/.codex/plugins/` (см. `codex plugin --help`). Все четыре CLI
делятся одним базовым форматом markdown с YAML frontmatter:

```markdown
---
name: code-reviewer
description: Reviews diffs for security and style issues
mode: subagent
tools: [read, grep, edit]
---

You are a code reviewer. Read the diff in .git/changes.diff and respond
with a markdown bulleted list of issues. Severity tags: [critical], [high],
[low]. Do not modify any files.
```

### MCP (Model Context Protocol) серверы

MCP — стандарт от Anthropic для подключения tools к LLM agents. Все три
CLI поддерживают его. Конфиг лежит в `~/.claude/mcp.json` (общий) или в
конкретных `.opencode/mcp.json` / `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres"],
      "env": { "POSTGRES_URL": "postgresql://localhost/mydb" }
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    }
  }
}
```

После этого агент может через MCP читать БД и файлы. Подключите volume с
конфигом MCP к контейнеру воркера и оба воркера получат идентичный набор
инструментов.

### Когда использовать --cli vs --llm

| Сценарий                                          | Рекомендация              |
|---------------------------------------------------|---------------------------|
| Архитектурное демо state-machine                  | `--cli stub` (default)    |
| Реальный код-генерационный workflow               | `--cli claude-code` (paid) или `--cli opencode` + free OpenRouter |
| Быстрый smoke-test что задача попадёт в LLM       | `--llm groq` (raw, без файлов) |
| Локальная offline-разработка                      | OpenCode + локальная Ollama |
| CI / scripted automation                          | `--cli gemini` (free 1500 req/day) |
| OpenAI / GPT-5.x ecosystem (skills + plugins)     | `--cli codex` (paid OpenAI API; gpt-5.5 — ChatGPT Pro/Plus OAuth) |

`--cli` тащит весь agentic стек (sub-agents/skills/MCP/file-tools) — это
**всегда** правильнее для production. `--llm` — это «голая модель без
рук», полезна только для быстрых demo и проверки connectivity.

---

## Real LLM modes (raw, без agentic capabilities)

По умолчанию `workshop-demo.sh` использует **stub Claude** — фиктивный
скрипт, который через 2.5 секунды выдаёт `<promise>COMPLETE</promise>`
без реального LLM. Этого достаточно чтобы показать state-machine,
distributed-claim и audit-log. Чтобы подключить реальный raw LLM (без
agentic стека — для этого см. секцию выше), передайте
`--llm <provider>` и нужный API-ключ через env:

```bash
# Самый быстрый бесплатный путь — Groq (14400 req/day на free-tier):
export GROQ_API_KEY=YOUR_GROQ_KEY_HERE           # https://console.groq.com/keys
./workshop-demo.sh --workers 2 --llm groq

# Локальная Ollama (нужен запущенный ollama serve на хосте):
ollama pull qwen2.5-coder:7b
./workshop-demo.sh --workers 2 --llm ollama

# Платный Claude (Anthropic):
export ANTHROPIC_API_KEY=YOUR_ANTHROPIC_KEY_HERE
./workshop-demo.sh --workers 2 --llm claude
```

### Поддерживаемые провайдеры

| `--llm`      | Required env var       | Cost       | Кому подходит                    |
|--------------|------------------------|------------|----------------------------------|
| `stub`       | —                      | $0         | дефолт; быстро, без LLM          |
| `groq`       | `GROQ_API_KEY`         | $0 free    | live-демо, fastest TTFB          |
| `openrouter` | `OPENROUTER_API_KEY`   | $0 на `:free` | свободный выбор моделей       |
| `cerebras`   | `CEREBRAS_API_KEY`     | $0 free    | очень быстрый inference          |
| `gemini`     | `GEMINI_API_KEY`       | $0 free    | 1500 req/day Gemini 2.0 Flash    |
| `ollama`     | —                      | $0 local   | offline, без cloud-зависимостей  |
| `claude`     | `ANTHROPIC_API_KEY`    | $$$ paid   | production-quality вывод         |

### Auto-pick модели под ресурсы контейнера

Скрипт **не задаёт** конкретную модель — entrypoint в контейнере читает
cgroup-лимиты (RAM + CPU) и подбирает модель из таблицы под provider+tier:

| Tier   | RAM     | CPU   | Groq                       | OpenRouter (free)                        | Ollama                  | OpenAI (codex)    |
|--------|---------|-------|----------------------------|------------------------------------------|-------------------------|-------------------|
| TINY   | <4GB    | <2    | llama-3.1-8b-instant       | llama-3.2-3b-instruct:free               | qwen2.5-coder:1.5b      | gpt-5.4-mini      |
| SMALL  | 4-8GB   | 2-4   | llama-3.1-8b-instant       | llama-3.1-8b-instruct:free               | qwen2.5-coder:7b        | gpt-5.4-mini      |
| MEDIUM | 8-16GB  | 4-8   | llama-3.3-70b-versatile    | llama-3.3-70b-instruct:free              | qwen2.5-coder:14b       | gpt-5.4           |
| LARGE  | ≥16GB   | ≥8    | llama-3.3-70b-versatile    | deepseek-chat-v3.1:free                  | qwen2.5-coder:32b       | gpt-5.4           |

Эффективный tier берётся как **min** из mem-tier и cpu-tier, чтобы не
получить «96 cores но 2GB RAM → запустили 70B и упали в OOM».

Принудительно зафиксировать tier:
```bash
./workshop-demo.sh --workers 2 --llm openrouter --tier large
```

Принудительно зафиксировать модель (минуя picker):
```bash
LLM_MODEL=meta-llama/llama-3.1-405b:free ./workshop-demo.sh --workers 2 --llm openrouter
```

### Как это устроено внутри

Whilly worker всегда зовёт `$CLAUDE_BIN` ровно одной командой:

```bash
$CLAUDE_BIN --dangerously-skip-permissions --output-format json \
            --model <model> -p "<prompt>"
```

И ждёт на stdout single-envelope JSON с полем `result`, в котором есть
маркер `<promise>COMPLETE</promise>`. Реальный Claude это умеет искаропки.
Для всех остальных провайдеров используется `docker/llm_shim.py` — это
~150 строк Python (httpx + stdlib), которые:

1. Принимают тот же argv что и Claude CLI (whilly не отличает).
2. Дёргают любой OpenAI-compatible endpoint (`/v1/chat/completions`).
3. Конвертят OpenAI-shape ответ в Claude-shape envelope.
4. Корректно сигналят whilly retry-логике через те же substrings
   (`failed to authenticate`, `API Error: 5xx`).

`docker/llm_resource_picker.py` читает cgroup v2 (`memory.max`, `cpu.max`)
с fallback'ом на cgroup v1 и хост-уровневый `/proc/meminfo`. Маппинг
provider→tier→model задан в одной таблице — добавить нового провайдера =
~10 строк (см. `PROVIDER_MODEL_MAP`).

Обе утилиты покрыты unit-тестами (`tests/unit/test_llm_shim.py`,
`tests/unit/test_llm_resource_picker.py` — 48 cases в сумме).

---

## Что показывать на презентации

Готовая раскадровка ~3-5 минут:

1. **Слайд** — диаграмма из топа этого документа («postgres ◄ control-plane ◄ worker»).
2. **Терминал 1** — `docker compose -f docker-compose.demo.yml up -d`. Дать
   compose поднять три сервиса; пока поднимается — рассказывать про
   архитектуру.
3. **Терминал 1** — `whilly plan show demo` (через `exec control-plane`).
   Все 4 задачи в `PENDING`, граф зависимостей виден.
4. **Терминал 2** — `docker compose logs -f worker`. Включить ровно перед
   импортом, чтобы поток был живой.
5. **Терминал 1** — `whilly plan import examples/demo/tasks.json`.
   В Терминале 2 сразу побегут CLAIM-логи.
6. **Терминал 1** — `whilly plan show demo` ещё раз. Все задачи в `DONE`.
7. **Терминал 1** — `psql ... 'SELECT … FROM events …'`. Показать, что
   audit log полный.
8. **Слайд** — что важно: один и тот же образ, две роли, никаких файловых
   очередей, никаких race condition'ов (Postgres + `FOR UPDATE SKIP LOCKED`).

### Полезные кадры для скриншотов в слайды

```bash
# Граф плана — красивый ASCII со статусами
docker compose -f docker-compose.demo.yml exec control-plane whilly plan show demo

# Live TUI — Rich Live, обновляется каждые ~1с
docker compose -f docker-compose.demo.yml exec control-plane whilly dashboard --plan demo

# Тарелка с событиями
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c \
  "SELECT task_id, event_type, ts FROM events WHERE plan_id='demo' ORDER BY id;"
```

---

## FAQ и траблшутинг

**worker лезет в `localhost:8000` и не находит control-plane.**
Внутри docker-сети адрес control-plane'a — `http://control-plane:8000`,
не `localhost`. Проверьте `WHILLY_CONTROL_URL` в `docker-compose.demo.yml`
(должно быть `http://control-plane:8000`). Если запускаете worker на хосте
против control-plane в Docker — используйте `http://127.0.0.1:8000`
(порт пробрасывается в compose).

**`alembic` ругается на «relation already exists» при перезапуске.**
Это значит, что том `whilly_demo_pgdata` живёт от прошлого запуска.
Это нормально — миграции идемпотентны. Если хочется чистого старта:

```bash
docker compose -f docker-compose.demo.yml down -v   # снимает том
```

**Нужно подключить настоящий Claude вместо stub.**
В `docker-compose.demo.yml`, в сервисе `worker`, удалите `CLAUDE_BIN: ...`
и пробросьте бинарник:

```yaml
worker:
  # ...
  environment:
    # CLAUDE_BIN убран → берётся `claude` из PATH в контейнере
  volumes:
    - /usr/local/bin/claude:/usr/local/bin/claude:ro
    - $HOME/.config/anthropic:/home/whilly/.config/anthropic:ro  # API ключ
```

**Ошибка `WHILLY_WORKER_BOOTSTRAP_TOKEN is not set`.**
Это в логах control-plane. Compose-файл задаёт дефолт `demo-bootstrap`,
но если переопределили через `.env` — убедитесь, что значение действительно
есть в env. Можно явно: `WHILLY_WORKER_BOOTSTRAP_TOKEN=demo-bootstrap docker compose ... up`.

**Можно ли запустить два worker'а одновременно?**

Да — это и есть главный «параллельный» демо-кейс:

```bash
docker compose -f docker-compose.demo.yml up -d --scale worker=2
```

Каждая реплика воркера автоматически регистрируется через bootstrap-token
(`WHILLY_WORKER_BOOTSTRAP_TOKEN`) и получает свой уникальный `worker_id` +
per-worker bearer. `FOR UPDATE SKIP LOCKED` на стороне Postgres гарантирует,
что одна задача — у одного воркера, без deadlock'ов и race condition'ов.
Готовый чеклист параллельного запуска есть в [`DEMO-CHECKLIST.md`](DEMO-CHECKLIST.md).

**Где почитать дальше.**

- [`README.md`](README.md) — полный обзор v4.1.
- [`docs/Whilly-v4-Architecture.md`](docs/Whilly-v4-Architecture.md) — гексагональный layout, scheduling, locks.
- [`docs/Whilly-v4-Worker-Protocol.md`](docs/Whilly-v4-Worker-Protocol.md) — wire-протокол (auth, long-polling).
- [`docs/Whilly-Init-Guide.md`](docs/Whilly-Init-Guide.md) — `whilly init` (PRD wizard).
- [`docs/demo-remote-worker.sh`](docs/demo-remote-worker.sh) — реальный SC-3 single-host скрипт.
