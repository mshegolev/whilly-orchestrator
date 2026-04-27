# PRD: Whilly v4.0 — Distributed Orchestrator Refactoring

**Author:** Mikhail Shchegolev
**Reviewer perspective:** Guido van Rossum (pragmatic Python, "explicit is better than implicit", "flat is better than nested", "there should be one obvious way to do it")
**Date:** 2026-04-27
**Status:** Draft, готов к декомпозиции в `tasks.json`
**Target version:** Whilly 4.0.0 (breaking, big-bang rewrite)
**Replaces:** PRD-refactoring-1.md (предыдущая итерация — Whilly 3.4 incremental — снята)

---

## Problem Statement

Текущий Whilly v3.x — императивный single-machine orchestrator, построенный вокруг трёх неявных предположений:

1. **Одна машина** — весь runtime в одном процессе, агенты — локальные subprocess/tmux.
2. **`tasks.json` как единственный источник истины** — одновременная запись из нескольких агентов и main loop приводит к гонкам состояния (см. коммит `d182629 fix: tolerate concurrent tasks.json reload`).
3. **Claude CLI как единственный исполнитель** — runner жёстко связан с `claude` бинарником.

Это вызывает **две конкретные боли** (зафиксированы в интервью):

- **Race conditions в параллельных запусках** — `MAX_PARALLEL > 1` периодически приводит к потерянным обновлениям статуса, дубликатам выполнения, "застрявшим" `in_progress` задачам, требующим костылей вроде reset stale tasks on startup.
- **Архитектура сопротивляется новым фичам** — попытки добавить distributed execution, Web UI, multi-LLM, DAG-планирование, БД-persistence упираются в монолитный `cli.py::run_plan` и тесную связку tmux/subprocess/file-state.

## Objectives

Переписать ядро Whilly **с нуля** (big-bang rewrite, v3.x freeze) с архитектурой, которая:

1. **Устраняет race conditions by design** — атомарные переходы статуса через транзакции Postgres (`SELECT FOR UPDATE SKIP LOCKED`), а не через файловые блокировки.
2. **Поддерживает горизонтальное расширение воркерами** — control plane на одной машине, воркеры подключаются с других VM по сети (pull-based).
3. **Имеет чистое тестируемое ядро** — pure functions + dataclasses в `whilly/core/`, I/O вынесен на периферию (адаптеры).
4. **Закладывает фундамент для будущих v4.x фич** — Web UI, multi-LLM, multi-tenant идут как additive расширения без переписывания ядра.

### Non-objectives (явно НЕ цели MVP v4.0)

- Web UI / REST API для пользователя (отложено на v4.1).
- Multi-LLM провайдеры кроме Claude CLI (отложено на v4.2).
- Multi-tenant / SaaS режим (отложено на v5.x или навсегда).
- Обратная совместимость с v3.x (clean slate — старые `tasks.json`, `PRD-*.md`, `.whilly_state.json` НЕ читаются).

## Target Users

- **Primary:** Mikhail Shchegolev (maintainer + power user) и small-team пользователи Whilly v3.x, готовые мигрировать на v4.0 с потерей данных.
- **Secondary (future v4.1+):** Команды до 5-20 человек, желающие подключать удалённые воркеры на dedicated VM (с GPU или ближе к API endpoint).

**Personas:**

- *Solo developer* — запускает control plane локально, иногда подключает second VM как воркер для тяжёлых параллельных планов.
- *Power user* — держит control plane 24/7 на сервере, воркеры на 2-3 VM, наблюдает прогресс через CLI dashboard.

**Шкала:** single-user control plane + ≤ 10 одновременных агентов + до 3 worker VM. **НЕ** multi-tenant, **НЕ** cloud-scale.

## Requirements

### Functional Requirements

#### FR-1: Pull-based distributed worker protocol

- **FR-1.1.** Control plane экспозит HTTP endpoint (FastAPI) для регистрации воркеров и выдачи задач.
- **FR-1.2.** Воркер аутентифицируется bootstrap-токеном (`WHILLY_WORKER_TOKEN`); опционально mTLS поверх.
- **FR-1.3.** Воркер тянет задачи через `claim_task()` — атомарный `SELECT ... FOR UPDATE SKIP LOCKED` на стороне Postgres + visibility timeout.
- **FR-1.4.** Если воркер не подтвердил завершение задачи в течение `task_visibility_timeout` (default 15 минут) — задача автоматически возвращается в `pending` и берётся другим воркером.
- **FR-1.5.** Воркер устанавливается отдельным пакетом: `pip install whilly-worker && whilly-worker --connect <url> --token <t>` и сразу начинает брать задачи.
- **FR-1.6.** Control plane может работать без удалённых воркеров — встроенный local worker запускается автоматически в режиме `whilly run` (single-machine как сегодня).

#### FR-2: Postgres state machine

- **FR-2.1.** Все статусы задач, прогон планов, расходы и iteration counters хранятся в Postgres. Файлов `.whilly_state.json` / `tasks.json` (как state) больше нет.
- **FR-2.2.** Переходы статусов идут через типизированный state machine (`PENDING → CLAIMED → IN_PROGRESS → DONE | FAILED | SKIPPED`); невалидные переходы — исключение на уровне core.
- **FR-2.3.** Каждый переход — отдельная Postgres-транзакция; pessimistic locking через `SELECT FOR UPDATE` при claim, optimistic версионирование (`version` column) при complete.
- **FR-2.4.** Event log: каждое изменение статуса пишется в таблицу `events` (append-only), что закрывает требование observability и даёт возможность post-mortem.
- **FR-2.5.** Plan I/O: команда `whilly plan import tasks.json` импортирует план из JSON-файла в БД (одноразовая операция); `whilly plan export <plan_id>` отдаёт обратно. Файл — транспорт, БД — источник истины.

#### FR-3: DAG-based planning

- **FR-3.1.** Планировщик строит граф зависимостей `Task.dependencies` и проверяет ацикличность при импорте плана; цикл → ошибка с указанием цепочки.
- **FR-3.2.** Команда `whilly plan show <plan_id>` рисует ASCII-граф зависимостей с цветовой подсветкой статусов.
- **FR-3.3.** Scheduler выдаёт задачи строго по топологическому порядку с учётом приоритета (`critical > high > medium > low`) внутри одного "уровня" DAG.
- **FR-3.4.** Конфликты по `key_files` решаются на уровне scheduler — две задачи с пересекающимся `key_files` не выдаются параллельно (то же поведение, что v3.x `orchestrator.plan_batches`, но реализовано на стороне БД, не in-memory).

### Non-Functional Requirements

- **NFR-1 (Reliability):** Worker может упасть в любой момент (kill -9, network partition) — задача автоматически возвращается в pool через visibility timeout без потерь и без дубликатов выполнения.
- **NFR-2 (Testability):** Покрытие unit-тестами ядра (`whilly/core/`) ≥ 80%. Интеграционные тесты используют `testcontainers` (реальный Postgres в Docker), без mock'ов БД.
- **NFR-3 (Observability):** Все события (claim, status change, error, retry) пишутся в `events` таблицу + structured JSON в `whilly_logs/whilly_events.jsonl` для совместимости с текущим pipeline.
- **NFR-4 (Pythonicity):** Код следует "Zen of Python" — explicit type hints везде (PEP 484/585), dataclasses для domain models, pure functions в core, никаких метаклассов / monkey-patching / неявной магии. Линтеры: `ruff` + `mypy --strict` для `whilly/core/`.
- **NFR-5 (No premature optimization):** Не вводим Temporal / Celery / Go workers — Postgres + asyncio + `asyncpg` достаточно для целевого масштаба. Миграция на Temporal остаётся возможной в v5.x, но не закладывается в API сейчас.

### Technical Constraints

- **TC-1 (Runtime):** Python 3.12+ (используем `asyncio.TaskGroup`, PEP 695 type aliases).
- **TC-2 (Database):** PostgreSQL 15+ (нужны `SKIP LOCKED`, `LISTEN/NOTIFY`).
- **TC-3 (Driver):** `asyncpg` для async-доступа из core; `psycopg[binary]` как fallback для CLI-утилит миграции.
- **TC-4 (ORM-стратегия):** **отказ от ORM в core** — pure SQL через `asyncpg` + dataclasses. SQLAlchemy допустима только в admin-утилитах (миграции через Alembic).
- **TC-5 (Queue):** не вводим отдельный broker. Используем Postgres как очередь (`SKIP LOCKED` + `LISTEN/NOTIFY`). Redis НЕ нужен в MVP.
- **TC-6 (Worker protocol):** **HTTP + long-polling** (FastAPI на control plane, `httpx` на воркере). gRPC отложен — лишний рантайм-overhead для масштаба "10 агентов".
- **TC-7 (Claude CLI integration):** runner остаётся через `subprocess` к `claude` бинарнику, как в v3.x. Это **единственный** исполнитель в MVP.
- **TC-8 (Архитектурный стиль):** Hexagonal / Ports & Adapters. `whilly/core/` — domain (no I/O), `whilly/adapters/` — Postgres, HTTP, subprocess, filesystem.
- **TC-9 (No backwards compat):** v3.x `tasks.json`, `PRD-*.md`, `.whilly_state.json` НЕ читаются. Утилита `whilly migrate` НЕ предоставляется.

### Module structure (target)

```
whilly/
├── core/                    # pure domain — no I/O, no asyncio
│   ├── models.py            # @dataclass(frozen=True): Task, Plan, Event, WorkerHandle
│   ├── state_machine.py     # pure: (Task, Transition) -> Task | StateError
│   ├── scheduler.py         # pure: (Plan, set[TaskId]) -> list[TaskId] (topo + priority + key_files)
│   └── prompts.py           # pure: (Task) -> str (тот же build_task_prompt, но без cwd-magic)
├── adapters/
│   ├── db/
│   │   ├── repository.py    # async Postgres I/O: claim, complete, fail, list_events
│   │   ├── migrations/      # Alembic
│   │   └── schema.sql       # reference DDL
│   ├── runner/
│   │   ├── claude_cli.py    # subprocess к claude binary
│   │   └── result_parser.py # тот же AgentResult, но pure
│   ├── transport/
│   │   ├── server.py        # FastAPI: /workers/register, /tasks/claim, /tasks/complete
│   │   └── client.py        # httpx-based worker client
│   └── filesystem/
│       └── plan_io.py       # import/export tasks.json
├── cli/
│   ├── __main__.py          # whilly run | plan | worker | migrate
│   └── dashboard.py         # Rich Live TUI (как сегодня, но читает из БД)
└── worker/                  # отдельный пакет whilly-worker
    └── main.py              # whilly-worker --connect ... --token ...
```

## Success Criteria

Релиз v4.0.0 считается успешным, если **все 6 критериев** выполнены и подтверждены тестами / демо:

| #     | Критерий                                                                                                                  | Способ проверки                                                |
| ----- | ------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| SC-1  | Race conditions исчезли: 100 параллельных операций над одним планом без потерянных / дублированных обновлений             | `tests/integration/test_concurrent_claims.py` с testcontainers |
| SC-2  | Worker fault tolerance: `kill -9` воркера в середине задачи → автоматический re-claim другим воркером в течение ≤ 30с     | Manual demo + integration test                                 |
| SC-3  | Remote worker out-of-the-box: на чистой VM `pip install whilly-worker && whilly-worker --connect ... --token ...` работает | Demo на second VM, log в release notes                         |
| SC-4  | DAG визуализация: `whilly plan show <id>` рисует ASCII-граф; цикл → ошибка с указанием цепочки                            | CLI snapshot test + e2e на синтетическом плане с циклом        |
| SC-5  | Test coverage ядра ≥ 80%: state_machine, scheduler, models — без mock'ов БД, через testcontainers                         | `coverage report --include='whilly/core/*'`                    |
| SC-6  | Чистое ядро: `whilly/core/` не импортирует `asyncpg`, `httpx`, `subprocess`, `os.chdir` — проверяется import-linter        | `import-linter` config в `pyproject.toml`                      |

## Out of Scope

Явно **НЕ** делаем в v4.0, чтобы уложиться в 1-week deadline:

- ❌ **Web UI / REST API для пользователя** — отдельная FastAPI-обвязка. Внутренний worker-protocol HTTP — да, публичный UI — нет.
- ❌ **Multi-LLM провайдеры** — только Claude CLI, как в v3.x. Adapter-интерфейс заложен, но реализация одна.
- ❌ **Multi-tenant / shared instance** — один пользователь, один Postgres, один control plane.
- ❌ **Backwards compatibility** — v3.x `tasks.json`, `PRD-*.md`, `.whilly_state.json` НЕ читаются. Утилита `whilly migrate` отсутствует.
- ❌ **Production deployment manifests** — Docker Compose для локального запуска есть, но Kubernetes / Helm / systemd unit'ы — забота пользователя.
- ❌ **Auto-scaling воркеров** — воркеры запускаются вручную; auto-spawn в облаке отложен на v4.1+.
- ❌ **Authorization beyond bootstrap token** — RBAC, OIDC, audit log без событий уровня "user X ran plan Y" — отложено.
- ❌ **PRD wizard rewrite** — `prd_wizard.py` и `prd_generator.py` остаются как есть (Markdown-генератор + Claude CLI), просто переезжают в `adapters/prd/`.
- ❌ **Per-task token cost accounting** (был в предыдущем PRD 3.4) — переносится в v4.1 как additive фича.

## Risks and Assumptions

### Risks

| Риск | Вероятность | Impact | Митигация |
| ---- | ----------- | ------ | --------- |
| **R-1:** 1-week deadline нереалистичен — уйдёт 2-3 недели          | Высокая  | Средний | День 1 — спайк на критическом пути (state machine + claim/complete). Если не работает — режем DAG-визуализацию (FR-3.2) и event log (FR-2.4). |
| **R-2:** Big-bang rewrite оставляет v3.x пользователей без апгрейд-пути | Средняя  | Средний | Объявить v3.x в EOL за 30 дней до релиза v4.0. Отдельной веткой сделать тег `v3-final`. |
| **R-3:** Postgres-as-queue не масштабируется выше 10 воркеров      | Низкая (для целевого масштаба) | Низкий | В целевой scale ≤ 10 параллельных агентов это не проблема. При нужде — миграция на Redis Streams в v4.x. |
| **R-4:** Worker protocol HTTP+long-polling даёт высокую latency (1-2с задержка claim) | Средняя | Низкий | Приемлемо для агентов, работающих минутами. Если станет проблемой — `LISTEN/NOTIFY` через отдельный admin endpoint. |
| **R-5:** Hexagonal architecture усложнит код, замедлит разработку  | Средняя | Средний | Жёсткий лимит: `whilly/core/` ≤ 1500 строк. Если адаптеры распухают — рефакторим в плоскую структуру. |
| **R-6:** "No backwards compat" вызовет негатив у v3.x пользователей | Низкая (мало пользователей) | Низкий | Чёткие release notes, EOL announcement, миграционный гайд "как переразложить план вручную". |

### Assumptions

- **A-1.** PostgreSQL 15+ доступен у пользователя или развёртывается через Docker Compose из коробки.
- **A-2.** Claude CLI остаётся стабильным API-контрактом (JSON output, `<promise>COMPLETE</promise>` маркер) на горизонте 6+ месяцев.
- **A-3.** Целевой масштаб (≤ 10 агентов, ≤ 3 VM) реалистичен и не вырастет до cloud-scale за время жизни v4.0.
- **A-4.** Сетевая связность между control plane и воркерами стабильна (worker → control plane outbound HTTPS); NAT-traversal не нужен.
- **A-5.** Один разработчик способен поддерживать кодовую базу из ~5000 строк Python с тестами. Если код вырастает за это — сигнал для рефакторинга.

## Timeline

**1-week sprint, solo developer.** Каждый день — vertical slice (рабочий end-to-end функционал).

| День           | Цель                                                                  | Deliverable                                                                                | Demo / тест                                                       |
| -------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------- |
| **Day 1 (Пн)** | Скелет: репо, схема БД, `core/models.py`, `state_machine.py`           | `whilly/core/` + Alembic init migration. Postgres up через docker-compose.                  | `pytest tests/unit/test_state_machine.py` — 20 тестов на переходы. |
| **Day 2 (Вт)** | Postgres adapter: claim / complete / fail с `SKIP LOCKED`              | `adapters/db/repository.py`. Команда `whilly plan import tasks.json`.                       | Concurrent claim test (testcontainers) — SC-1 проходит.            |
| **Day 3 (Ср)** | DAG scheduler + topological order + key_files conflict detection       | `core/scheduler.py`. Команда `whilly plan show <id>` (ASCII).                               | SC-4 — план с циклом отвергается с понятным сообщением.            |
| **Day 4 (Чт)** | Local worker + Claude CLI runner                                       | `adapters/runner/claude_cli.py`, single-machine `whilly run` работает.                      | E2E тест: маленький план из 3 задач выполняется до конца.          |
| **Day 5 (Пт)** | Worker protocol: FastAPI server + httpx client + bootstrap token auth  | `adapters/transport/`. Remote worker берёт задачи.                                          | SC-3 demo: `whilly-worker --connect ... --token ...` на second VM. |
| **Day 6 (Сб)** | Visibility timeout + worker fault tolerance + dashboard read from DB   | Killed worker → re-claim. Rich TUI показывает live статус из БД.                            | SC-2 demo: `kill -9` worker → задача завершается на другом воркере. |
| **Day 7 (Вс)** | Polish: import-linter, mypy --strict для core, doc, release notes, tag | Все 6 SC выполнены. Релиз v4.0.0.                                                            | `coverage report` — SC-5 (≥80% core). `lint-imports` — SC-6.       |

**Buffer:** нет. При срыве дня — режется functional requirement из приоритета "nice-to-have" (DAG-visualization, dashboard read-from-db).

**Hard cutoff feature priority (если режем):**

1. **Не режем:** SC-1 (race conditions), FR-1.3 (claim through DB), FR-2.1-2.3 (state machine).
2. **Режем первым:** FR-3.2 (DAG ASCII visualization) → CLI команду заменяем на `psql` query.
3. **Режем вторым:** FR-2.4 (event log) → оставляем только текущий статус, без истории.
4. **Режем третьим:** SC-4 (DAG cycle detection) → отлавливаем циклы только в runtime, не at-import.

---

## Appendix A: "Guido perspective" — design principles applied

В аудите от лица Гвидо ван Россума выделяются следующие принципы, прошитые в этот PRD:

1. **"Explicit is better than implicit"** — переходы статуса через named transitions (`Transition.CLAIM`), не через неявные mutations. SQL пишем сами, ORM магия отвергается в ядре.
2. **"Flat is better than nested"** — `whilly/core/` — плоский namespace из 4 модулей, не вложенные пакеты `domain/entities/aggregates/...`.
3. **"There should be one obvious way to do it"** — один runner (Claude CLI), один queue backend (Postgres), один transport (HTTP). Multi-backend конфигурация отложена.
4. **"Readability counts"** — код ядра должен читаться как описание бизнес-логики. Pure functions over dataclasses — да; монады, currying, Result-типы из `returns` — нет (это не идиоматичный Python).
5. **"In the face of ambiguity, refuse the temptation to guess"** — schema validation падает явно с указанием поля и причины, не "best-effort tolerance".
6. **Skepticism toward functional purity** — Гвидо удалил `reduce` из builtins в Python 3 не случайно. Pure core — да, потому что это даёт testability. Но не FP ради FP: классы используются, где они уместны (state machine handler, runner protocol, FastAPI dependency injection).
7. **"Now is better than never. Although never is often better than *right* now"** — режем scope агрессивно. Web UI / multi-LLM / migration tool — отложены, не отвергнуты.

## Appendix B: Migration story для v3.x пользователей

Хотя backwards compatibility вне scope, минимальное уважение к существующим пользователям:

1. v3.x ветка замораживается за 30 дней до релиза v4.0, помечается тегом `v3-final`.
2. README v3.x обновляется баннером: "v3.x is in maintenance mode. v4.0 (incompatible) is the new development line."
3. В release notes v4.0 — раздел "Migrating from v3.x": "Re-run `whilly plan import` on your existing tasks.json. PRD files are unchanged. State files (.whilly_state.json) are no longer used."
4. Старая команда `whilly --tasks tasks.json` в v4.0 печатает понятное сообщение об ошибке: "v3.x CLI is gone. Use `whilly plan import tasks.json && whilly run`".

## Appendix C: Why NOT functional programming (rejected approach)

Изначально пользователь предложил «возможно надо переписать код на функциональное программирование». В ходе интервью этот путь явно **отвергнут** в пользу прагматичного Python. Обоснование:

- **Природа проблемы — I/O-heavy.** Whilly координирует subprocess'ы, БД, HTTP — это область side effects. FP-обёртки (IO-монады, `returns.Result`) добавили бы синтаксический шум без реального выигрыша.
- **Race conditions решаются транзакциями, не immutability.** Postgres `SELECT FOR UPDATE SKIP LOCKED` — настоящий ответ на гонки. Frozen dataclasses в Python не дают concurrency safety, потому что собрать новое значение и записать его — две операции, между которыми кто-то другой может вмешаться.
- **Команда — один человек.** FP-инструменты в Python (`toolz`, `returns`, `pyrsistent`) требуют onboarding-инвестиции, которая не окупается на масштабе ≤ 5000 строк.
- **Гвидо van Rossum исторически скептичен к FP в Python** — удаление `reduce` из builtins, отказ от расширения lambda-синтаксиса, осторожность с walrus operator. Дух языка — императивный с заимствованиями там, где они помогают.

**Что взято из FP:** pure core (no I/O в `whilly/core/`), immutable dataclasses (`frozen=True`), pure functions для бизнес-логики (state transitions, scheduling). Это даёт **testability** — главное практическое преимущество FP — без онтологических обязательств.

---

**Next step:** декомпозиция этого PRD в `tasks.json` через `whilly --init` или ручную нарезку под 1-week sprint.
