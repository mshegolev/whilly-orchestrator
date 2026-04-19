# ADR-003 — tmux + git worktree for parallelism

- **Status:** accepted
- **Date:** 2026-04-19 (retroactive)
- **Deciders:** project author
- **Domain:** concurrency / isolation

## Context

Loop должен поддерживать параллельный запуск N агентов. Каждый агент должен:

1. Иметь **изолированную рабочую копию репо** (не ломать друг друга через перекрывающиеся правки).
2. Иметь **отдельный stdout/stderr** для дашборда (`l` hotkey показывает живой лог).
3. Управляться **долгоживущим супервизором** (Python loop), который не блокируется на одном агенте.
4. Переживать `kill -9` супервизора — running tasks доделают свою работу, можно потом attach.

## Decision

**Параллелизм = tmux session + git worktree per task**:

- Каждый агент запускается в собственном tmux session `whilly-{task_id}`.
- Каждой задаче (опционально, при `MAX_PARALLEL>1` и `WHILLY_WORKTREE=1`) создаётся git worktree `.whilly_worktrees/{task_id}/`.
- Супервизор пишет prompt в файл, открывает session с `claude … -p file:promptfile`, мониторит exit code через `EXIT_CODE=N` маркер в логе.
- На done — supervisor cherry-picks коммиты из worktree обратно в основной branch, удаляет worktree.

## Considered alternatives

### `subprocess.Popen` + threading

- ✅ Pure Python, нет внешних зависимостей.
- ✅ Работает в headless (CI) окружении.
- ❌ Логи сложно показывать "живьём" (буферизация).
- ❌ Kill супервизора убивает детей.
- ❌ Нельзя "attach" к запущенному агенту мышкой.

### Docker containers

- ✅ Сильнейшая изоляция.
- ❌ Workshop friction (Docker setup, image pull).
- ❌ Намного медленнее старт.
- ❌ Не работает на корпоративных машинах без Docker.

### `asyncio` coroutines

- ✅ Single-process, понятный control flow.
- ❌ Не помогает с изоляцией FS — overlap key_files = race.
- ❌ Не показывает "живые" логи без отдельной работы.

### tmux + worktree (выбрано)

- ✅ Каждый агент = standalone process в своей copy-on-write копии репо.
- ✅ `tmux attach` — живой просмотр.
- ✅ Detach-able: kill whilly не убивает агента.
- ✅ Логи в `whilly_logs/{task_id}.log` — можно tail -f.
- ✅ Worktree — встроен в git, ноль внешних tools.
- ❌ Tmux требуется на машине пользователя.
- ❌ Не работает на Windows (но это и не цель).

## Decision details

- **Subprocess fallback:** если `tmux_available()` == False ИЛИ `WHILLY_USE_TMUX=0` — переходим на чистый subprocess (`subprocess.Popen` + `cwd=worktree_path`).
- **Plan-level workspace:** `WHILLY_USE_WORKSPACE=1` (default) — главный supervisor chdir'ит в `.whilly_workspaces/{slug}/` (тоже git worktree). Это защищает основное репо от случайных правок.
- **Per-task worktree:** активируется только когда `MAX_PARALLEL>1` AND `WHILLY_WORKTREE=1` — оверхед `git worktree add` стоит дорого только если есть конкуренция.
- **Cherry-pick back:** на task done supervisor делает `git -C .whilly_workspaces/{slug} cherry-pick {worktree_commits}` и удаляет worktree через `git worktree remove`.
- **Stale worktree cleanup:** при старте loop сканит `.whilly_worktrees/`, удаляет завершённые.

## Consequences

### Positive

- Параллельные агенты не мешают друг другу.
- Workshop-демо «3 агента в 3 tmux pane'ах» очень нагляден.
- Crash recovery естественный: tmux session живёт, supervisor restart attach'ится.
- `tmux attach -t whilly-{task_id}` — debugging tool из коробки.

### Negative

- Зависимость от tmux на хосте.
- Worktree creation добавляет ~200ms на task — ничтожно при N>1, но overhead для одной маленькой задачи.
- Cherry-pick conflicts могут возникать на overlapping changes — текущий план учитывает только `key_files` overlap, но edge cases возможны.

### Neutral

- Subprocess-only режим существует и работает — просто без `tmux attach`.

## References

- `whilly/tmux_runner.py`, `whilly/worktree_runner.py`.
- ADR-002 — state model независим от runner.
- PRD §2.1 FR-9, FR-10.
