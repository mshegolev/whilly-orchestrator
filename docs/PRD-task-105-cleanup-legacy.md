# PRD: Whilly v4.1 — Legacy v3 Codebase Cleanup

| Поле | Значение |
|------|----------|
| Автор | QA Team |
| Дата | 2026-04-29 |
| Статус | Draft |
| Репозиторий | /opt/develop/whilly-orchestrator |
| Базовая версия | 4.0.0 (commit `09372ea`) |
| Целевая версия | 4.1.0 |

## 1. Контекст и Мотивация

Whilly v4.0.0 (2026-04-29) перевёл оркестратор на hexagonal-архитектуру (`whilly/core`, `whilly/adapters`, `whilly/cli`, `whilly/worker`) с Postgres-бэкендом и HTTP remote workers. При этом legacy v3-код был **оставлен на один релизный цикл** для обратной совместимости:

> *"Legacy v3 CLI lives in `whilly/cli_legacy.py` for one release cycle and will be removed in a v4.1+ follow-up."* — CHANGELOG.md, v4.0.0

**Текущее состояние (боли):**

| Проблема | Влияние |
|----------|---------|
| `cli_legacy.py` (~900 строк, 37 KB) — полный v3 оркестратор, не используется в v4 | Увеличивает cognitive load, ломает IDE-навигацию, wildcard re-export в `cli/__init__.py` загрязняет namespace |
| `tmux_runner.py` (190 строк) — tmux-сессии для агентов | Мёртвый код, импортируется только из `cli_legacy.py` и 1 тест-файла |
| `worktree_runner.py` (383 строк) — plan-level workspace + per-task worktree | Мёртвый код, импортируется только из `cli_legacy.py` |
| CLI-флаги `--workspace`, `--worktree` + config-поля `WORKTREE`, `USE_WORKSPACE` | Флаги по умолчанию `False` с v3.3.0, маршрутизируются через legacy CLI |
| README.md строки 71–364 — v3 документация с предупреждением "do not work on v4" | Путает новых пользователей, дублирует информацию |
| `whilly/dashboard.py:1259` — help text содержит `WHILLY_WORKTREE=1` | Ссылка на удалённый config-параметр |
| `whilly/config.py` строки 89–90 — `WORKTREE` и `USE_WORKSPACE` поля | Dead config, не используется v4 dispatcher'ом |
| Тесты `test_project_board.py`, `test_log_viewer.py` — импортируют legacy-символы через wildcard `from whilly.cli import` | Сломаются после удаления `cli_legacy` |

**Цель:** Оставить **только v4 codebase**. После cleanup: все тесты проходят, `ruff` чист, `mypy --strict whilly/core/` проходит, `import-linter` контракт сохранён.

## 2. Целевая аудитория

| Роль | Что использует | Частота |
|------|---------------|---------|
| Разработчик (contributor) | Кодовая база, IDE, `ruff`, `mypy` | Ежедневно |
| QA-инженер | `pytest`, CI pipeline, CHANGELOG | При каждом MR |
| DevOps | `pyproject.toml`, entry points, CI | При релизе |
| Новый пользователь | README.md, `whilly --help` | При onboarding |

## 3. User Stories

| # | User Story | Фаза |
|---|-----------|------|
| US-1 | Как разработчик, я хочу открыть `whilly/` и видеть только v4 модули, чтобы не путаться между legacy и актуальным кодом | Phase 1 |
| US-2 | Как QA-инженер, я хочу чтобы `pytest -q` проходил без legacy-зависимостей, чтобы тесты отражали реальный codebase | Phase 1 |
| US-3 | Как новый пользователь, я хочу читать README и видеть только актуальные команды (`whilly run`, `whilly plan`, `whilly init`), чтобы не пробовать несуществующие v3-примеры | Phase 2 |
| US-4 | Как DevOps-инженер, я хочу чтобы `pyproject.toml` не содержал зависимостей, нужных только legacy-коду (e.g. `psutil` для tmux) | Phase 2 |
| US-5 | Как contributor, я хочу видеть в CHANGELOG чёткий перечень удалённого, чтобы понимать breaking changes при обновлении | Phase 2 |

## 4. Функциональные требования

### Phase 1 — Удаление legacy-модулей

| ID | Требование | Acceptance Criteria |
|----|-----------|-------------------|
| F1.1 | Удалить файл `whilly/cli_legacy.py` | Файл отсутствует в дереве; `git status` показывает deleted; `grep -r cli_legacy whilly/` — 0 совпадений (кроме CHANGELOG) |
| F1.2 | Удалить файл `whilly/tmux_runner.py` | Файл отсутствует; `grep -r tmux_runner whilly/` — 0 совпадений (кроме CHANGELOG); `from whilly.tmux_runner` нигде не импортируется |
| F1.3 | Удалить файл `whilly/worktree_runner.py` | Файл отсутствует; `grep -r worktree_runner whilly/` — 0 совпадений (кроме CHANGELOG) |
| F1.4 | Переписать `whilly/cli/__init__.py` — убрать wildcard import из `cli_legacy`, убрать legacy fallback в `main()` | `from whilly.cli_legacy` нигде в пакете; `main()` маршрутизирует только v4 subcommands (`plan`, `run`, `dashboard`, `init`); неизвестная subcommand → help/error, а не legacy fallback |
| F1.5 | Удалить config-поля `WORKTREE` и `USE_WORKSPACE` из `WhillyConfig` (`whilly/config.py:89-90`) | `grep -r "WORKTREE\|USE_WORKSPACE" whilly/config.py` — 0 совпадений; `WhillyConfig.from_env()` не читает `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` |
| F1.6 | Удалить строку `WHILLY_WORKTREE=1` из help text в `whilly/dashboard.py:1259` | `grep WHILLY_WORKTREE whilly/dashboard.py` — 0 совпадений |
| F1.7 | Вычистить `whilly/doctor.py` — убрать проверку `.whilly_workspaces/` и `.whilly_worktrees/`, поля `orphan_workspaces` / `orphan_worktrees` из `DoctorReport` | `grep -r "workspaces\|worktrees" whilly/doctor.py` — 0 совпадений; `DoctorReport` не содержит полей `orphan_workspaces`, `orphan_worktrees` |
| F1.8 | Обновить `whilly/agents/__init__.py` — убрать ссылку на `tmux_runner.launch_agent` из docstring (строка 79) | `grep tmux_runner whilly/agents/__init__.py` — 0 совпадений |
| F1.9 | Удалить / переписать тесты tmux_runner backend wiring: `tests/test_agent_backend_wiring.py` (строки 119–230) | Тесты tmux_runner backend wiring удалены или переписаны на v4 adapter; `pytest tests/test_agent_backend_wiring.py` — PASSED |
| F1.10 | Удалить / переписать `tests/test_project_board.py` — импортирует `_finalise_project_board` из `whilly.cli` (legacy symbol через wildcard) | Тест удалён или переписан на v4 эквивалент; `pytest tests/test_project_board.py` — PASSED или файл отсутствует |
| F1.11 | Удалить / переписать `tests/test_log_viewer.py` — импортирует `_log_event` из `whilly.cli` (legacy symbol через wildcard) | Тест удалён или переписан; `pytest tests/test_log_viewer.py` — PASSED или файл отсутствует |
| F1.12 | Обновить `tests/test_doctor.py` — удалить `test_orphan_workspaces_detected` | Тест удалён; `pytest tests/test_doctor.py` — PASSED |
| F1.13 | Все тесты проходят: `pytest -q` → exit code 0 | Полный прогон без failures и errors |
| F1.14 | Линтинг чист: `python3 -m ruff check whilly/ tests/` → exit code 0 | Ноль warnings, ноль errors |
| F1.15 | Strict mypy на core: `mypy --strict whilly/core/` → exit code 0 | Success, 0 errors |
| F1.16 | Import-linter контракт сохранён: `lint-imports` → exit code 0 | Контракт `core-purity` в `.importlinter` проходит |

### Phase 2 — Документация и cleanup

| ID | Требование | Acceptance Criteria |
|----|-----------|-------------------|
| F2.1 | Переписать README.md — удалить строки 71–364 (v3 секции), заменить на актуальное v4 описание | Нет упоминания tmux runner, plan-level workspace, `--tasks tasks.json`, `whilly --resume` как v3-примеров; Python badge: `3.12+` (не `3.10+`) |
| F2.2 | Обновить README.md секцию "Features" — убрать "Parallel execution — tmux panes or git worktrees" | `grep -i "tmux panes\|git worktrees" README.md` — 0 совпадений; features описывают v4 distributed workers |
| F2.3 | Обновить CHANGELOG.md — добавить секцию `## [4.1.0] — YYYY-MM-DD` с перечнем удалённого | Секция `[4.1.0]` содержит: Removed `cli_legacy.py`, Removed `tmux_runner.py`, Removed `worktree_runner.py`, Removed `--workspace`/`--worktree` flags |
| F2.4 | Обновить `docs/Whilly-Usage.md` — удалить env vars `WHILLY_WORKTREE`, `WHILLY_USE_WORKSPACE` | `grep -i "WHILLY_WORKTREE\|WHILLY_USE_WORKSPACE" docs/Whilly-Usage.md` — 0 совпадений |
| F2.5 | Обновить `docs/Whilly-Interfaces-and-Tasks.md` — убрать описания `tmux_runner`, `worktree_runner` модулей | `grep -i "tmux_runner\|worktree_runner" docs/Whilly-Interfaces-and-Tasks.md` — 0 совпадений |
| F2.6 | Обновить `CLAUDE.md` — убрать секции, описывающие v3 workspace/worktree/tmux логику | CLAUDE.md описывает только v4 архитектуру; `grep -c "tmux_runner\|worktree_runner\|cli_legacy\|plan-level workspace" CLAUDE.md` ≤ 2 (только в "what was removed" контексте) |
| F2.7 | Удалить зависимость `psutil` из `pyproject.toml` если она нужна только legacy-коду | `grep psutil whilly/*.py whilly/**/*.py` — используется в `resource_monitor.py` → вероятно остаётся; иначе удалить |
| F2.8 | Проверить `whilly/prd_generator.py:178` — упоминание `cli_legacy` в комментарии | Комментарий обновлён или удалён |

## 5. Не-цели

| # | Что НЕ входит в scope |
|---|----------------------|
| NG-1 | Рефакторинг v4 модулей (`whilly/core`, `whilly/adapters`, `whilly/worker`) — только удаление legacy |
| NG-2 | Добавление новой функциональности (новые subcommands, новые adapters) |
| NG-3 | Миграция данных из v3 `.whilly_state.json` → Postgres (отдельная задача) |
| NG-4 | Удаление `whilly/orchestrator.py`, `whilly/task_manager.py` и других shared-модулей, которые могут использоваться и v4 |
| NG-5 | Изменение `.importlinter` контракта — только проверка что он проходит |
| NG-6 | Удаление модулей `whilly/dashboard.py`, `whilly/reporter.py`, `whilly/state_store.py` — они могут использоваться в v4 косвенно; требуют отдельного анализа |
| NG-7 | Изменение entry points в `pyproject.toml` (они уже указывают на v4) |

## 6. Архитектура

### Текущая структура (до cleanup)

```
whilly/
├── __init__.py
├── __main__.py
├── cli_legacy.py          ← УДАЛИТЬ (F1.1)
├── tmux_runner.py         ← УДАЛИТЬ (F1.2)
├── worktree_runner.py     ← УДАЛИТЬ (F1.3)
├── config.py              ← ИЗМЕНИТЬ: убрать WORKTREE, USE_WORKSPACE (F1.5)
├── dashboard.py           ← ИЗМЕНИТЬ: убрать WHILLY_WORKTREE из help (F1.6)
├── doctor.py              ← ИЗМЕНИТЬ: убрать workspace/worktree detection (F1.7)
├── agents/__init__.py     ← ИЗМЕНИТЬ: убрать ссылку на tmux_runner (F1.8)
├── prd_generator.py       ← ИЗМЕНИТЬ: убрать комментарий про cli_legacy (F2.8)
├── cli/
│   ├── __init__.py        ← ПЕРЕПИСАТЬ: убрать legacy fallback (F1.4)
│   ├── dashboard.py
│   ├── init.py
│   ├── plan.py
│   ├── run.py
│   └── worker.py
├── core/                  ← НЕ ТРОГАТЬ
│   ├── models.py
│   ├── prompts.py
│   ├── scheduler.py
│   └── state_machine.py
├── adapters/              ← НЕ ТРОГАТЬ
│   ├── db/
│   ├── filesystem/
│   ├── runner/
│   └── transport/
└── worker/                ← НЕ ТРОГАТЬ
    ├── local.py
    ├── main.py
    └── remote.py
```

### Целевая структура (после cleanup)

```
whilly/
├── __init__.py
├── __main__.py
├── config.py              ✓ без WORKTREE / USE_WORKSPACE
├── dashboard.py           ✓ без WHILLY_WORKTREE в help
├── doctor.py              ✓ без workspace/worktree detection
├── agents/__init__.py     ✓ без ссылки на tmux_runner
├── cli/
│   ├── __init__.py        ✓ чистый v4 dispatcher (plan/run/dashboard/init)
│   ├── dashboard.py
│   ├── init.py
│   ├── plan.py
│   ├── run.py
│   └── worker.py
├── core/                  (без изменений)
├── adapters/              (без изменений)
├── worker/                (без изменений)
└── [другие v4 модули]     (без изменений)
```

### Граф зависимостей (что ломается при удалении)

```
cli_legacy.py ──imports──► tmux_runner.py
cli_legacy.py ──imports──► worktree_runner.py
cli/__init__.py ──wildcard import──► cli_legacy.py
    └─► tests/test_project_board.py  (imports _finalise_project_board)
    └─► tests/test_log_viewer.py     (imports _log_event)
    └─► tests/test_agent_backend_wiring.py (imports main + tmux_runner directly)
tests/test_agent_backend_wiring.py ──imports──► tmux_runner.py
tests/test_doctor.py ──tests──► workspace detection logic
```

Все остальные модули **не зависят** от удаляемых файлов. Удаление безопасно при условии переписывания `cli/__init__.py` и обновления затронутых тестов.

## 7. Фазы реализации

### Phase 1 — Удаление legacy-модулей и фиксация тестов (1 неделя)

| Шаг | Задача | Effort | Требования |
|-----|--------|--------|-----------|
| 1.1 | Удалить `whilly/cli_legacy.py` | 0.5ч | F1.1 |
| 1.2 | Удалить `whilly/tmux_runner.py` | 0.25ч | F1.2 |
| 1.3 | Удалить `whilly/worktree_runner.py` | 0.25ч | F1.3 |
| 1.4 | Переписать `whilly/cli/__init__.py` — чистый v4 dispatcher | 2ч | F1.4 |
| 1.5 | Вычистить `whilly/config.py` — убрать dead config fields | 0.5ч | F1.5 |
| 1.6 | Вычистить `whilly/dashboard.py` — убрать WHILLY_WORKTREE из help | 0.25ч | F1.6 |
| 1.7 | Вычистить `whilly/doctor.py` | 1ч | F1.7 |
| 1.8 | Обновить `whilly/agents/__init__.py` docstring | 0.25ч | F1.8 |
| 1.9 | Обновить/удалить legacy-тесты в `test_agent_backend_wiring.py` | 1.5ч | F1.9 |
| 1.10 | Удалить/переписать `test_project_board.py` (legacy symbol `_finalise_project_board`) | 1ч | F1.10 |
| 1.11 | Удалить/переписать `test_log_viewer.py` (legacy symbol `_log_event`) | 0.5ч | F1.11 |
| 1.12 | Удалить `test_orphan_workspaces_detected` из `test_doctor.py` | 0.25ч | F1.12 |
| 1.13 | Прогнать полный test suite + lint + mypy + import-linter | 1ч | F1.13–F1.16 |
| **Итого Phase 1** | | **~9.25ч** | |

### Phase 2 — Документация и финализация (1 неделя)

| Шаг | Задача | Effort | Требования |
|-----|--------|--------|-----------|
| 2.1 | Переписать v3 секции README.md | 3ч | F2.1, F2.2 |
| 2.2 | Обновить CHANGELOG.md — секция 4.1.0 | 0.5ч | F2.3 |
| 2.3 | Обновить `docs/Whilly-Usage.md` | 1ч | F2.4 |
| 2.4 | Обновить `docs/Whilly-Interfaces-and-Tasks.md` | 1ч | F2.5 |
| 2.5 | Обновить `CLAUDE.md` | 1.5ч | F2.6 |
| 2.6 | Проверить / удалить `psutil` из зависимостей | 0.5ч | F2.7 |
| 2.7 | Вычистить оставшиеся ссылки (prd_generator, etc.) | 0.5ч | F2.8 |
| 2.8 | Финальный прогон всех quality gates | 0.5ч | F1.13–F1.16 |
| **Итого Phase 2** | | **~8.5ч** | |

**Общий effort: ~18 часов (2–3 рабочих дня)**

## 8. Метрики успеха

| Метрика | Текущее | Цель |
|---------|---------|------|
| Файлы legacy v3 в `whilly/` | 3 (`cli_legacy.py`, `tmux_runner.py`, `worktree_runner.py`) | 0 |
| Строки legacy-кода | ~1 470 (900 + 190 + 383) | 0 |
| Dead config fields (`WORKTREE`, `USE_WORKSPACE`) | 2 | 0 |
| `pytest -q` exit code | 0 (с legacy) | 0 (без legacy) |
| `ruff check` violations | 0 | 0 |
| `mypy --strict whilly/core/` errors | 0 | 0 |
| `lint-imports` exit code | 0 | 0 |
| README v3 deprecated sections (строки) | ~290 (строки 71–364) | 0 |
| `grep -rc "cli_legacy\|tmux_runner\|worktree_runner" whilly/` | ≥12 | 0 |

## 9. Тестирование

### Команды для проверки после каждого шага

```bash
# 1. Полный test suite
pytest -q

# 2. Ruff lint + format check
python3 -m ruff check whilly/ tests/
python3 -m ruff format --check whilly/ tests/

# 3. Strict mypy на core
mypy --strict whilly/core/

# 4. Import-linter (hexagonal boundary)
lint-imports

# 5. Проверка отсутствия legacy-ссылок
grep -rn "cli_legacy\|tmux_runner\|worktree_runner" whilly/ --include="*.py"
# Ожидаемый результат: 0 совпадений

# 6. Проверка отсутствия dead config
grep -n "WORKTREE\|USE_WORKSPACE" whilly/config.py
# Ожидаемый результат: 0 совпадений

# 7. Проверка что v4 entry point работает
whilly --help
whilly plan --help
whilly run --help
whilly init --help
whilly dashboard --help

# 8. Проверка что legacy CLI не работает (expected: error/help)
whilly --tasks tasks.json 2>&1 | head -5
# Ожидаемый результат: "Unknown command" или help text, не v3 loop

# 9. Проверка README не содержит deprecated v3
grep -c "tmux panes\|plan-level workspace\|--tasks tasks.json" README.md
# Ожидаемый результат: 0
```

### Smoke-тест после полного cleanup

```bash
# Полный quality gate (одна команда)
pytest -q \
  && python3 -m ruff check whilly/ tests/ \
  && python3 -m ruff format --check whilly/ tests/ \
  && mypy --strict whilly/core/ \
  && lint-imports \
  && echo "✅ All quality gates passed"
```

## 10. Зависимости

| Пакет | Версия | Статус после cleanup |
|-------|--------|---------------------|
| `rich` | ≥13.0.0 | Остаётся (v4 dashboard) |
| `pydantic` | ≥2.6 | Остаётся (wire DTOs) |
| `typer` | ≥0.12 | Остаётся (v4 sub-CLI) |
| `psutil` | ≥5.9.0 | **Проверить** — используется в `resource_monitor.py` → вероятно остаётся (F2.7) |
| `platformdirs` | ≥4.0 | Остаётся (user config paths, `secrets.py`) |
| `keyring` | ≥24.0 | Остаётся (secret store, `secrets.py`) |
| `pytest` | dev | Остаётся |
| `ruff` | dev, ==0.11.5 | Остаётся |
| `mypy` | dev, ≥1.8 | Остаётся |
| `import-linter` | dev, ≥2.0 | Остаётся |

## 11. Risks & Mitigations

| Риск | Вероятность | Влияние | Mitigation |
|------|------------|---------|-----------|
| Тесты v3, замаскированные wildcard import в `cli/__init__.py`, сломаются после удаления `cli_legacy` | Высокая | Среднее | `grep -rn "from whilly.cli import" tests/` перед удалением — найти все прямые зависимости (`_finalise_project_board`, `_log_event`, `_emit_json`); починить поштучно |
| Внешние скрипты/CI используют `whilly --tasks` или `whilly --resume` (v3 CLI) | Средняя | Высокое | Проверить `.github/workflows/`, `Makefile`, `scripts/` на v3 CLI вызовы; добавить deprecation error message с подсказкой v4 эквивалента |
| `psutil` используется не только в legacy (`resource_monitor.py`) | Средняя | Низкое | `grep -rn "psutil" whilly/` перед удалением; если используется → оставить |
| Shared-модули (`orchestrator.py`, `task_manager.py`, `reporter.py`) ломаются без `cli_legacy` импортов | Низкая | Высокое | Эти модули не импортируют `cli_legacy` напрямую; проверить `grep -rn "import.*cli_legacy" whilly/` |
| CLAUDE.md описывает v3 архитектуру, Claude Code будет генерировать несоответствующий код | Средняя | Среднее | Обновить CLAUDE.md в Phase 2 (F2.6); убрать `run_plan`, `build_task_prompt`, workspace/worktree описания |
| Пользователи, обновившиеся с v3, не поймут почему `--workspace` / `--worktree` не работают | Средняя | Низкое | Добавить в `whilly --help` секцию "Removed in v4.1" или informative error при попытке использовать старые флаги |
| `whilly/dashboard.py` help text ссылается на удалённый `WHILLY_WORKTREE` | Высокая | Низкое | Удалить строку в Phase 1 (F1.6), включить в тот же коммит что и config cleanup |

---

`★ Insight ─────────────────────────────────────`
**Что PRD добавляет поверх "просто удалить файлы":**
1. **Blast radius mapping** — граф зависимостей показал 5 точек поломки (3 файла удаления + wildcard re-export + 4 тест-файла), а не очевидные 3 файла.
2. **Порядок имеет значение** — удалять нужно "снизу вверх": тесты → `cli/__init__.py` → сами файлы. Иначе test suite падает целиком и отлаживать сложнее.
3. **`psutil` — ложный друг** — кажется legacy, но `resource_monitor.py` его использует в v4. PRD фиксирует это как проверочный шаг (F2.7), а не безусловное удаление.
`─────────────────────────────────────────────────`