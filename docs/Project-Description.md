---
title: О проекте
layout: default
nav_order: 2
description: "Whilly Orchestrator — распределённый оркестратор LLM-агентов на Postgres + FastAPI + remote workers."
permalink: /project-description
---

# Whilly Orchestrator
{: .fs-9 }

Распределённый оркестратор LLM-агентов: Postgres-очередь, FastAPI control plane, remote workers через HTTP.
{: .fs-5 .fw-300 }

📦 Репозиторий: [mshegolev/whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator)
📦 PyPI: [whilly-orchestrator](https://pypi.org/project/whilly-orchestrator/) · [whilly-worker](https://pypi.org/project/whilly-worker/)
🏷 Релиз: [v4.0.0](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v4.0.0)

---

Распределённый оркестратор LLM-агентов: импортирует план задач (JSON или из GitHub Issues), кладёт в Postgres-очередь, и прогоняет через пайплайн `Import → Claim → Run (Claude CLI) → Complete | Fail`. Один control plane, N удалённых worker'ов, общая шина — таблица `tasks` под optimistic locking + `SKIP LOCKED` ([детали в Architecture]({{ site.baseurl }}/Whilly-v4-Architecture#concurrency-primitives)).

**Основные фичи:**

- **Hexagonal-архитектура** ([Whilly-v4-Architecture]({{ site.baseurl }}/Whilly-v4-Architecture)): [`whilly/core/`](https://github.com/mshegolev/whilly-orchestrator/tree/main/whilly/core) — pure domain (state machine + DAG scheduler) с нулевыми внешними зависимостями, защищён [`.importlinter`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.importlinter) контрактом в CI ([SC-6](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/PRD-refactoring-1.md)). Всё I/O живёт строго в [`whilly/adapters/*`](https://github.com/mshegolev/whilly-orchestrator/tree/main/whilly/adapters).
- **Worker может умереть в любой момент** — visibility-timeout sweep ([`release_stale_tasks` в repository.py](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/repository.py)) вернёт его задачу в `PENDING` через 60 секунд, peer worker подберёт. SIGKILL посреди задачи — штатный сценарий, гарантия [SC-2 (worker fault tolerance)](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/PRD-refactoring-1.md), запинено [`test_phase6_resilience.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_phase6_resilience.py).
- **Полный аудит-лог** в таблице `events` ([CLAIM/START/COMPLETE/FAIL/RELEASE]({{ site.baseurl }}/Whilly-v4-Architecture#audit-log)) — пишется в той же транзакции что и `UPDATE tasks`, рассинхрон невозможен.
- **Удалённый worker ставится одной командой** `pip install whilly-worker` ([PyPI](https://pypi.org/project/whilly-worker/)) — мета-пакет тащит только `httpx` + `pydantic`, без FastAPI/asyncpg/SQLAlchemy. Контракт SC-6 («worker box не имеет server-side зависимостей на диске») гарантируется extras-split'ом в [`pyproject.toml`](https://github.com/mshegolev/whilly-orchestrator/blob/main/pyproject.toml) *и* статически через [`lint-imports`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.importlinter).
- **Все 6 PRD Success Criteria** ([SC-1..SC-6]({{ site.baseurl }}/v4.0-release-checklist#success-criteria--all-6-must-be-)) закрываются одним pytest'ом — [`tests/integration/test_release_smoke.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_release_smoke.py).

**Технический стек:** Python 3.12+, Postgres 15+, FastAPI + asyncpg + Alembic + uvicorn (server), httpx + pydantic (worker), Claude CLI через `asyncio.create_subprocess_exec`, Rich для TUI-дашборда, Typer для CLI, pytest + testcontainers + import-linter + mypy --strict для качества.

## Что уже работает

- **7 CI jobs зелёные** ([`.github/workflows/ci.yml`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.github/workflows/ci.yml)): lint, [arch-guard](https://github.com/mshegolev/whilly-orchestrator/blob/main/.github/workflows/ci.yml), type-check, test, agent-backends, build, publish. `mypy --strict whilly/core/` чистый ([SC-5/NFR-4](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/PRD-refactoring-1.md)), coverage `whilly/core` = 100% (≥80% gate).
- **Два deployment shape'а** ([Architecture: Data flow]({{ site.baseurl }}/Whilly-v4-Architecture#data-flow--local-worker-shape)): local (`whilly run --plan <id>` — control plane embedded в worker процесс) и distributed (отдельный `uvicorn` + N `whilly-worker --connect URL`).
- **Полный набор HTTP-эндпоинтов** ([Worker Protocol]({{ site.baseurl }}/Whilly-v4-Worker-Protocol#endpoints-overview)): `/workers/register`, `/heartbeat`, `/tasks/claim` (long-polled), `/tasks/{id}/{complete,fail,release}` с bearer-аутентификацией ([Worker Protocol: Authentication]({{ site.baseurl }}/Whilly-v4-Worker-Protocol#authentication)).
- **Postgres recovery**: visibility-timeout sweep ([TASK-025a](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/transport/server.py)), heartbeat-driven offline detection ([TASK-025b](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/repository.py)), optimistic locking на каждой записи ([repository.py `_COMPLETE_SQL`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/repository.py)). Концепция «один SIGKILL = одна потерянная попытка, не один потерянный план» — пинится [`test_phase6_resilience.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_phase6_resilience.py) и [`test_worker_kill.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/tests/integration/test_worker_kill.py).
- **v3.x line зафиксирована** на тэге [`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final) (single-process Ralph loop с tmux/worktree, TRIZ analyzer, Decision Gate, PRD wizard) — для тех кому распределёнка не нужна. Migration: [Whilly-v4-Migration-from-v3]({{ site.baseurl }}/Whilly-v4-Migration-from-v3).

## Что в планах

- **Per-worker bearer rotation** ([текущий статус: shared-bearer caveat]({{ site.baseurl }}/Whilly-v4-Worker-Protocol#authentication)): сейчас один shared токен на весь кластер, валидируется через `Authorization: Bearer X`. Нужно научить server проверять hash в [`workers.token_hash`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/schema.sql) чтобы можно было отзывать токен на одного воркера.
- **Budget guards** (`WHILLY_BUDGET_USD`) — у v3 был, на v4 пока выпилен ([release-checklist: Out of scope]({{ site.baseurl }}/v4.0-release-checklist#out-of-scope-for-v40--tracked-for-v41)).
- **Удалить [`whilly/cli_legacy.py`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/cli_legacy.py)** целиком и docstring-rewrite README, который сейчас наполовину про v3.
- **PRD wizard / `whilly --init` / TRIZ analyzer** — портировать с v3 на v4-овую плановую модель (сейчас они ходят к старому файловому `tasks.json` и не дружат с Postgres).
- **Forge pipeline** (Issue → PR end-to-end) — частично shipped в v3 как `scripts/whilly_e2e_*.py`, нужно выделить в `whilly/forge/` с явными FR-1..FR-11 этапами по vNext-плану из [README](https://github.com/mshegolev/whilly-orchestrator/blob/main/README.md#vnext--whilly-forge-issue--pr).

## Основные сложности

- **State-machine gap для remote shape:** изначально HTTP transport не имел `/tasks/{id}/start` endpoint'а, а [`_COMPLETE_SQL`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/adapters/db/repository.py) фильтровал по `status = 'IN_PROGRESS'`. Remote worker делал `claim → run → complete` и каждый раз получал [409 VersionConflict]({{ site.baseurl }}/Whilly-v4-Worker-Protocol#post-taskstask_idcomplete). Решение — релакс state machine: добавили валидное ребро [`(COMPLETE, CLAIMED) → DONE`](https://github.com/mshegolev/whilly-orchestrator/blob/main/whilly/core/state_machine.py) (TASK-024a). Дешевле чем добавлять `/start` RPC ради no-op round-trip.
- **Plan-level workspace оказался cure-worse-than-disease:** в v3.0–3.2 делали `git worktree add` по умолчанию для каждого плана. Сабпроцессы с абсолютными путями в `.venv`, pending changes, и confused git status в реальных пилотах сожгли больше часов чем сэкономили. С [v3.3.0 off by default]({{ site.baseurl }}/), опт-ин через `--workspace`. Урок: нельзя по умолчанию включать фичу которая меняет cwd сабпроцессов.
- **Авто-релизный workflow выстрелил молча:** push'нул `git tag v4.0.0` не проверив [`.github/workflows/release.yml`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.github/workflows/release.yml). У того было `on: push: tags: ["v*"]` с PyPI [trusted publisher](https://docs.pypi.org/trusted-publishers/) — релиз ушёл на PyPI без явного confirm'а. PyPI immutable, откатить нельзя. Плюс `release.yml` публиковал только root package, а meta-package [`whilly-worker`](https://pypi.org/project/whilly-worker/) пришлось загружать отдельно через `twine` — partial release ловушка. Записал правило: перед любым `git push origin v*` — `grep tags: .github/workflows/*.yml` и предупреждать пользователя.
- **Two-branch git топология:** v4 разрабатывался на feature ветке + plan-level workspace worktree (отдельная `whilly/workspace/...` ветка где жил собственно код). Долго не было merge'а workspace → feature, поэтому в [`feat/v4-rewrite`](https://github.com/mshegolev/whilly-orchestrator/commits/main/) лежали только `chore(planning)` коммиты-маркеры, а реальные 54 коммита кода висели сбоку. Финальный `--no-ff` merge с одним конфликтом в `.planning/refactoring-1_tasks.json` (резолвили `--ours`) сработал, но топологию стоит спрямить с самого начала следующего больших рефакторинга.

---

## Ссылки на документацию

- [Архитектура v4]({{ site.baseurl }}/Whilly-v4-Architecture) — Hexagonal layout, data flow, concurrency primitives
- [Миграция с v3]({{ site.baseurl }}/Whilly-v4-Migration-from-v3) — env-var mapping, breaking changes
- [Worker HTTP Protocol]({{ site.baseurl }}/Whilly-v4-Worker-Protocol) — спецификация HTTP API для не-Python воркеров
- [Release Checklist]({{ site.baseurl }}/v4.0-release-checklist) — SC-1..SC-6 gates на каждый релиз
- [PRD-refactoring-1]({{ site.baseurl }}/PRD-refactoring-1) — оригинальный PRD v4 рефакторинга
