# PRD — `whilly init` (PRD wizard port to v4)

**Version:** 0.1 (draft)
**Date:** 2026-04-29
**Owner:** v4.1 backlog (TASK-104a/b/c)
**Tracker:** [`.planning/v4-1_tasks.json`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.planning/v4-1_tasks.json)
**Design notes:** [`docs/Whilly-v41-Design-PRD-Wizard.md`](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/Whilly-v41-Design-PRD-Wizard.md)

## 1. Контекст

Whilly v3 имел интерактивный flow: оператор пишет `whilly --init "хочу X"`, Claude задаёт уточняющие вопросы и сохраняет PRD-документ, из PRD автоматически генерируется список задач, который сразу начинается выполняться. Этот flow был «продуктовой» поверхностью v3 — половина пользователей приходила именно через него.

В v4 эта поверхность отсутствует. Юзер v4 должен:
1. Вручную написать `tasks.json`.
2. Запустить `whilly plan import path/to/tasks.json`.
3. Запустить `whilly run --plan <id>`.

Это **regression** относительно v3 для всех use-case'ов где задача начинается с расплывчатой идеи, а не с готового списка задач. Гэп закрывает эта работа.

Параллельно — оригинальные `whilly/prd_wizard.py`, `whilly/prd_launcher.py`, `whilly/prd_generator.py` живут в repo, всё ещё рабочие, но дёргаются только через `whilly/cli_legacy.py` dispatcher fallback. Это дополнительный долг — пока эти файлы не порчированы или не удалены, мы не можем чисто удалить `cli_legacy.py` (TASK-105 в v4.1 backlog'е).

## 2. Цели

**G1.** Вернуть в v4 интерактивный flow «идея → PRD → план в Postgres → готов к запуску» эквивалентный v3 `whilly --init`, но запускающийся как `whilly init "..."`.

**G2.** Переиспользовать существующие модули (`prd_wizard.py`, `prd_launcher.py`, `prd_generator.py`, `config/prd_wizard_prompt.md`) **без переписывания их логики** — поменяться должен только финальный шаг записи плана.

**G3.** Подготовить почву к удалению `whilly/cli_legacy.py` целиком (TASK-105). После этой работы единственное что держит legacy — это TRIZ analyzer (TASK-104b) и Decision Gate (TASK-104c), которые не блокируют MVP `whilly init`.

## 3. Не-цели

**NG1.** Реализация TRIZ analyzer (TASK-104b) и Decision Gate (TASK-104c) — это отдельные задачи backlog'а. PRD wizard работает без них; они добавляются как опциональные pre-claim фильтры позже.

**NG2.** Web UI для wizard'а. Только terminal flow (interactive в TTY + tmux background для headless).

**NG3.** Поддержка не-Claude бэкендов в wizard'е. v3 имел `claude_handoff` / OpenCode — на v4 пока только Claude CLI. Multi-backend — отдельная задача (не в v4.1).

**NG4.** Authoring без Claude CLI на машине (эмулятор / cached prompts). Wizard требует `CLAUDE_BIN` на `$PATH`.

## 4. Functional Requirements

### FR-1. Console script entry

`whilly init "<описание идеи>"` доступна как подкоманда `whilly` console-script'а (тот же entry-point что `whilly plan` и `whilly run`).

### FR-2. Two execution modes

* **FR-2.1 Interactive (default in TTY).** `sys.stdin.isatty()` → `True` → вызывается `whilly.prd_launcher.run_interactive(idea, prd_path)` — Claude CLI запускается foreground в текущем терминале, юзер ведёт живой диалог.
* **FR-2.2 Headless (default outside TTY).** Если stdin не терминал — вызывается `whilly.prd_wizard.run_headless(idea, prd_path)` — Claude `-p` запускается single-shot, без интерактива.
* **FR-2.3 Override flags.** `--interactive` / `--headless` принудительно выбирают режим, игнорируя TTY-detection.

### FR-3. PRD output

Wizard сохраняет файл `docs/PRD-<slug>.md`. Slug вычисляется так:
* Если передан `--slug X` — используется `X` (sanitized: `[a-z0-9-]+`).
* Иначе — `_slugify(idea)` берёт первые 8 значимых слов и приводит к kebab-case.

Имя файла фиксированное: `docs/PRD-<slug>.md`. Если файл уже есть — wizard отказывает (FR-7) или предлагает пересоздать (`--force`).

### FR-4. Plan generation

После того как `docs/PRD-<slug>.md` существует:
1. Вызывается `whilly.prd_generator.generate_tasks_dict(prd_path, plan_id=slug)` (новая функция, см. Plan).
2. Возвращённый dict `{"project": ..., "tasks": [...]}` идёт в `whilly.adapters.filesystem.plan_io.import_plan_dict(payload, plan_id=slug)` (новая функция, см. Plan).
3. План попадает в Postgres: запись в `plans` + N записей в `tasks`.

### FR-5. Output and next steps

После успешного импорта `whilly init` печатает в stdout:

```
✓ PRD saved at docs/PRD-<slug>.md
✓ Plan <slug> imported (N tasks)

Next steps:
  whilly plan show <slug>
  whilly run --plan <slug>
```

Exit code `0`.

### FR-6. Skip import flag

`whilly init "..." --no-import` — wizard сохраняет PRD-файл, но НЕ импортирует план. Полезно для дебага (посмотреть PRD прежде чем коммитить в БД) и для use-case'ов где PRD используется отдельно.

### FR-7. Idempotency

Повторный запуск `whilly init` с тем же slug'ом:
* Если PRD-файл существует — отказ с сообщением `PRD-<slug>.md already exists; pass --force to overwrite or pick another slug`.
* `--force` — wizard перезаписывает PRD и (если нужно) делает `whilly plan reset <slug>` перед import (после того как TASK-103 реализован; пока — отказ с сообщением "plan exists, manual cleanup required").

### FR-8. Error handling

* Wizard завершился с не-нулевым кодом / без записи PRD → exit `1` с сообщением `wizard exited without saving PRD; rerun or check $CLAUDE_BIN`.
* `generate_tasks_dict` вернул пустой / невалидный план → exit `1`, PRD-файл оставлен на диске (для дебага).
* Ошибка БД на import → exit `1`, PRD-файл оставлен.
* `KeyboardInterrupt` посреди интерактива → exit `130` (POSIX SIGINT), PRD-файл сохранён только если уже был дописан Claude'ом.

## 5. Non-Functional Requirements

**NFR-1.** Никаких новых зависимостей. Работает с уже установленным `[server]` extras (нужен `asyncpg` для записи в БД).

**NFR-2.** `whilly init` как подкоманда не ломает существующие `whilly plan/run/dashboard`. Новый dispatcher branch добавляется к существующему argparse-дереву.

**NFR-3.** Code coverage для нового кода (`whilly/cli/init.py`, новые функции в `prd_generator.py` / `plan_io.py`) ≥ 80%.

**NFR-4.** Backward compatibility v3 не требуется (v3 line frozen на `v3-final`). Однако существующий `whilly --prd-wizard` (через `cli_legacy`) **должен продолжать работать неизменно** до того как TASK-105 удалит legacy целиком.

## 6. Success Criteria

**SC-1.** `whilly init "хочу CLI tool для X" --headless` (с stub'ом Claude через `CLAUDE_BIN=tests/fixtures/fake_claude_prd.sh`) → создаёт `docs/PRD-hochu-cli-tool-dlya-x.md` и запись в БД с N≥1 задачей. Pinned by integration test.

**SC-2.** `whilly init "..." --no-import` → создаёт PRD-файл, БД остаётся пустой. Pinned by integration test.

**SC-3.** Tab-completion / `whilly init --help` показывает все флаги: `--slug`, `--interactive`, `--headless`, `--no-import`, `--force`. Pinned by snapshot test on `--help` output.

**SC-4.** Существующий `whilly --prd-wizard` (legacy path) продолжает работать на ту же кодовую базу `prd_wizard.py` без regression'а. Pinned by re-running existing v3 e2e tests.

**SC-5.** Coverage report `--include='whilly/cli/init.py' --fail-under=80` зелёный.

**SC-6.** Integration test `tests/integration/test_init_e2e.py` проходит против testcontainers Postgres.

## 7. Constraints

**C-1.** Должно собраться на Python 3.12+ (требование v4 pyproject).

**C-2.** Должно работать на Linux + macOS. Windows — out-of-scope (v4 в целом серверный, этот flow не требуется на Windows).

**C-3.** Никаких изменений в `whilly/core/`. Pure domain layer не должен импортировать никакие новые модули. (Static gate: `lint-imports` поймает.)

**C-4.** Никаких новых зависимостей в `pyproject.toml`. Wizard переиспользует существующие subprocess/path/asyncpg.

## 8. Open Questions

**OQ-1.** Stub Claude для тестов — пишем ли отдельный `fake_claude_prd.sh` или расширяем существующий `tests/fixtures/fake_claude.sh`? **Резолв:** отдельный файл `tests/fixtures/fake_claude_prd.sh` — логика отличается (он эмулирует PRD-разговор + Tasks JSON в финале, а не одну задачу с `<promise>COMPLETE</promise>`).

**OQ-2.** Что делать если PRD создан, но `generate_tasks_dict` падает на парсинге? **Резолв:** PRD остаётся на диске, exit 1. Юзер может запустить `whilly init "..." --slug <тот-же> --force --no-import` чтобы переписать PRD руками или регенерировать через Claude.

**OQ-3.** Slug-collision policy. **Резолв:** см. FR-7. Без `--force` — отказ. С `--force` — пересоздать PRD-файл; для импорта плана требуется `whilly plan reset <slug>` (TASK-103) — до того как реализовано, пока fail с понятным сообщением.

## 9. Dependencies

* **Hard dependencies:** none. Эта задача self-contained, ничего не блокирует.
* **Soft dependencies:** TASK-103 (`whilly plan reset`) — без неё `--force` flow деградирует до "delete the plan via psql first". Не критично для MVP.
* **Unblocks:** TASK-104b (TRIZ), TASK-104c (Decision Gate), TASK-105 (cleanup `cli_legacy.py`).

## 10. Out of scope

* TRIZ-проверка идеи перед запуском wizard'а — TASK-104b.
* Decision Gate проверка спецификации после генерации — TASK-104c.
* Multi-LLM backend в wizard'е — backlog v4.2.
* Web UI / TUI dashboard для wizard'а — backlog v4.2.
* `whilly init --from-issue github-org/repo#123` (вытащить идею из GitHub Issue) — это TASK-108a (Forge intake).

## 11. Definition of Done

1. Все Success Criteria (SC-1..SC-6) зелёные.
2. Новый код в `whilly/cli/init.py` и обновлённые `prd_generator.py` / `plan_io.py` проходят `mypy --strict whilly/core/` (обновления в core нет, но guard остаётся), `ruff check + format`, `lint-imports`.
3. `pytest -q tests/unit/ tests/integration/` зелёный (релевантные новые тесты добавлены).
4. `docs/Whilly-Init-Guide.md` написан (или раздел в `Getting-Started.md` обновлён).
5. CHANGELOG.md имеет запись `[4.1.0] - YYYY-MM-DD ### Added — whilly init subcommand (PRD wizard port from v3)`.
