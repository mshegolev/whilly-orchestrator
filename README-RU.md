# Whilly Orchestrator (RU)

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://img.shields.io/pypi/v/whilly-orchestrator.svg)](https://pypi.org/project/whilly-orchestrator/)

Whilly Orchestrator — это control plane для безопасного и наблюдаемого запуска
AI-агентов на инженерных задачах.

Он принимает structured work из JSON plans, GitHub Issues, GitHub Projects,
Jira и PRD/Forge intake, приводит задачи к единой модели, сохраняет состояние в
Postgres, строит очередь выполнения, запускает local/remote workers, передаёт
подготовленные prompts runner/backend'ам, отслеживает статусы, budgets, events,
ошибки и human review points.

Whilly не является автономным AI-разработчиком. Он orchestrates agents; it does
not magically make agent output correct. Его ценность — controlled acceleration:
ограничить scope работы, проверить входные задачи, управлять очередью,
зафиксировать состояние, сделать выполнение наблюдаемым и оставить человеку
контроль над критическими решениями.

📘 [Full English README](README.md) · 🌐 [Распределённая установка](docs/Distributed-Setup.md) · 🧭 [Описание проекта](docs/Project-Description.md)

## Что Делает Whilly

- Принимает задачи из JSON plans, GitHub Issues, GitHub Projects, Jira и
  PRD/Forge intake.
- Нормализует задачи: description, dependencies, priority, acceptance criteria,
  test steps, key files, budget и `plan_id`.
- Проверяет задачи до запуска: vague work может быть rejected/skipped,
  dependency cycles не принимаются, decision gates могут работать в strict-mode.
- Оркестрирует выполнение через Postgres-backed queue, row locking, worker
  claiming, dependency readiness, priority и budget checks.
- Передаёт агенту только подготовленную задачу через runner/handoff backend.
  Агент не выбирает задачу произвольно и не управляет всем project plan.
- Фиксирует результат через deterministic state machine и append-only audit
  events.
- Даёт observability: dashboard, SSE, Prometheus metrics, health endpoints,
  worker heartbeat и JSONL mirror.
- Поддерживает human-in-the-loop через PR review, handoff backend, dashboard,
  issue/Jira comments и explicit `BLOCKED` / `HUMAN_LOOP` состояния.

## Что Работает Сейчас

Текущая версия лучше всего описывается как:

> Issue-driven AI task orchestrator для одного рабочего репозитория или
> workspace, с Postgres-backed task queue, deterministic state machine, worker
> execution, runner abstraction, audit events и базовыми safety gates.

Она подходит для:

- bugfix tasks;
- feature tasks;
- refactoring;
- test generation;
- documentation updates;
- structured task plans;
- controlled local/remote worker execution;
- observability of task lifecycle.

## Честные Ограничения

Core worker loop пока не нужно описывать как:

- полноценное multi-repo execution;
- автоматический PR review feedback loop;
- обязательный CI/lint verification;
- полноценную sandbox/VM isolation;
- semantic long-term memory;
- надёжный git rollback;
- автономный production release без человека.

Эти возможности могут развиваться поверх архитектуры, но они не являются
базовыми гарантиями текущего core loop.

## Целевое Состояние

Долгосрочная цель — сделать Whilly configurable project-aware orchestrator.
Каждый тип проекта должен уметь задавать свои sources, pipeline stages, quality
gates, verification steps, runners, sinks и human approval checkpoints.

Примеры:

- backend services: unit tests, lint checks, PR creation, human review;
- GraphQL APIs: schema diffing, resolver impact analysis, generated contract
  tests;
- ETL/data pipelines: data quality checks, QA/STLC stages, sample run,
  regression validation;
- documentation projects: PRD intake, doc generation, consistency checks,
  human approval.

## Быстрый Старт

```bash
python3.12 -m pip install whilly-orchestrator

# Import/apply a structured plan.
whilly plan import examples/demo/tasks.json
whilly plan apply examples/demo/tasks.json --strict

# Run a local worker for a plan.
whilly run --plan demo
```

Для control-plane/remote-worker установки см. [Distributed Setup](docs/Distributed-Setup.md).

## Legacy v3

Legacy v3.x single-process loop доступен на теге
[`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final).
Runtime state v3 не совместим с v4+. Подробности:
[миграция с v3 на v4](docs/Whilly-v4-Migration-from-v3.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).
