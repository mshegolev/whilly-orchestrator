# Whilly Orchestrator (RU)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Workshop kit](https://img.shields.io/badge/workshop-HackSprint1-blue.svg)](docs/workshop/INDEX.md)

Python-реализация **техники Ralph Wiggum** — непрерывного цикла, в котором AI-агент (Claude CLI) забирает задачи с board'а и выполняет их одну за другой, пока не закончатся или не сработает budget/timeout. Ralph TUI-дашборд, параллельные агенты в tmux + git worktree, decomposer, TRIZ analyzer и PRD wizard в комплекте.

> "I'm helping!" — Ralph Wiggum

📘 [Full English README](README.md) · 🎓 [Workshop kit (HackSprint1)](docs/workshop/INDEX.md)

## Что делает

Whilly крутит loop: взять pending-задачу → передать LLM-агенту → проверить → коммит → следующая. Работает, пока board не пуст, бюджет не исчерпан или вы не остановили. Параллельный режим запускает несколько агентов в tmux pane'ах или git worktree'ах.

Техника впервые описана в [посте Ghuntley о Ralph Wiggum](https://ghuntley.com/ralph/), стала популярна в Claude Code сообществе. Whilly — batteries-included оркестратор с дашбордом и task lifecycle вокруг этого цикла.

## Возможности

- **Непрерывный agent loop** — pending-задачи из JSON, прогон через Claude CLI, retry на transient errors.
- **Rich TUI dashboard** — живой прогресс, токены, cost, статусы; hotkeys для pause/reset/skip.
- **Параллельный запуск** — tmux pane'ы или git worktree'ы, до N одновременно с budget/deadlock guards.
- **Task decomposer** — LLM-разбивка слишком крупных задач на подзадачи.
- **PRD wizard** — интерактивная генерация PRD, потом auto-derive task'ов.
- **TRIZ analyzer** — выявление противоречий и инвентивных принципов для неоднозначных задач.
- **State store** — состояние задач переживает рестарт, per-task per-iteration логи.

## Установка

```bash
pip install whilly-orchestrator
```

Или из исходников:

```bash
git clone https://github.com/mshegolev/whilly-orchestrator
cd whilly-orchestrator
pip install -e .
```

Требуется [Claude CLI](https://docs.claude.com/en/docs/claude-code) в `PATH` (или `CLAUDE_BIN`).

## Quick start

1. Создай `tasks.json`:

```json
{
  "project": "health-endpoint",
  "tasks": [
    {
      "id": "TASK-001",
      "phase": "Phase 1",
      "category": "functional",
      "priority": "high",
      "description": "Добавь /health endpoint, возвращающий {\"status\":\"ok\"}",
      "status": "pending",
      "dependencies": [],
      "key_files": ["app/server.py"],
      "acceptance_criteria": ["GET /health возвращает 200 с {\"status\":\"ok\"}"],
      "test_steps": ["curl -s localhost:8000/health"]
    }
  ]
}
```

2. Запусти Whilly (2 параллельных агента, бюджет $5):

```bash
WHILLY_MAX_PARALLEL=2 WHILLY_BUDGET_USD=5 whilly tasks.json
```

3. Смотри dashboard. `q` — выйти, `d` — детали task, `l` — лог агента, `t` — список задач, `h` — помощь.

## Workshop kit (HackSprint1)

Whilly идёт с **workshop kit для HackSprint1** — hands-on на 90 минут от `pip install` до работающего self-hosting bootstrap demo:

- **Track A (`tasks.json`)** — без GitHub auth, ~30 минут.
- **Track B (GitHub Issues)** — полный e2e с созданием PR, ~60 минут.

Содержит BRD, PRD, 12 ADR'ов, sample plans, roadmap. См. [docs/workshop/INDEX.md](docs/workshop/INDEX.md) — полное руководство. RU/EN.

## Конфигурация

Большинство параметров — env vars с префиксом `WHILLY_`:

| Variable | Default | Назначение |
|---|---|---|
| `WHILLY_MODEL` | `claude-opus-4-6[1m]` | id Claude модели |
| `WHILLY_MAX_PARALLEL` | `3` | одновременных агентов (1 = последовательно) |
| `WHILLY_BUDGET_USD` | `0` | hard cap; 80% — warning, 100% — стоп |
| `WHILLY_TIMEOUT` | `0` | wall-clock cap в секундах |
| `WHILLY_USE_TMUX` | `1` | tmux для параллельных агентов |
| `WHILLY_WORKTREE` | `0` | git worktree per task (нужен `MAX_PARALLEL>1`) |
| `WHILLY_HEADLESS` | auto | CI mode — JSON на stdout |

## Troubleshooting

| Проблема | Решение |
|---|---|
| `gh auth status` возвращает 401 | `unset GITHUB_TOKEN`, потом `gh auth login` |
| `claude: command not found` | Установи Claude CLI или укажи путь через `CLAUDE_BIN` |
| Dashboard ломается на узком терминале | `WHILLY_HEADLESS=1 whilly tasks.json` |
| Бюджет упал в 0 раньше времени | `WHILLY_BUDGET_USD=N` (N>0) |
| `tmux ls` пуст после dispatch | tmux не установлен или `WHILLY_USE_TMUX=0` — whilly fallback'ит на subprocess |
| Агент в loop'е без `done` | проверь маркер `<promise>COMPLETE</promise>` в результате |

## Лицензия

MIT — см. [LICENSE](LICENSE).
