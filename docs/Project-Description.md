---
title: О проекте
layout: default
nav_order: 2
description: "Whilly Orchestrator — распределённый оркестратор LLM-агентов на Postgres + FastAPI + remote workers."
permalink: /project-description
---

# Whilly Orchestrator

📎 Репозиторий: [mshegolev/whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator) · 📦 PyPI: [whilly-orchestrator](https://pypi.org/project/whilly-orchestrator/) · [whilly-worker](https://pypi.org/project/whilly-worker/) · 🏷 [v4.0.0](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v4.0.0)

Распределённый оркестратор, который забирает задачи из очереди в Postgres и прогоняет их через распределённый пул воркеров по HTTP: `Import → Claim → Run (Claude CLI) → Complete | Fail → Audit Log` ([Architecture]({{ site.baseurl }}/Whilly-v4-Architecture)).

**Основные фичи:**

- Честная распределёнка: control plane на одной VM, воркеры на любых других — никакого общего диска, только HTTP ([Worker Protocol]({{ site.baseurl }}/Whilly-v4-Worker-Protocol)).
- Никаких гонок состояния благодаря транзакциям Postgres и `SKIP LOCKED` плюс optimistic locking на каждой записи ([repository.py](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/repository.py)).
- Гексагональная архитектура с чистым ядром [`whilly/core/`](https://github.com/mshegolev/whilly-orchestrator/tree/main/whilly/core), которое вообще ничего не знает про БД и сеть — за нарушение CI бьёт по рукам через [`.importlinter`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.importlinter).
- Воркеры сами тянут задачи через HTTP long-polling, так что NAT не помеха — control plane может вообще не знать где они стоят ([SC-3](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/PRD-refactoring-1.md)).
- SIGKILL воркера посреди задачи — штатный сценарий: visibility-timeout sweep вернёт его claim в `PENDING` за 30с, peer подберёт ([SC-2 / `test_phase6_resilience.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_phase6_resilience.py)).

**Технический стек:** Python 3.12+, PostgreSQL 15+, FastAPI, asyncpg, Alembic, httpx, pydantic, Claude CLI, Rich Live TUI, pytest + testcontainers + import-linter + mypy --strict.

## Что уже работает

- FastAPI control plane через [`uvicorn`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/transport/server.py), очередь задач в Postgres с optimistic locking + `SKIP LOCKED`, visibility-timeout sweep для упавших воркеров ([TASK-025a](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/transport/server.py)).
- Регистрация воркеров через bootstrap-token + system хартбитов для offline-detection ([TASK-025b](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/repository.py)).
- Импорт/экспорт планов из JSON, ASCII-граф зависимостей через `whilly plan show <id>` с обнаружением циклов ([SC-4 / `test_phase3_dag.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_phase3_dag.py)).
- Rich Live TUI dashboard над таблицей `tasks` плюс полный аудит-лог в таблице `events` ([Architecture: Audit log]({{ site.baseurl }}/Whilly-v4-Architecture#audit-log)).
- 7 CI jobs зелёные ([`.github/workflows/ci.yml`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.github/workflows/ci.yml)): lint, arch-guard (lint-imports + os.chdir grep), type-check (mypy --strict), test (coverage `whilly/core` = 100% при ≥80% gate), agent-backends, build, publish.
- Все 6 PRD Success Criteria закрываются одним pytest'ом — [`test_release_smoke.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_release_smoke.py).

## Что в планах

- Per-worker bearer rotation: сейчас один shared токен на весь кластер, надо научить server валидировать против `workers.token_hash` чтобы можно было отзывать индивидуально ([Worker Protocol: Authentication caveat]({{ site.baseurl }}/Whilly-v4-Worker-Protocol#authentication)).
- Лимиты по бюджету (`WHILLY_BUDGET_USD`) — у v3 был, на v4 пока выпилен ([Out-of-scope для v4.0]({{ site.baseurl }}/v4.0-release-checklist#out-of-scope-for-v40--tracked-for-v41)).
- Команда `whilly --reset PLAN.json` — сейчас приходится чистить таблицы вручную через psql ([Migration v3→v4]({{ site.baseurl }}/Whilly-v4-Migration-from-v3#cli-surface-mapping)).
- Удалить [`whilly/cli_legacy.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/cli_legacy.py) целиком и переписать v3-секцию README, которая сейчас наполовину про tmux/worktree.
- Портировать PRD wizard / `whilly --init` / TRIZ analyzer с v3 на v4-овую плановую модель — сейчас они ходят к старому файловому `tasks.json` и не дружат с Postgres.
- Forge pipeline (Issue → PR end-to-end): частично shipped в v3 как `scripts/whilly_e2e_*.py`, нужно выделить в `whilly/forge/` с явными FR-1..FR-11 этапами по vNext-плану из README.

## Основные сложности

- Пришлось пойти на big-bang rewrite и полностью сломать обратную совместимость, так как v3 на файлах (`.whilly_state.json`, `.whilly_workspaces/`) уже не масштабировалась ([Migration]({{ site.baseurl }}/Whilly-v4-Migration-from-v3#breaking-changes-summary)).
- Гексагоналка требовала много дисциплины: за попытку импорта `asyncpg` или `httpx` в [`whilly/core/`](https://github.com/mshegolev/whilly-orchestrator/tree/main/whilly/core) CI бьёт по рукам через `lint-imports`. Хорошо для архитектуры, болезненно когда хочется срезать угол.
- Много времени ушло на то, чтобы убитый по `kill -9` воркер не вешал систему, а его задача автоматически перехватывалась другими ([SC-2 / `test_phase6_resilience.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_phase6_resilience.py)). Visibility-timeout sweep + heartbeat-driven offline detection + dashboard, который не врёт ни в одной точке — это три согласованных движущиеся части.
- State-machine gap для remote shape: HTTP transport не имел `/tasks/{id}/start` endpoint'а, а `_COMPLETE_SQL` фильтровал по `IN_PROGRESS`. Remote worker делал `claim → run → complete` и каждый раз получал 409. Решили релаксом state machine — добавили валидное ребро `(COMPLETE, CLAIMED) → DONE` в [`whilly/core/state_machine.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/core/state_machine.py), а не тащить no-op `/start` RPC.
- Plan-level `git worktree` оказался cure-worse-than-disease: в v3.0–3.2 был включён по умолчанию, сабпроцессы с абсолютными путями в `.venv` и pending changes сожгли больше часов чем сэкономили. С v3.3.0 off by default. Урок: нельзя по умолчанию включать фичу которая меняет cwd сабпроцессов.
- Auto-релизный workflow выстрелил молча: push'нул `git tag v4.0.0` не проверив [`.github/workflows/release.yml`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.github/workflows/release.yml), у того было `on: push: tags: ["v*"]` с PyPI trusted publisher — релиз ушёл без явного confirm'а. PyPI immutable, откатить нельзя. Записал правило: перед любым `git push origin v*` грепать `tags:` в workflows.

---

## Документация

- [Архитектура v4]({{ site.baseurl }}/Whilly-v4-Architecture) — Hexagonal layout, data flow, concurrency primitives
- [Миграция с v3]({{ site.baseurl }}/Whilly-v4-Migration-from-v3) — env-var mapping, breaking changes
- [Worker HTTP Protocol]({{ site.baseurl }}/Whilly-v4-Worker-Protocol) — спецификация HTTP API
- [Release Checklist]({{ site.baseurl }}/v4.0-release-checklist) — SC-1..SC-6 gates
- [PRD-refactoring-1]({{ site.baseurl }}/PRD-refactoring-1) — оригинальный PRD v4
