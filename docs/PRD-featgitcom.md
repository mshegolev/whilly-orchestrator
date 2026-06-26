# PRD: Git-интеграция и автоматизация PR/Release в `prd-wizard`

## Problem Statement

Сейчас `whilly --prd-wizard` создаёт PRD-файл и плавно переходит к выполнению плана задач, но **полностью игнорирует git-состояние** проекта. Пользователю приходится вручную:

- инициализировать git, если репо ещё нет;
- решать, что делать с незакоммиченными изменениями перед началом работы;
- создавать ветку под фичу;
- коммитить результат каждой выполненной задачи;
- пушить и открывать MR/PR в GitHub или GitLab;
- бампить версию в `whilly/__init__.py` и `pyproject.toml`;
- создавать release, тег и публиковать пакет на PyPI.

Это рутина, которая занимает время и часто забывается, особенно после длительной автономной работы Whilly. Также отсутствует механизм **самовосстановления Whilly при крэше его собственного кода** — пользователь упирается в трейсбэк и должен лечить руками.

## Objectives

1. Превратить `prd-wizard` в полноценный «git-aware» режим, который сам проводит пользователя через все этапы git-workflow.
2. Сделать настройку **one-shot**: единожды ответил на серию вопросов — все следующие запуски в этом проекте работают молча по сохранённой политике.
3. Дать полный override-стек: env vars и CLI-флаги переопределяют конфиг без перезаписи; команда `--git-config` редактирует политику интерактивно.
4. Обеспечить безопасную и идемпотентную автоматизацию вплоть до публикации в PyPI с умной классификацией ошибок.
5. Реализовать «маску на себя»: при internal crash Whilly сам открывает issue, лечит себя через отдельный Claude-агент, после успешного фикса возвращается к выполнению исходного плана.

## Target Users

- **Соло-разработчики**, использующие Whilly для автоматизации мелких/средних фич — хотят «один раз настроить и забыть».
- **corporate DevOps команды**, работающие через `gitlab.example.com` (`glab`) — нужен MR-флоу + сохранение токена.
- **Open-source мейнтейнеры** Whilly-подобных пакетов на GitHub (`gh`) — нужен полный pipeline вплоть до GitHub Release + PyPI upload.

## Requirements

### Functional Requirements

#### FR-1. Триггер в начале `whilly --prd-wizard`

- Перед интерактивным сбором PRD Whilly запускает **git-preflight**.
- Все вопросы preflight задаются **только при первом запуске** в данной директории. Ответы сохраняются в `.whilly/git.yaml`. При последующих запусках preflight молча применяет сохранённую политику, если нет override через env/flag.

#### FR-2. Случай: git-репозиторий **не инициализирован**

- Whilly спрашивает: `Initialize git repo? (y/n)`.
- На `y`: выполнить `git init`, создать `.gitignore` (если отсутствует) с дефолтным шаблоном для Python (`.venv/`, `__pycache__/`, `*.pyc`, `whilly_logs/`, `.whilly_state.json`, `.whilly_workspaces/`, `.whilly_worktrees/`), сделать initial commit `chore: initial commit`, продолжить wizard.
- На `n`: продолжить wizard без git-интеграции (записать `git.enabled: false` в `.whilly/git.yaml`).

#### FR-3. Случай: git инициализирован + есть незакоммиченные изменения

- Whilly показывает `git status` и спрашивает: `Commit all changes before wizard? (y/n/skip)`.
- `y`: `git add -A && git commit -m "chore: pre-wizard snapshot"`, продолжить.
- `n`: прервать запуск с exit code != 0.
- `skip`: продолжить wizard без коммита (изменения остаются в working tree).

#### FR-4. Выбор провайдера (только при первом запуске)

- Whilly спрашивает: `Which git provider to use for MR/PR? (gh / glab / none)`.
- `gh`: использовать GitHub CLI (`gh pr create`, `gh pr merge`, `gh release create`).
- `glab`: использовать GitLab CLI или GitLab API (`gitlab.example.com` по умолчанию для corporate env, конфигурируемо).
- `none`: не открывать PR — Whilly останавливается на `git push` ветки.
- Сохранить в `.whilly/git.yaml` ключ `git.provider`.

#### FR-5. Создание ветки под фичу

- Whilly автоматически создаёт ветку `feature/prd-{slug}` от `main` (fallback `master`), где `{slug}` — slug PRD-файла, тот же что в `PRD-{slug}.md`.
- Если ветка уже существует — переключиться в неё с предупреждением.
- Wizard и весь дальнейший план задач работают в этой ветке.

#### FR-6. Per-task auto-commit во время плана

- После каждого `task done` Whilly выполняет:
  - `git add {task.key_files}` (только файлы из `key_files`, чтобы не подмешать чужой шум) или `git add -A`, если `key_files` пуст.
  - `git commit -m "feat({task.id}): {task.description первая строка}"`.
- Если в worktree-режиме (`WHILLY_WORKTREE=1` или `--worktree`) — коммит идёт в worktree-ветку, потом `cherry-pick` в основную фича-ветку (как уже делает `worktree_runner.WorktreeManager`).
- Если коммит-команда падает (например, нет изменений, hook fail) — записать в лог, **не блокировать** прогресс плана.

#### FR-7. Version bump (по явному флагу)

- По умолчанию **никакого автобампа**.
- Флаг `--bump=patch|minor|major` (или env `WHILLY_BUMP=patch`) — после последнего `task done`:
  - изменить версию в `whilly/__init__.py` и `pyproject.toml` синхронно;
  - сделать commit `chore: bump version to X.Y.Z`;
  - применяется **только** если `git.enabled=true`.

#### FR-8. PR/MR creation

- При первом запуске спросить (если `provider != none`): `Auto-create MR/PR after plan? (y/n)`. Сохранить в `git.auto_pr`.
- Если `auto_pr=true` и план завершён успешно (все задачи `done` или `skipped`):
  - `git push -u origin {branch}`;
  - сформировать тело PR из PRD-файла (`PRD-{slug}.md`) + краткой сводки выполненных задач;
  - `gh pr create --base main --head {branch} --title "..." --body-file ...` или `glab mr create ...`.
- Сохранить URL созданного PR в state и вывести в финальный отчёт.

#### FR-9. Auto-release pipeline

- При первом запуске спросить (если `auto_pr=true`): `Auto-release after PR merge? (y/n)`. Сохранить в `git.auto_release`.
- Если `auto_release=true` и `--bump` указан, после создания PR Whilly:
  1. `gh pr merge --auto --squash` (или `glab mr merge --when-pipeline-succeeds`).
  2. Polling CI до зелёного статуса с timeout (по умолчанию 1800 сек, конфигурируемо).
  3. После merge: `git checkout main && git pull && git tag vX.Y.Z && git push --tags`.
  4. `gh release create vX.Y.Z --notes-from-tag` (или `glab release create`).
  5. `python -m build && twine upload dist/*` (PyPI publish).
- Все шаги — **полностью автоматические без подтверждений**, если `auto_release=true`.

#### FR-10. Обработка ошибок (retry + idempotent + classification)

- **Идемпотентность**: каждый шаг (push, PR create, tag, release, PyPI upload) проверяет «уже сделано?» — если да, skip без ошибки. Это позволяет безопасно перезапускать Whilly с `--resume`.
- **Retry с backoff**: 3 попытки с экспоненциальной задержкой (5/15/45s) на network/transient errors.
- **Классификация ошибок** (определяется по сообщению/exit code):
  - `auth_error` (gh: `HTTP 401`, glab: `unauthorized`, twine: `403 Forbidden`) → **fail-fast**, лог: `❌ Credentials missing/invalid for {tool}. Run '{tool} auth login' and retry. Whilly cannot continue.` Записать в `whilly_logs/whilly_events.jsonl`, exit code 4 (новый — `auth required`).
  - `timeout_error` → удвоить timeout, retry до 3 раз, потом fail-fast.
  - `network_error` (DNS, connection refused) → retry с backoff.
  - `merge_conflict` → fail-fast с подсказкой `Resolve conflicts manually and re-run with --resume`.
  - `ci_failure` → fail-fast, оставить PR открытым, exit с подсказкой.
  - `unknown_error` → лог + fail-fast.

#### FR-11. Self-heal Whilly («маска на себя»)

- При **internal crash** в самом Whilly (Python traceback не из task-агента, а из `whilly/*.py`):
  1. Поймать exception на верхнем уровне `cli.run_plan`.
  2. Открыть issue в GitHub-репо Whilly (`gh issue create --repo {whilly_repo} --title "..." --body "{traceback + context}"`).
  3. Запустить отдельный Claude-агент на репозитории Whilly (определить путь через `Path(whilly.__file__).parent.parent`) с промптом: `Fix this Whilly bug. Context: {traceback}. Issue: {issue_url}. After fix, run 'pytest -q' and 'ruff check'. Open MR with title 'fix: ...' and exit when MR is created.`.
  4. Дождаться появления fix-MR (polling).
  5. Дождаться green CI на fix-MR.
  6. Auto-merge fix-MR.
  7. Pull новую версию Whilly (если работает в pip-installed mode — `pip install -U` из git, иначе `git pull` в репо).
  8. Возобновить выполнение **исходного плана** через `--resume` (state уже сохранён в `.whilly_state.json`).
- Принцип «сначала маску на себя»: пока Whilly не починен, исходный план не продолжается.
- **Защита от бесконечной рекурсии**: счётчик self-heal попыток в state, max 2 за один запуск, потом fail-fast с сообщением `Self-heal failed twice. Manual intervention required.`.

#### FR-12. Override-стек (env > flag > file)

- **Файл** `.whilly/git.yaml` — источник дефолтов:
  ```yaml
  git:
    enabled: true
    provider: gh          # gh | glab | none
    base_branch: main
    branch_template: "feature/prd-{slug}"
    auto_pr: true
    auto_release: false
    auto_commit_per_task: true
    self_heal: true
  ```
- **Env vars** (читаются `WhillyConfig.from_env()`):
  - `WHILLY_GIT_ENABLED=0|1`
  - `WHILLY_GIT_PROVIDER=gh|glab|none`
  - `WHILLY_AUTO_PR=0|1`
  - `WHILLY_AUTO_RELEASE=0|1`
  - `WHILLY_AUTO_COMMIT=0|1`
  - `WHILLY_BUMP=patch|minor|major`
  - `WHILLY_SELF_HEAL=0|1`
  - `WHILLY_BASE_BRANCH=main`
- **CLI флаги**:
  - `--no-pr` / `--pr`
  - `--no-release` / `--release`
  - `--no-auto-commit` / `--auto-commit`
  - `--no-self-heal` / `--self-heal`
  - `--bump=patch|minor|major`
  - `--git-provider=gh|glab|none`
  - `--base-branch=main`
- **Команда `whilly --git-config`** — интерактивно перепрожить вопросы preflight и перезаписать `.whilly/git.yaml`.
- **Команда `whilly --git-reset`** — удалить `.whilly/git.yaml`, в следующий запуск preflight снова спросит.
- **Приоритет**: CLI flag > env var > file > built-in default.

### Non-Functional Requirements

- **Совместимость**: Python 3.10+, как и весь Whilly. Зависит от наличия `git`, опционально `gh` или `glab` на `PATH`.
- **Безопасность**: PyPI publish использует `twine` с токеном из env (`TWINE_PASSWORD`) или `~/.pypirc` — **никогда не запрашивать токен интерактивно и не сохранять в `.whilly/git.yaml`**.
- **Производительность**: per-task commit не должен блокировать loop > 2 сек на средний commit (`git add` + `git commit`).
- **Совместимость с worktree**: интегрироваться с существующим `worktree_runner.WorktreeManager` — коммиты идут в worktree-ветку, cherry-pick в основную сохраняется.
- **Совместимость с `--headless`**: в headless-режиме все интерактивные вопросы preflight **fail-fast** с сообщением `git config not set, run 'whilly --git-config' interactively first or pass WHILLY_GIT_* env vars`.
- **Логирование**: все git-операции пишут структурные события в `whilly_logs/whilly_events.jsonl` (`event: git.commit | git.push | git.pr_create | git.release | git.error`).
- **Atomic config writes**: `.whilly/git.yaml` пишется через `tempfile + os.replace` (как `TaskManager`), чтобы не было битого YAML при крэше.

### Technical Constraints

- **`gh` CLI** должен быть установлен и аутентифицирован для GitHub-провайдера. При отсутствии — fall back на режим «push only, no PR» с предупреждением.
- **`glab` CLI** или `python-gitlab` для GitLab. corporate env: использовать паттерн из `~/.claude/CLAUDE.md` (`glab config get token -h gitlab.example.com`).
- **Stdin/TTY detection**: preflight только в TTY-режиме. В headless — error + exit, как описано выше.
- **Не модифицировать `whilly_logs/` ИЛИ `.whilly_state.json`** в commit'ах — добавить в `.gitignore` если их там нет.
- **`pyproject.toml` version bump** должен использовать `tomllib` (Python 3.11+) или `tomli` (3.10), чтобы парсить корректно — не regex.
- **Self-heal Claude-агент** должен запускаться через тот же `agent_runner` что и обычные задачи, чтобы не дублировать subprocess-логику.

## Success Criteria

1. **Метрика 1 (UX)**: время от `whilly --prd-wizard` до открытого MR/PR при первом запуске на чистом репо ≤ 2 минуты + время выполнения плана. При втором запуске — preflight ≤ 5 секунд (без вопросов).
2. **Метрика 2 (надёжность)**: на тестовом репо последовательность `prd-wizard → план из 5 задач → auto-pr → auto-release → PyPI` отрабатывает успешно в **≥ 95% запусков** без ручного вмешательства (тестируется в CI с `gh` против test-org).
3. **Метрика 3 (self-heal)**: искусственно введённый bug в `whilly/cli.py` приводит к: открытому issue + созданному fix-MR + возобновлению исходного плана. Покрыто отдельным e2e-тестом.
4. **Метрика 4 (override)**: `WHILLY_AUTO_PR=0 whilly --prd-wizard` отключает PR creation **без модификации** `.whilly/git.yaml`. Проверяется unit-тестом.
5. **Метрика 5 (idempotency)**: повторный запуск `whilly --resume` после успешного auto-release **не создаёт дубликат** tag/release/PyPI upload. Проверяется e2e-тестом.

## Out of Scope

- **Поддержка других git-хостингов** (Bitbucket, Gitea, Codeberg) — только GitHub и GitLab в первой итерации.
- **Auto-merge без `--bump`**: если `--bump` не указан, auto-release вообще не запускается, даже если `auto_release=true` в конфиге.
- **Conventional commits parsing для авто-определения bump уровня** — отвергнуто на этапе сбора требований.
- **PyPI без token / interactive auth** — токен только из env / `.pypirc`.
- **Rollback при failure** — отвергнуто, выбран `idempotent + retry + classification`.
- **Изменение существующих non-git компонентов** Whilly (TUI, batch planning, decomposer) — всё остаётся как есть, новая фича — слой поверх.
- **Multi-PR на один плана** — план = одна ветка = один PR. Если нужно разбить — пользователь делит план вручную.

## Risks and Assumptions

### Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Self-heal зацикливается на нечинимом баге | High | Счётчик max 2 self-heal за запуск, после — fail-fast |
| Self-heal Claude-агент создаёт **broken fix** который мержится автоматически | High | Auto-merge в self-heal только после **green CI**; если CI красный → fail-fast |
| `gh pr merge --auto` плохо работает с repository rules / required reviewers | Medium | Документировать ограничение; fallback на ручной merge с подсказкой |
| `twine upload` опубликует пакет с битой версией | High | Проверка: версия из `pyproject.toml` не существует в PyPI до publish |
| Per-task commit ломает CI (промежуточный код не собирается) | Medium | По умолчанию `auto_commit_per_task=true` для feature branch; main защищён branch protection |
| Конфликт с existing branch `feature/prd-{slug}` (старый незаконченный план) | Medium | При коллизии — спросить «reuse / new with suffix `-N`», в headless — fail |
| Headless-режим теряет gracefulness preflight | Low | В headless требовать `WHILLY_GIT_*` env заранее, иначе exit 5 (новый код) |

### Assumptions

- Пользователь имеет `git`, `gh`/`glab` установленные и аутентифицированные **до** запуска Whilly. Whilly не делает `gh auth login` за пользователя.
- Default branch проекта называется `main` или `master` — иные имена через `WHILLY_BASE_BRANCH=develop`.
- PyPI token хранится в env или `~/.pypirc` пользователем заранее.
- Whilly-репозиторий для self-heal доступен на GitHub под известным URL (вычисляется из `setup.py`/`pyproject.toml` или хардкодится в константу).
- `.whilly/` директория добавлена в `.gitignore` глобально — иначе локальный конфиг попадёт в коммит.
- Claude CLI (`claude`) присутствует на PATH (или `CLAUDE_BIN` указывает на него) для self-heal агента.

## Timeline

Декомпозиция на milestones (детальная нарезка задач — следующим шагом через `prd-to-tasks`):

- **M1 — Git preflight + config**  (~1 день)
  - `.whilly/git.yaml` schema + atomic read/write.
  - Preflight функция: detect git, ask init, ask dirty, save config.
  - Override-стек (env + flag).
  - `whilly --git-config` / `--git-reset` команды.

- **M2 — Branch + per-task commit**  (~1 день)
  - Auto-create `feature/prd-{slug}` branch.
  - Hook в `task done` → commit.
  - Интеграция с `worktree_runner`.

- **M3 — PR creation (gh + glab)**  (~1.5 дня)
  - Adapter pattern для двух провайдеров.
  - PR body builder из PRD + summary.
  - Idempotent push + create.

- **M4 — Version bump + auto-release**  (~2 дня)
  - `--bump` flag, синхронный bump в обоих файлах через `tomllib`.
  - Auto-merge polling.
  - Tag + release + PyPI upload pipeline.
  - Idempotency checks на каждом шаге.

- **M5 — Error classification + retry**  (~1 день)
  - Error classifier (auth/timeout/network/merge_conflict/ci/unknown).
  - Retry с backoff.
  - Новые exit codes (4, 5).

- **M6 — Self-heal**  (~2 дня)
  - Top-level exception catcher.
  - Issue creation.
  - Self-heal Claude-агент launcher.
  - Polling fix-MR + CI + auto-merge.
  - Resume исходного плана.
  - Anti-recursion counter.

- **M7 — Tests + docs**  (~1 день)
  - Unit-тесты на classifier, config, override-стек.
  - E2E-тесты на test-репо (mock gh/glab).
  - Обновить `README.md`, `docs/Whilly-Usage.md`, `docs/Whilly-Interfaces-and-Tasks.md`.

**Total estimate**: ~9.5 рабочих дней для одного исполнителя. Возможна параллелизация M3 (PR) и M4 (release) после M2.
