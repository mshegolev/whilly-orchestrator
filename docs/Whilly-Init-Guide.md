---
title: whilly init — guide
layout: default
nav_order: 4
description: "Как пользоваться `whilly init` — интерактивный PRD-визард + автоматический импорт плана в Postgres."
permalink: /whilly-init-guide
---

# `whilly init` — guide

> Один из основных способов начать работу с Whilly v4. Описываешь идею в свободной форме — на выходе получаешь PRD-документ + план задач в Postgres, готовый к запуску через `whilly run`.

## Когда это нужно

Если у тебя уже есть `tasks.json` — пропускай и иди в [`whilly plan import`]({{ site.baseurl }}/Getting-Started). `whilly init` пригождается когда:

- У тебя в голове идея, но не план — нужно «допросить себя через Claude», получить PRD, и автоматически распилить на задачи.
- Хочешь воспроизвести v3-style flow (`whilly --init "..."`) на Postgres-плановой модели v4.
- Нужно быстро накидать рабочую гипотезу (через `--no-import` посмотришь PRD, прежде чем коммитить план в БД).

## Базовый сценарий

```bash
# 1. Postgres + миграции (если ещё нет)
docker compose up -d
export WHILLY_DATABASE_URL=postgresql://whilly:whilly@localhost:5432/whilly
alembic upgrade head

# 2. Запускаем визарда
whilly init "сделать CLI tool для мониторинга API endpoints" --slug api-monitor

# 3. Дальше — работаем как с любым другим планом
whilly plan show api-monitor
whilly run --plan api-monitor
```

В TTY (обычный терминал) это запустит **интерактивный** Claude-сеанс: визард будет задавать уточняющие вопросы, ты отвечаешь, в конце он сохраняет `docs/PRD-api-monitor.md` и автоматически импортирует план задач в Postgres.

Вне TTY (cron, CI, `ssh -T`) — переключается на **headless-режим**: Claude получит описание одним промптом без живого диалога. Поведение по умолчанию определяется через `sys.stdin.isatty()`; принудить можно флагами.

## Флаги

| Флаг | Что делает |
|------|-----------|
| `--slug X` | Явное имя для PRD-файла (`docs/PRD-X.md`) и `plan_id` в Postgres. По умолчанию выводится из первых 8 слов идеи (kebab-case). |
| `--interactive` | Принудительно интерактивный режим (даже если stdin не TTY). |
| `--headless` | Принудительно headless (даже в терминале). |
| `--no-import` | Сохранить только PRD-файл, не импортировать план в БД. Полезно для дебага. |
| `--force` | Перезаписать существующий `docs/PRD-<slug>.md`. Без этого флага — отказ с подсказкой использовать другой slug. |
| `--model X` | Передать модель Claude'у. По умолчанию `claude-opus-4-6[1m]`. |
| `--output-dir X` | Куда положить PRD-файл. По умолчанию `docs`. |

## Headless для скриптов

```bash
whilly init "идея в одной строке" --headless --slug feature-x
```

Headless подразумевает single-shot Claude-вызов без интерактива. Это:
- быстрее (~30 секунд против многоминутного диалога),
- детерминированнее (нет человека в петле),
- работает в CI / cron / ssh без TTY.

Ценой — Claude угадывает какие-то детали сам, потому что не задаёт уточняющих вопросов. Если нужен качественный PRD — открывай терминал и используй interactive.

## Тонкости и FAQ

**Что если `whilly init` упал посреди генерации?**
Если PRD-файл успел записаться — он останется на диске для инспекции (ты сможешь вручную проверить что Claude успел нагенерировать). Если потом захочешь регенерировать — добавь `--force`.

**Что если план уже импортирован, но я хочу пересоздать?**
Сейчас `--force` перезаписывает только PRD-файл, но не очищает план из Postgres. Это ловушка: попытка повторного импорта со старым `plan_id` упадёт на foreign-key constraint (план уже существует). Воркэраунд — ручная очистка через psql:

```sql
DELETE FROM events WHERE task_id IN (SELECT id FROM tasks WHERE plan_id = 'api-monitor');
DELETE FROM tasks WHERE plan_id = 'api-monitor';
DELETE FROM plans WHERE id = 'api-monitor';
```

Полноценный `whilly plan reset` — на роадмапе ([TASK-103]({{ site.baseurl }}/v4.0-release-checklist#out-of-scope-for-v40--tracked-for-v41)).

**Откуда берётся `WHILLY_DATABASE_URL`?**
Тот же DSN, что использует `whilly plan import` и `whilly run`. Единый источник правды для всей v4-линии. Если переменная не задана — `whilly init` остановится с понятной подсказкой *перед* тем как тратить токены на Claude.

**А если у меня нет Claude CLI?**
Headless / interactive режимы оба требуют установленный Claude CLI на `$PATH` (или путь через `CLAUDE_BIN` env var). Без него PRD не сгенерится — `whilly init` упадёт с сообщением «`Claude CLI not found`». В тестах используется shell-stub (`tests/fixtures/fake_claude_prd.sh`), но для production — реальный Claude.

## Exit-коды

| Код | Что значит |
|-----|------------|
| `0` | Всё прошло, PRD сохранён, план импортирован. |
| `1` | Ошибка пользователя: пустая идея, невалидный slug, существующий PRD без `--force`, упавший wizard, ошибка генерации задач или импорта в БД. |
| `2` | Ошибка окружения: `WHILLY_DATABASE_URL` не установлен. PRD при этом сохраняется — можно поправить env и перезапустить с `--force`. |
| `130` | `Ctrl-C` посреди работы (POSIX SIGINT). |

## Что под капотом

`whilly init` — это thin composition root в `whilly/cli/init.py`. Реальная работа делается в трёх местах:

1. **Генерация PRD** — `whilly.prd_launcher.run_prd_wizard` (interactive) или `whilly.prd_generator.generate_prd` (headless). Оба пишут в `docs/PRD-<slug>.md`.
2. **Распилка PRD на задачи** — `whilly.prd_generator.generate_tasks_dict` запрашивает у Claude tasks JSON, валидирует, возвращает dict в памяти (никакого `tasks.json` на диске).
3. **Импорт в БД** — `whilly.adapters.filesystem.plan_io.parse_plan_dict` шейп-чек + `whilly.cli.plan._async_import` транзакционный INSERT в `plans` + `tasks`. Тот же helper, что использует `whilly plan import`.

Полная архитектурная картина: [Whilly v4 Architecture]({{ site.baseurl }}/Whilly-v4-Architecture). Дизайн-док с обоснованиями: [TASK-104a design notes]({{ site.baseurl }}/design/prd-wizard-v4).
