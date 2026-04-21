# Whilly Orchestrator (RU)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Workshop kit](https://img.shields.io/badge/workshop-HackSprint1-blue.svg)](docs/workshop/INDEX.md)

Python-реализация **Whilly Wiggum loop** — умного брата Ralph'а. Та же семья, тот же дух «I'm helping!», только сверху — TRIZ-анализатор противоречий, Decision Gate (отфильтровать мусорные задачи до старта), PRD wizard и Rich TUI-дашборд. Параллельные агенты в tmux/git-worktree, decomposer — в комплекте.

> "I'm helping — и я читал ТРИЗ." — Whilly Wiggum

📘 [Full English README](README.md) · 🎓 [Workshop kit (HackSprint1)](docs/workshop/INDEX.md)

## Что делает

Whilly крутит loop: взять pending-задачу → передать LLM-агенту → проверить → коммит → следующая. Работает, пока board не пуст, бюджет не исчерпан или вы не остановили. Параллельный режим запускает несколько агентов в tmux pane'ах или git worktree'ах.

Базовая техника впервые описана в [посте Ghuntley про Ralph Wiggum loop](https://ghuntley.com/ralph/) и стала популярна в Claude Code сообществе. Whilly — «умнастный брат» Ralph'а: та же упорная пахота «взял → попробовал → повторил», плюс TRIZ для противоречий, Decision Gate для фильтрации задач и PRD wizard для понимания проблемы *до* того, как бросаться её решать.

## vNext — Whilly Forge (Issue → PR)

> Whilly не просто *отвечает* на issue. Он *сдаёт Pull Request, который можно смёржить.*

**Forge** — направление, в котором Whilly идёт в vNext: конвейер, превращающий один GitHub Issue в ветку, диф и PR, готовый к ревью. Тот же agent loop в ядре, но со структурой *до* и *после* агента.

```
Issue ──► Intake ──► Normalize ──► Readiness ──► Strategy ──► Plan ──► Execute ──► Verify ──► Repair ──► Compose PR ──► Timeline
         (fetch)    (spec +       (Decision    (bugfix /    (per-     (agent    (tests +   (auto-fix   (what/why/    (board +
                     classify)    Gate)         feature /    task)     loop)     lint)      loop)       validation)   dashboard)
                                                refactor /
                                                unknown)
```

| Стадия | Что есть сегодня | Куда идёт vNext |
|---|---|---|
| **Intake** — забрать issue в план | `whilly --from-issue owner/repo/N` | `whilly/intake_github.py` (FR-1) |
| **Normalize** — явный spec + классификатор типа задачи | ad-hoc prompts | `whilly/spec.py` + classifier (FR-2) |
| **Readiness** — отфильтровать недо-issue до старта | `decision_gate.py` | состояния `whilly/readiness.py` (FR-3) |
| **Strategy** — подобрать playbook под тип задачи | один общий loop | 4 стратегии (FR-4) |
| **Plan** — план, заскоупленный под задачу (не по всему репо) | `decomposer.py` | `whilly/planner.py` (FR-5) |
| **Execute** — agent loop, parallel / tmux / worktree | ✅ стабильно | — (ядро) |
| **Verify** — структурированный вердикт, учитывает repo profile | `verifier.py` | structured verdict (FR-7) |
| **Repair** — auto-fix цикл при фейле verify | частично (self-healing) | `whilly/repair.py` (FR-8) |
| **Compose PR** — ветка `whilly/issue-{N}-{slug}`, тело what/why/validation | `github_pr.py` | полная композиция (FR-9 / FR-10) |
| **Timeline** — каждая стадия видна на board + dashboard | пока только колонки board | timeline events (FR-11) |

**Почему «Forge»?** Кузница превращает сырой материал в готовую деталь. Whilly превращает сырой issue в готовый patch — с той же упёртостью «I'm helping!», только теперь с квитанциями на каждой стадии.

**Что работает сегодня:** `scripts/whilly_e2e_demo.py` и `scripts/whilly_e2e_triz_prd.py` уже демонстрируют e2e-поток. vNext refactor (трекается в issues `FR-1`…`FR-11`) разбивает его на чётко очерченные модули, чтобы команды могли подменять strategy, verifier и PR composer под свой стек.

**Что за рамками:** Whilly не вмёрживает код в прод сам. Для всего нетривиального Forge создаёт Draft PR; финальный мёрдж — за человеком. Политика Draft-vs-auto-merge — в [ADR-017](https://github.com/mshegolev/whilly-orchestrator/issues/158).

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
| `WHILLY_AGENT_BACKEND` | `claude` | активный backend (`claude` или `opencode`) |
| `WHILLY_OPENCODE_BIN` | `opencode` | путь к OpenCode CLI |
| `WHILLY_OPENCODE_SAFE` | `0` | `1` → не передавать `--dangerously-skip-permissions` в OpenCode |
| `WHILLY_OPENCODE_SERVER_URL` | _(не задано)_ | URL удалённого OpenCode server'а |

CLI-флаги: `--all`, `--headless`, `--resume`, `--reset PLAN.json`, `--init "desc"`, `--plan PRD.md`, `--prd-wizard`, `--no-worktree`, `--agent {claude,opencode}`.

## Backends

Whilly поддерживает два agent backend'а за единым `AgentBackend` Protocol'ом (`whilly/agents/`):

| Backend | Выбор | Обёртка CLI | Заметки |
|---|---|---|---|
| **Claude** (по умолчанию) | `--agent claude` / `WHILLY_AGENT_BACKEND=claude` | `claude --output-format json -p "…"` | Нужен [Claude CLI](https://docs.claude.com/en/docs/claude-code). Путь через `CLAUDE_BIN`. |
| **OpenCode** | `--agent opencode` / `WHILLY_AGENT_BACKEND=opencode` | `opencode run --format json --model <provider/id> "…"` | Нужен [sst/opencode](https://github.com/sst/opencode) в `PATH` (или `WHILLY_OPENCODE_BIN`). `WHILLY_OPENCODE_SAFE=1` — включить per-tool permission policy. |

Модель автоматически нормализуется под backend (напр. `claude-opus-4-6` → `anthropic/claude-opus-4-6` для OpenCode). Сигнал завершения одинаковый (`<promise>COMPLETE</promise>`). Decision Gate, tmux-раннер и subprocess-fallback — все роутят через активный backend.

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
