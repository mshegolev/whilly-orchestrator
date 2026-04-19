---
title: Whilly Workshop Tutorial — 90-minute hands-on
type: tutorial
created: 2026-04-20
status: v1
audience: bilingual (EN summary above each track, RU detail)
related: [INDEX.md, BRD-Whilly.md, PRD-Whilly.md]
---

# Whilly Workshop Tutorial

> **EN summary:** End-to-end 90-minute hands-on. Two parallel tracks:
> - **Track A (`tasks.json`)** — works without GitHub auth, ~30 min, every participant can complete.
> - **Track B (GitHub Issues)** — full self-hosting demo, ~60 min, requires `gh auth login` and write access to a sandbox repo.
> Targets the HackSprint1 minimum requirements: source → agent → PR → retry → real repo.
>
> **RU:** Hands-on на 90 минут в двух треках: Track A (`tasks.json`) — без GitHub auth, Track B (GitHub Issues) — полный self-hosting. Закрывает все 5 минимальных требований HackSprint1.

---

## ⏱ Pre-flight checklist (5 минут)

Прежде чем начать, проверь:

```bash
# Python 3.10+
python3 --version
# 3.10.x or higher

# git
git --version
# 2.40+ recommended

# Claude CLI (для Track A и Track B)
claude --version
# any recent version

# Anthropic API key
echo $ANTHROPIC_API_KEY
# должен быть непустым

# tmux (опционально, для параллельных агентов)
tmux -V

# gh CLI (только для Track B)
gh --version
gh auth status
# должен показывать "Logged in to github.com"
```

Если что-то отсутствует:

| Tool | macOS | Linux |
|---|---|---|
| Python 3.10+ | `brew install python@3.12` | apt/dnf |
| git | preinstalled | apt/dnf |
| Claude CLI | https://docs.claude.com/en/docs/claude-code | same |
| tmux | `brew install tmux` | `apt install tmux` |
| gh | `brew install gh` then `gh auth login` | https://cli.github.com/ |

---

## 🚀 Install whilly (5 минут)

```bash
# Option A — from PyPI
pip install whilly-orchestrator

# Option B — from source (recommended for workshop, you'll modify it)
git clone https://github.com/mshegolev/whilly-orchestrator
cd whilly-orchestrator
pip install -e ".[dev]"

whilly --help
```

Если `whilly --help` выводит usage — установка сработала.

---

# Track A — `tasks.json` (no GitHub auth required)

> **Цель:** за 30 минут увидеть Ralph loop в действии — агент берёт задачу, выполняет, помечает `done`, переходит к следующей.

## Step A.1 — Скопировать sample plan (2 мин)

```bash
cp examples/workshop/tasks.json my_first_run.json
cat my_first_run.json
```

Содержимое:

```json
{
  "project": "workshop-track-a",
  "tasks": [
    {
      "id": "TASK-001",
      "phase": "Phase 1",
      "category": "doc",
      "priority": "low",
      "description": "Create a file HELLO.md with one line: 'Hello from Whilly!'",
      "status": "pending",
      "dependencies": [],
      "key_files": ["HELLO.md"],
      "acceptance_criteria": ["HELLO.md exists with the exact line"],
      "test_steps": ["test -s HELLO.md"]
    }
  ]
}
```

## Step A.2 — Запустить (3 мин)

```bash
whilly my_first_run.json
```

Появится Rich TUI dashboard. Смотри:

- В левой колонке — список задач, статус `pending` → `in_progress`.
- В правой — live лог агента.
- Hotkeys: `q`=quit, `d`=detail, `l`=log, `t`=tasks, `h`=help.

Через ~30-60 секунд:
- Задача → `done`.
- В корне репо появился `HELLO.md`.

Если хочешь убедиться, что это был **агент**, а не магия:

```bash
cat HELLO.md
# Hello from Whilly!
git log --oneline | head -3
# увидишь коммит с описанием задачи
```

## Step A.3 — Понять, что произошло (5 мин)

```bash
cat whilly_logs/whilly_events.jsonl | jq .
```

Увидишь поток событий:
- `plan.start` — план загружен
- `iteration.start` — итерация 1
- `task.start` — TASK-001 ушла в работу
- `task.done` — закрыта, с `cost_usd` и `duration_s`
- `plan.end` — итог

Это **JSONL events** (ADR-005). Любой инструмент (jq, grep, Python) разбирает их.

## Step A.4 — Параллельный запуск (10 мин)

Добавь в `my_first_run.json` ещё 2-3 задачи (разные `key_files`!) и:

```bash
WHILLY_MAX_PARALLEL=3 WHILLY_USE_TMUX=1 whilly my_first_run.json
```

Откроется tmux session `whilly-TASK-001`, `-002`, `-003` параллельно.

```bash
# В другом терминале:
tmux ls
# увидишь все активные agent sessions

tmux attach -t whilly-TASK-001
# живой лог агента
```

После: tmux session убивается автоматически на done.

## Step A.5 — Что почитать дальше (5 мин)

- [PRD §1.2 Module map](PRD-Whilly.md) — какие модули за что отвечают.
- [adr/ADR-002](adr/ADR-002-tasks-json-source-of-truth.md) — почему `tasks.json` source of truth.
- [adr/ADR-003](adr/ADR-003-tmux-and-worktree-parallelism.md) — почему tmux+worktree.

✅ Если ты дошёл сюда — **MVP HackSprint1 минимальные требования #1, #2 у тебя уже работают**.

---

# Track B — GitHub Issues + PR creation (full self-hosting)

> **Цель:** за 60 минут собрать полный e2e flow «GH Issue → агент → PR → retry». Закрывает все 5 минимальных требований HackSprint1.

## Step B.1 — Подготовить sandbox-репо (5 мин)

Если у тебя нет sandbox-репо для HackSprint1 — создай:

```bash
gh repo create my-hacksprint1-sandbox --public --add-readme
git clone https://github.com/<your-user>/my-hacksprint1-sandbox
cd my-hacksprint1-sandbox
```

Заведи 3 простых issue с label `whilly:ready`:

```bash
gh issue create \
  --title "Add MIT LICENSE" \
  --label "whilly:ready" \
  --body "Add a standard MIT LICENSE file at repo root.

**Files:** LICENSE

## Acceptance
- LICENSE file exists
- Contains MIT text and current year

## Test
- test -s LICENSE
- grep -q 'MIT License' LICENSE
"

gh issue create \
  --title "Add Python .gitignore" \
  --label "whilly:ready" \
  --body "Add a Python-flavoured .gitignore.

**Files:** .gitignore

## Acceptance
- .gitignore exists with __pycache__, .venv, .env entries

## Test
- grep -q __pycache__ .gitignore
"

gh issue create \
  --title "Add CONTRIBUTING.md" \
  --label "whilly:ready" \
  --body "Create a minimal CONTRIBUTING.md.

**Files:** CONTRIBUTING.md

## Acceptance
- File exists with at least Issues / PRs / Style sections

## Test
- test -s CONTRIBUTING.md
"
```

## Step B.2 — Pull issues into a tasks.json (5 мин)

С новой gap-pack функцией:

```bash
# В корне whilly-orchestrator:
python3 -c "
from whilly.sources import fetch_github_issues
path, stats = fetch_github_issues('<your-user>/my-hacksprint1-sandbox')
print(f'Fetched: new={stats.new}, updated={stats.updated}, total_open={stats.total_open}')
print(f'Plan written to: {path}')
"
```

Получишь `tasks.json` в текущей директории. Открой:

```bash
cat tasks.json | jq '.tasks[] | {id, status, description: .description[:50]}'
```

Должно быть 3 задачи с id `GH-1`, `GH-2`, `GH-3`.

## Step B.3 — Запустить с PR creation (15 мин)

```bash
# Перейди в sandbox-репо
cd ~/my-hacksprint1-sandbox

# Скопируй сгенерированный tasks.json
cp /path/to/whilly-orchestrator/tasks.json .

# Запусти whilly (последовательно для простоты)
WHILLY_MAX_PARALLEL=1 whilly tasks.json
```

После каждой done-задачи в whilly будет создаваться branch вида `whilly/GH-1`.

Открой PR вручную (gap-pack PR sink — следующий шаг для интеграции в loop):

```bash
git push origin whilly/GH-1
gh pr create --base main --head whilly/GH-1 --title "GH-1: Add MIT LICENSE" --body "Closes #1"
```

> 💡 **Production-режим (полная автоматизация):** в текущей gap-pack ветке `--pr-on-done` ещё не интегрирован в `cli.py`. Это твоё ДЗ — посмотри `whilly/sinks/github_pr.py` и хук в `cli.py::run_plan`. Контракт уже есть.

## Step B.4 — Decision Gate (10 мин)

Добавь в sandbox-репо «плохой» issue:

```bash
gh issue create --title "x" --label "whilly:ready" --body ""
```

Запусти Decision Gate:

```bash
python3 -c "
from whilly.sources import fetch_github_issues
from whilly.decision_gate import evaluate
from whilly.task_manager import TaskManager

# Refresh tasks.json
fetch_github_issues('<your-user>/my-hacksprint1-sandbox')

tm = TaskManager('tasks.json')
for task in tm.tasks:
    if task.status != 'pending':
        continue
    d = evaluate(task)
    print(f'{task.id}: {d.decision} ({d.reason})')
"
```

«Плохой» issue с пустым описанием получит `refuse` (auto-refuse без LLM-вызова — описание короче 20 символов).

## Step B.5 — Наблюдай retry в действии (10 мин)

Заведи issue, который умышленно сложный (агент будет ошибаться):

```bash
gh issue create \
  --title "Add /metrics endpoint with Prometheus format" \
  --label "whilly:ready" \
  --body "Add Prometheus metrics endpoint.

**Files:** app/metrics.py

## Acceptance
- GET /metrics returns text/plain
- Includes process_uptime_seconds gauge
- Includes whilly_tasks_total counter
"
```

Запусти whilly, смотри на retry-петлю в логах:

```bash
WHILLY_MAX_PARALLEL=1 WHILLY_MAX_TASK_RETRIES=3 whilly tasks.json
```

В JSONL events увидишь повторные попытки на API errors / failed acceptance criteria.

## Step B.6 — Записать демо (5 мин — но запиши когда будет готов прогон)

Самое важное для HackSprint1: **записать демо-видео**.

```bash
# macOS
brew install --cask obs

# Linux
sudo apt install obs-studio

# Установи + запиши screencast 2-3 минут:
# 0:00 — терминал, видно tasks.json и Issues в браузере
# 0:30 — запуск whilly, появляется dashboard
# 1:00 — первая задача → done, PR в браузере
# 2:00 — Decision Gate refuses плохой issue, label flip виден
# 2:30 — short summary, fadeout
```

✅ Если ты дошёл сюда и записал видео — **все 5 минимальных требований HackSprint1 закрыты**.

---

## What you've learned in 90 minutes

- **Track A:** Ralph loop, tasks.json, JSONL events, parallel agents в tmux.
- **Track B:** GitHub Issues source, PR creation, Decision Gate, retry-loop.
- **Architecture:** ADR-001 to ADR-008 теперь будут читаться предметно.

## Next steps

1. Прочитай [PRD-Whilly.md](PRD-Whilly.md) — увидишь весь контракт целиком.
2. Прочитай [BRD-Whilly.md](BRD-Whilly.md) §11 Decision Log — пойми D6 (выбор типа задачи).
3. Сделай свой fork whilly, добавь второй source adapter (Linear?) — ADR-006 — это шаблон.
4. Запиши demo-видео — это **обязательно** для зачёта HackSprint1.

## Troubleshooting

| Проблема | Решение |
|---|---|
| `claude: command not found` | https://docs.claude.com/en/docs/claude-code |
| `gh auth status` показывает 401 (token invalid) | `unset GITHUB_TOKEN; gh auth login` |
| Dashboard ломается на узком терминале | `WHILLY_HEADLESS=1 whilly tasks.json` |
| Бюджет упал в 0 раньше времени | проверь `WHILLY_BUDGET_USD` env, default = unlimited |
| tmux пишет "no server running" при attach | session уже завершилась — посмотри лог в `whilly_logs/{task_id}.log` |
| Агент в loop'е выдаёт мусор | смотри `agent_runner.is_complete` — нужна строка `<promise>COMPLETE</promise>` |

## FAQ

**Q: Сколько стоит прогнать tutorial Track A?**
A: ~$0.10-0.30 на 1-3 простых задач.

**Q: Сколько стоит Track B?**
A: ~$0.50-2 в зависимости от сложности issue. Decision Gate auto-refuse экономит на мусоре.

**Q: Я соло-участник HackSprint1, успею за 10 дней?**
A: Да — Track A + минимальные требования. Optional блоки придётся сократить до 1.

**Q: Можно ли запустить без `gh` CLI?**
A: Только Track A. Track B требует `gh`.

**Q: Какой бюджет на весь HackSprint1?**
A: BRD §8 Constraints — **~$300 личных** (бюджет не компенсируется клубом). Разнеси по дням, hard-cap через `WHILLY_BUDGET_USD=300`.

---

**Status:** v1 · 2026-04-20 · maintained alongside the codebase.
