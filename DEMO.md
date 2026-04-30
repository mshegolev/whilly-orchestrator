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
4. [Что показывать на презентации](#что-показывать-на-презентации)
5. [FAQ и траблшутинг](#faq-и-траблшутинг)

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
