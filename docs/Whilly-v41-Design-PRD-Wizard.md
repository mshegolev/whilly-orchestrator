---
title: Design — PRD wizard port to v4 (TASK-104a)
layout: default
nav_order: 99
description: "Mini-design doc для TASK-104a: как portировать v3 PRD wizard на Postgres-плановую модель v4."
permalink: /design/prd-wizard-v4
---

# PRD wizard port to v4 — design notes

> **Scope:** TASK-104a из [`.planning/v4-1_tasks.json`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.planning/v4-1_tasks.json). Привести интерактивный PRD-визард из v3 в v4-плановую модель (Postgres, не файловый `tasks.json`). Соответствующие dependent task'и: TASK-104b (TRIZ), TASK-104c (Decision Gate), TASK-105 (удаление `cli_legacy.py`).

## Текущее состояние (v3)

В v3 PRD-визард — это связка двух модулей плюс мастер-промпт:

* [`whilly/prd_wizard.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/prd_wizard.py) — асинхронный фоновой режим под tmux: dashboard крутится, рядом запускается `claude -p` headless, по завершении вызывается `on_complete(slug, prd_path)` callback.
* [`whilly/prd_launcher.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/prd_launcher.py) — интерактивный режим в терминале: `claude --append-system-prompt @config/prd_wizard_prompt.md` запускается foreground, юзер ведёт живой диалог.
* `config/prd_wizard_prompt.md` — мастер-промпт, говорящий Claude как вести интервью и что в финале сохранить как `docs/PRD-<slug>.md`.

Финал обоих flow одинаковый: на диске лежит `docs/PRD-<slug>.md`, на чём-то остановились дальше — у v3 это был автоматический вызов `generate_tasks(prd_path)` из [`whilly/prd_generator.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/prd_generator.py), который пишет `tasks.json` рядом с PRD.

## Что меняется в v4

Файловый `tasks.json` больше не source of truth. План должен оказаться в Postgres-таблице `tasks` через [`whilly plan import <path>`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/cli/plan.py) или эквивалентный API. То есть пайплайн раскручивается так:

```
whilly init "хочу X"
    ├─ launches claude with PRD master prompt (interactive in TTY,
    │  background-tmux otherwise) — REUSE whilly.prd_launcher / prd_wizard AS-IS
    ├─ Claude → docs/PRD-<slug>.md (как в v3)
    ├─ generate_tasks(prd_path) → tasks-payload (JSON dict, в памяти, НЕ на диск)
    │                              REUSE whilly.prd_generator with one tweak:
    │                              return dict вместо writing to file
    └─ NEW: whilly.adapters.filesystem.plan_io.import_plan_dict(payload)
       → INSERT INTO plans + INSERT INTO tasks через TaskRepository
```

## Что переиспользуется AS-IS

* `whilly/prd_wizard.py` — tmux + headless flow. Меняется только `on_complete` callback: вместо записи `tasks.json` вызывает `import_plan_dict`.
* `whilly/prd_launcher.py` — interactive flow. Меняется тот же финальный шаг.
* `config/prd_wizard_prompt.md` — мастер-промпт; никаких изменений.
* `whilly/prd_generator.py::generate_tasks` — уже возвращает dict; нужно либо expose это явно, либо обёрнуть в новый `generate_tasks_dict(prd_path) -> dict`.

## Что нужно написать новое

### 1. `whilly/cli/init.py` — новый sub-CLI

Composition root, тонкий обёртчик. Пример surface:

```bash
whilly init "хочу CLI для мониторинга API"
    # → запускает interactive PRD-wizard, по завершении засевает план в БД,
    #   печатает 'whilly run --plan <slug>' и 'whilly plan show <slug>'

whilly init "хочу X" --slug api-monitor
    # → задаёт slug явно (по умолчанию — slugify первых 8 слов)

whilly init "хочу X" --non-interactive
    # → headless режим: Claude получает описание как один-shot запрос,
    #   wizard не задаёт уточняющих вопросов, годится для скриптов

whilly init "хочу X" --no-import
    # → создать только PRD-файл, не засевать в БД (опт-аут, debugging)
```

Internals:

```python
def run_init_command(argv: list[str]) -> int:
    args = parse_args(argv)
    slug = args.slug or _slugify(args.description)
    prd_path = Path(f"docs/PRD-{slug}.md")

    if args.non_interactive:
        prd_wizard.run_headless(args.description, prd_path)
    else:
        prd_launcher.run_interactive(args.description, prd_path)

    if not prd_path.exists():
        log.error("wizard exited without saving PRD; nothing to import")
        return EXIT_USER_ABORT

    if args.no_import:
        print(f"PRD saved at {prd_path}; --no-import set, plan not imported")
        return EXIT_OK

    plan_payload = prd_generator.generate_tasks_dict(prd_path, plan_id=slug)
    asyncio.run(import_plan_dict(plan_payload))

    print(f"plan {slug!r} imported. Next steps:")
    print(f"  whilly plan show {slug}")
    print(f"  whilly run --plan {slug}")
    return EXIT_OK
```

### 2. `whilly.adapters.filesystem.plan_io.import_plan_dict`

Уже есть `import_plan(path)` который читает JSON-файл и зовёт repo. Нужен второй вход который принимает dict напрямую — чтобы не материализовать `tasks.json` на диске только ради того чтобы сразу прочитать.

Реализация — 5 строк: вынести core-логику из `import_plan` в helper, который оба варианта зовут.

### 3. `whilly.prd_generator.generate_tasks_dict`

Сейчас `generate_tasks(prd_path)` пишет `tasks.json` рядом с PRD. Нужна вариация которая возвращает dict без записи на диск (опционально с записью — для `--no-import` debugging пути).

```python
def generate_tasks_dict(prd_path: Path, plan_id: str) -> dict:
    """Same flow as generate_tasks() but returns the payload instead of writing it."""
    # ... запросить Claude → распарсить → вернуть {project, tasks: [...]}
```

## Совместимость с tmux-режимом

v3 имел два режима — interactive (foreground в TTY) и tmux-background (для dashboard). На v4 dashboard живёт в Postgres-проекции (`whilly dashboard`), поэтому tmux-режим становится менее важным. Но удалять его рано: операторы которые гоняют long-running PRD-сессии (15-20 минут диалога) вне TTY всё ещё хотят tmux, а не блокирующий foreground.

**Решение:** оставить оба режима. По умолчанию detect TTY (`sys.stdin.isatty()`) → если терминал, interactive; если нет, tmux background. `--tmux` / `--no-tmux` для явного override.

## Что делать если PRD wizard упал посреди разговора

v3 поведение: PRD-файл может быть полу-написан, юзер видит ошибку, запускает заново. Проблема: `generate_tasks` на полу-PRD может выдать мусорный план.

v4 предложение: `whilly init` записывает sentinel `.wizard-state-<slug>.json` (slug, prd_path, started_at), и при повторном запуске с тем же slug'ом предлагает либо продолжить (Claude с историей), либо начать сначала (удалить sentinel). На первом этапе — простая проверка «PRD файл существует и содержит секцию `## Tasks` (или подобный маркер) → ОК импортировать; иначе — отказ с подсказкой `--no-import` для дебага».

## Тесты

Unit:

* `tests/unit/test_prd_generator_dict.py` — `generate_tasks_dict` возвращает dict нужной формы (mock Claude response).
* `tests/unit/test_init_cli_args.py` — argparse: `--slug`, `--non-interactive`, `--no-import`.

Integration (testcontainers):

* `tests/integration/test_init_e2e.py` — фейковый Claude-stub отдаёт PRD + tasks JSON; CLI вызов `whilly init "..." --non-interactive` приводит к INSERT в Postgres; `whilly plan show <slug>` показывает граф.
* `tests/integration/test_init_idempotent.py` — повторный `whilly init` с тем же slug'ом не дублирует план (либо отказ, либо replace, по флагу).

## Удаление cli_legacy

Это TASK-105, депенды на 104a/b/c. После того как всё портировано:

1. `whilly/cli_legacy.py` удаляется целиком.
2. `whilly/cli/__init__.py::main` теряет ветку «fall back to legacy if v4 doesn't recognise». Просто `argparse` через v4-таблицу подкоманд.
3. README перепишется (это часть TASK-105) — секция «v3 features» уходит, на её место — нормальная v4 docs roster.
4. Конфиги `.whilly_state.json` / `.whilly_workspaces/` — больше нигде не читаются. `whilly doctor` (если останется) их игнорирует.

## Оценка

Скоп TASK-104a — самый большой кусок v4.1, потому что фактически возвращает «продуктовую» поверхность из v3. Trance-grain-ближе:

* `whilly/cli/init.py` — ~150 строк (composition + argparse).
* `import_plan_dict` + `generate_tasks_dict` extraction — ~80 строк (плюс existing logic move).
* TTY detection / tmux fallback — ~30 строк.
* Tests — ~250 строк (unit + integration).
* Документация (`docs/Whilly-Init-Guide.md`?) — ~100 строк.

Реалистичная оценка — **2-3 дня full-time** (без отвлечений), **5-7 дней** в обычном режиме.

## Pointers

* PRD-визард v3: [`whilly/prd_wizard.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/prd_wizard.py), [`whilly/prd_launcher.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/prd_launcher.py)
* Generator v3: [`whilly/prd_generator.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/prd_generator.py)
* Master prompt: [`config/prd_wizard_prompt.md`](https://github.com/mshegolev/whilly-orchestrator/blob/main/config/prd_wizard_prompt.md)
* План задач: [`.planning/v4-1_tasks.json`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.planning/v4-1_tasks.json) → TASK-104a
* Adapter contract для plan import: [`whilly/adapters/filesystem/plan_io.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/filesystem/plan_io.py)
