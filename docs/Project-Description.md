---
title: О проекте
layout: default
nav_order: 2
description: "Whilly Orchestrator — control plane для наблюдаемого запуска AI-агентов на инженерных задачах."
permalink: /project-description
---

# Whilly Orchestrator

📎 [GitHub](https://github.com/mshegolev/whilly-orchestrator) · 📦 PyPI: [whilly-orchestrator](https://pypi.org/project/whilly-orchestrator/)

> Эта страница про **v4+** — Postgres-backed orchestration/control-plane версию.
> Legacy **v3.x** доступна на теге [`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final)
> и описана отдельно: [v3 legacy]({{ site.baseurl }}/project-description-v3).

## Миссия

Whilly Orchestrator — это control plane для AI-assisted software delivery.

Он соединяет task intake, deterministic planning, guarded execution, worker
orchestration, auditability и human review в единый workflow. Цель системы —
сделать выполнение AI-assisted engineering work воспроизводимым, контролируемым
и масштабируемым.

Whilly не пытается заменить инженерную команду. Он помогает безопасно запускать
AI-агентов на coding tasks, уменьшая ручную координацию, сохраняя контроль над
состоянием задач и оставляя инженеру прозрачный audit trail.

## Что Делает Whilly

Whilly принимает задачи из разных источников:

- JSON plans;
- GitHub Issues;
- GitHub Projects;
- Jira;
- Forge/PRD intake.

После intake задачи приводятся к единой модели: описание, зависимости,
приоритет, acceptance criteria, test steps, key files, budget и `plan_id`.

Перед запуском Whilly проверяет качество задачи: расплывчатые задачи можно
отклонять или переводить в `SKIPPED`, acceptance criteria и test steps
валидируются, dependency cycles не принимаются, decision gates могут работать в
strict-режиме.

## Как Идёт Выполнение

Задачи хранятся в Postgres. Control plane выбирает следующую `PENDING` задачу
из очереди с учётом dependencies, priority и budget. Claim защищён row locking,
поэтому два воркера не должны выполнять одну задачу одновременно.

Worker запускается локально или удалённо. Он получает не весь проектный план, а
конкретную подготовленную задачу через runner или handoff backend. Агент не
выбирает задачу произвольно и не получает полный контроль над планированием
проекта.

Результат фиксируется через state machine. `DONE` означает, что runner завершился
успешно и был найден completion marker. Иначе задача становится `FAILED`; при
shutdown или visibility timeout она может вернуться в `PENDING`.

## Наблюдаемость

Whilly проектируется как auditable system. Он пишет append-only events,
сохраняет audit trail в Postgres, может зеркалировать события в JSONL, отдаёт
dashboard, SSE stream, Prometheus metrics, health endpoints и worker heartbeat.

По этим данным оператор может восстановить, что происходило с задачей: кто её
claim'нул, какой runner запускался, чем завершилось выполнение, где возникла
ошибка и какие переходы состояния были сделаны.

## Human-In-The-Loop

Whilly сохраняет контроль за человеком на критических этапах. Human review может
проходить через PR review, handoff backend, dashboard, issue/Jira comments или
checkpoint evidence. `BLOCKED` и `HUMAN_LOOP` сейчас не являются core task
statuses; это целевые checkpoint concepts из документационного пакета.

Это важно: Whilly orchestrates agents; it does not magically make agent output
correct. Его ценность не в неограниченной автономности, а в управляемом
ускорении.

## Что Работает Сейчас

Текущая версия лучше всего описывается так:

> Issue-driven AI task orchestrator для одного рабочего репозитория или
> workspace, с Postgres-backed task queue, deterministic state machine, worker
> execution, runner abstraction, audit events и базовыми safety gates.

Она уже подходит для:

- bugfix tasks;
- feature tasks;
- refactoring;
- test generation;
- documentation updates;
- structured task plans;
- controlled local/remote worker execution;
- observability of task lifecycle.

## Чего Не Нужно Обещать

Документация и демо не должны обещать то, что core worker loop пока не
гарантирует:

- полноценное multi-repo execution;
- автоматический PR review feedback loop;
- обязательный CI/lint verification без настроенных verification commands;
- полноценную sandbox/VM isolation;
- semantic long-term memory;
- надёжный git rollback;
- автономный production release без человека.

Эти направления могут развиваться поверх текущей архитектуры, но их нельзя
подавать как уже решённые гарантии.

## Целевое Состояние

Целевое состояние Whilly — configurable orchestration layer для разных типов
инженерных проектов. Project config должен задавать sources, pipeline stages,
quality gates, verification steps, runners, sinks и human approval points.

Примеры целевых доменов:

- Python backend: issue intake, dependency analysis, implementation, unit tests,
  lint, PR creation, human review.
- GraphQL API: schema diff, resolver impact analysis, generated API tests,
  contract tests, backward compatibility checks.
- ETL/data pipelines: source/target validation, data quality checks, STLC/QA
  workflow, sample run, regression validation.
- Documentation-heavy projects: PRD intake, doc generation, consistency checks,
  human approval.

## Архитектура Коротко

```
Sources -> Plan/task model -> Postgres queue -> Worker claim -> Runner/backend
                         |              |                 |
                         v              v                 v
                 Decision gates    Audit events      Human review
                         |              |                 |
                         +---- Dashboard / SSE / metrics / health
```

Postgres — source of truth для plans, tasks, workers и events. Control plane
формирует транзакционные переходы состояния. Workers — stateless pollers,
которые claim'ят задачи, запускают runner/backend и репортят результат.

## Читать Дальше

- [Архитектура v4]({{ site.baseurl }}/Whilly-v4-Architecture)
- [Worker HTTP Protocol]({{ site.baseurl }}/Whilly-v4-Worker-Protocol)
- [Миграция с v3]({{ site.baseurl }}/Whilly-v4-Migration-from-v3)
- [Project config]({{ site.baseurl }}/Project-Config)
- [Current vs Target]({{ site.baseurl }}/Current-vs-Target)
