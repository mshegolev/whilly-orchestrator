---
title: Whilly Orchestrator — Product Requirements Document
type: prd
created: 2026-04-20
status: v1
audience: bilingual (RU primary, EN summary)
related:
  - BRD-Whilly.md
  - READINESS-REPORT.md
  - ROADMAP.md
  - adr/README.md
---

# Whilly Orchestrator — PRD

> **Назначение:** что и **как** строим. Архитектурные контракты, функциональные требования, scope текущего gap pack, acceptance criteria. BRD (зачем) — отдельный документ.
>
> **Purpose (EN):** what and how we build. Architectural contracts, functional requirements, current gap pack scope, acceptance criteria.

---

## TL;DR

**RU.** Whilly — Python пакет с CLI `whilly`, реализующий **continuous Ralph loop**. Источник задач (`tasks.json` или GitHub Issues) → batch planner → параллельные агенты в tmux/git worktree → JSONL events + Rich TUI → опциональный sink (PR / Slack). Текущий gap pack добавляет **GitHub Issues source**, **PR creation sink** и **Decision Gate**, чтобы поддержать workshop demo «whilly закрывает свои же issues».

**EN.** Whilly is a Python package + `whilly` CLI implementing a **continuous Ralph loop**. Source of tasks (`tasks.json` or GitHub Issues) → batch planner → parallel agents in tmux/git worktrees → JSONL events + Rich TUI → optional sink (PR / Slack). The current gap pack adds **GitHub Issues source**, **PR creation sink**, and **Decision Gate** to enable the workshop self-hosting demo.

---

## 1. System overview

### 1.1 High-level architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         WHILLY ORCHESTRATOR                          │
└─────────────────────────────────────────────────────────────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
   ┌─────────┐              ┌──────────┐             ┌──────────┐
   │ Source  │              │   Loop   │             │   Sink   │
   │ adapter │ ──tasks.json─▶│ (cli.py  │──result─▶│ adapter  │
   │         │              │ run_plan)│             │ (PR/log) │
   └─────────┘              └──────────┘             └──────────┘
        │                         │                         │
        │                         │                         │
   ┌────┴────┐         ┌──────────┴──────────┐         ┌────┴────┐
   │GH Issues│         │ TaskManager (state) │         │gh PR    │
   │tasks.json         │ Reporter (logs)     │         │comment  │
   │Linear*  │         │ Dashboard (TUI)     │         │webhook* │
   └─────────┘         │ StateStore (resume) │         └─────────┘
                       └──────────┬──────────┘
                                  │
                       ┌──────────┴──────────┐
                       │  Batch planner      │
                       │  (orchestrator.py)  │
                       └──────────┬──────────┘
                                  │
                       ┌──────────┴──────────┐
                       │  Decision Gate*     │   ← NEW (gap pack)
                       │  proceed / refuse   │
                       └──────────┬──────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        ┌──────────┐        ┌──────────┐        ┌──────────┐
        │ Tmux     │        │ Worktree │        │ Subproc  │
        │ runner   │   +    │ runner   │   or   │ runner   │
        └────┬─────┘        └────┬─────┘        └────┬─────┘
             │                   │                   │
             └───────────────────┼───────────────────┘
                                 ▼
                        ┌──────────────┐
                        │ Claude CLI   │
                        │ (agent)      │
                        └──────────────┘
                                 │
                                 ▼
                        ┌──────────────┐
                        │AgentResult   │
                        │usage,is_done │
                        └──────────────┘

  *  = planned / future
```

### 1.2 Module map

| Module | Responsibility | LOC | Status |
|---|---|---|---|
| `cli.py` | Entry point, `run_plan()` главный loop, prompt builders | 1592 | ✅ existing |
| `orchestrator.py` | Batch planning (file-based + LLM-based) | 174 | ✅ existing |
| `agent_runner.py` | Claude CLI subprocess, JSON output parsing, `AgentResult` | 225 | ✅ existing |
| `tmux_runner.py` | tmux session lifecycle для параллельных агентов | 137 | ✅ existing |
| `worktree_runner.py` | git worktree per task / per plan | 382 | ✅ existing |
| `task_manager.py` | `Task`, `Plan`, `TaskManager` (state, atomic JSON) | 174 | ✅ existing |
| `state_store.py` | `.whilly_state.json`, `--resume` | 128 | ✅ existing |
| `dashboard.py` | Rich Live TUI, hotkeys | 1232 | ✅ existing |
| `reporter.py` | Per-iteration JSON, summary MD | 207 | ✅ existing |
| `decomposer.py` | LLM split oversized tasks | 108 | ✅ existing |
| `prd_wizard.py`, `prd_generator.py`, `prd_launcher.py` | PRD pipeline | 895 | ✅ existing |
| `triz_analyzer.py` | TRIZ contradiction analysis | 390 | ✅ existing |
| `verifier.py` | Acceptance criteria verifier | 152 | ✅ existing |
| `notifications.py` | Budget/deadlock/auth alerts | 52 | ✅ existing |
| `history.py` | Cross-run analytics | 165 | ✅ existing |
| `web_status.py` | HTTP read-only status endpoint | 171 | ✅ existing |
| `config.py` | `WhillyConfig.from_env()` | 49 | ✅ existing |
| **`sources/github_issues.py`** | **GH Issues → tasks.json adapter** | ~150 | 🆕 gap pack |
| **`sinks/github_pr.py`** | **task done → `gh pr create`** | ~120 | 🆕 gap pack |
| **`decision_gate.py`** | **proceed / refuse short LLM gate** | ~100 | 🆕 gap pack |

### 1.3 Data contracts

#### 1.3.1 `Task` dataclass (existing)

```python
@dataclass
class Task:
    id: str                                  # "TASK-001" or "GH-42"
    phase: str                               # "Phase 1"
    category: str                            # "functional" / "test" / etc
    priority: str                            # critical | high | medium | low
    description: str
    status: str                              # pending | in_progress | done | failed | skipped
    dependencies: list[str] = []
    key_files: list[str] = []
    acceptance_criteria: list[str] = []
    test_steps: list[str] = []
    prd_requirement: str = ""
```

**GH Issues adapter** конвертирует issue → Task:
- `id = f"GH-{issue.number}"`
- `phase = "GH-Issues"`
- `category = "github-issue"`
- `priority` ← issue label `priority:high|medium|low|critical` (default = `medium`)
- `description` = первые ~500 символов issue body
- `key_files` ← парсятся из issue body (markdown bullet list под `**Files:**`)
- `acceptance_criteria` ← парсятся под `**Acceptance:**`
- `prd_requirement` = issue URL (для traceability)

#### 1.3.2 `AgentResult` (existing)

```python
@dataclass
class AgentResult:
    result_text: str = ""
    usage: AgentUsage = AgentUsage()
    exit_code: int = 0
    duration_s: float = 0.0
    is_complete: bool = False                # True если "<promise>COMPLETE</promise>" в тексте
```

#### 1.3.3 Plan JSON schema (existing)

```jsonc
{
  "project": "string",                       // optional
  "prd_file": "string",                      // optional
  "created_at": "ISO timestamp",             // optional
  "agent_instructions": {                    // optional, hints for agent
    "before": ["..."],
    "after": ["..."]
  },
  "source": {                                // 🆕 NEW (gap pack), optional
    "type": "github_issues",
    "repo": "owner/repo",
    "label": "whilly:ready",
    "since": "2026-04-20T00:00:00Z"
  },
  "tasks": [
    { /* Task */ }
  ]
}
```

#### 1.3.4 JSONL events (existing + new)

Existing events (in `whilly_logs/whilly_events.jsonl`):

```json
{"ts":"...","event":"plan.start","plan":"tasks.json","total":12}
{"ts":"...","event":"iteration.start","iter":1,"ready":3}
{"ts":"...","event":"task.start","task_id":"TASK-001","worker":"tmux:whilly-TASK-001"}
{"ts":"...","event":"task.done","task_id":"TASK-001","cost_usd":0.42,"duration_s":180}
{"ts":"...","event":"budget.warning","spent":4.0,"cap":5.0}
{"ts":"...","event":"plan.end","done":12,"failed":0,"skipped":0}
```

**New events (gap pack):**

```json
{"ts":"...","event":"source.fetch","source":"github_issues","repo":"mshegolev/whilly-orchestrator","new":5,"updated":1}
{"ts":"...","event":"decision_gate","task_id":"GH-42","decision":"proceed","reason":"clear spec"}
{"ts":"...","event":"decision_gate","task_id":"GH-43","decision":"refuse","reason":"missing acceptance criteria"}
{"ts":"...","event":"sink.pr.created","task_id":"GH-42","pr_url":"https://github.com/owner/repo/pull/128","branch":"whilly/GH-42"}
{"ts":"...","event":"sink.pr.failed","task_id":"GH-42","reason":"gh CLI not authenticated"}
```

---

## 2. Functional requirements

### 2.1 Core (existing, must not regress)

- **FR-1 Continuous loop.** `run_plan(plan_path)` берёт ready tasks, запускает агентов, ждёт результаты, обновляет state, повторяет — пока pending != 0 ИЛИ не сработал budget/deadlock/timeout guard.
- **FR-2 Atomic state.** `TaskManager.save()` пишет через temp + `os.replace` (POSIX atomic).
- **FR-3 Resume.** `--resume` восстанавливает state из `.whilly_state.json` после kill/crash.
- **FR-4 Headless mode.** При отсутствии TTY или `--headless` — JSON events на stdout, exit code 0/1/2/3.
- **FR-5 Dashboard hotkeys.** `q/p/d/l/t/h` — quit/pause/detail/log/tasks/help.
- **FR-6 Budget guard.** `WHILLY_BUDGET_USD` — warning at 80%, hard stop at 100%.
- **FR-7 Deadlock guard.** Task `in_progress` ≥ 3 итерации → mark `skipped` с reason `deadlock`.
- **FR-8 Auth error short-circuit.** На `is_auth_error` → fail-fast, не ретраим.
- **FR-9 Plan workspace isolation.** `WHILLY_USE_WORKSPACE=1` (default) — `.whilly_workspaces/{slug}/` git worktree, loop chdir'ит туда.
- **FR-10 Per-task worktree.** `WHILLY_WORKTREE=1` + `MAX_PARALLEL>1` — каждая задача в отдельном `.whilly_worktrees/{task_id}` worktree, cherry-pick back при done.

### 2.2 New — gap pack

#### 2.2.1 GitHub Issues source

- **FR-GH-1.** CLI flag `--source gh:owner/repo[:label]` (default label = `whilly:ready`).
- **FR-GH-2.** Adapter читает open issues через `gh issue list --repo owner/repo --label whilly:ready --json number,title,body,labels --limit 50`, конвертирует в `Task` (см. §1.3.1).
- **FR-GH-3.** Записывает `tasks.json` с `source` блоком (см. §1.3.3) — после этого loop работает обычным образом.
- **FR-GH-4.** Idempotent: повторный запуск на тех же issues — обновляет `description`, `priority`, не теряет `status` уже взятых задач (matched by `id == "GH-{number}"`).
- **FR-GH-5.** Issue closed во время выполнения → задача marks `skipped` с reason `issue closed externally`.
- **FR-GH-6.** Сеть недоступна → fail с понятным сообщением, exit code 1, не оставляем half-written `tasks.json`.

#### 2.2.2 PR creation sink

- **FR-PR-1.** CLI flag `--pr-on-done` (или config `WHILLY_PR_ON_DONE=1`).
- **FR-PR-2.** При task `done`:
  1. Внутри worktree: `git push origin HEAD:whilly/{task_id}` (force-with-lease).
  2. `gh pr create --title "{task_id}: {short description}" --body "{template}" --base main`.
  3. Если task связан с GH issue (есть `prd_requirement` URL) — `Closes #N` в body.
- **FR-PR-3.** Шаблон PR body:
  ```markdown
  Implements [issue link or task id].

  ### Plan
  - {acceptance_criteria}

  ### Validation
  - {test_steps}

  ### Whilly run
  - cost: ${cost}
  - duration: {duration_s}s
  - log: {log_file relative path}
  - JSONL events: see whilly_events.jsonl

  ---
  🤖 Opened by [whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator).
  Human review required before merge.
  ```
- **FR-PR-4.** Если PR creation fails — task остаётся `done`, но event `sink.pr.failed` пишется. Loop не падает.
- **FR-PR-5.** `--draft` опция → `gh pr create --draft`.

#### 2.2.3 Decision Gate

- **FR-DG-1.** CLI flag `--decision-gate` (или config `WHILLY_DECISION_GATE=1`).
- **FR-DG-2.** Перед основным prompt'ом — короткий LLM-вызов с моделью из config'а:
  - Prompt: «Дано описание задачи: {description}. Acceptance criteria: {acceptance}. Ты возьмёшься или откажешься? Ответ JSON: `{"decision":"proceed|refuse","reason":"..."}`».
  - Timeout: 60 секунд.
- **FR-DG-3.** На `proceed` → продолжаем как обычно.
- **FR-DG-4.** На `refuse` → `task.status = "skipped"`, `event: decision_gate refuse`, если source=GH → `gh issue edit {N} --add-label needs-clarification --remove-label whilly:ready`.
- **FR-DG-5.** На `parse error` или `timeout` → fail-open: считаем proceed, log warning.
- **FR-DG-6.** Decision Gate стоимость учитывается в budget (не выкидываем из cost tracking).

### 2.3 Workshop UX

- **FR-WS-1.** `examples/workshop/tasks.json` — sample plan на 5-10 простых задач (README badge, healthcheck, lint fix, etc).
- **FR-WS-2.** `docs/workshop/TUTORIAL.md` — пошаговое руководство, ≤ 90 минут от install до self-hosting.
- **FR-WS-3.** `docs/workshop/INDEX.md` — bilingual навигация.
- **FR-WS-4.** README.md — секция «Workshop kit» с CTA.
- **FR-WS-5.** GitHub repo `mshegolev/whilly-orchestrator` — 5-10 заведённых issues с label `whilly:ready` для self-hosting demo.

---

## 3. Non-functional requirements

| # | Категория | Требование |
|---|---|---|
| **NFR-1** | Performance | First-run latency < 60 секунд (до первого `task.start`). |
| **NFR-2** | Cost predictability | Decision Gate стоимость < $0.01 per task. PR sink overhead < $0.001. |
| **NFR-3** | Reliability | gap-pack модули не должны валить main loop при ошибке — graceful degrade. |
| **NFR-4** | Observability | Все новые ветки логирования через JSONL events + dashboard refresh. |
| **NFR-5** | Test coverage | Новые модули ≥ 75% покрытие (unit + integration). |
| **NFR-6** | Backwards compat | `tasks.json` flow без изменений. GH Issues = opt-in. |
| **NFR-7** | Security | `gh` token читается из `gh auth token`, не передаётся в LLM prompt. PR никогда не auto-merge. |
| **NFR-8** | Portability | Работает на macOS / Linux. Windows не таргет (worktree+tmux зависят от unix). |
| **NFR-9** | Python | 3.10+, без новых runtime deps кроме `gh` CLI и `rich` (уже в проекте). |
| **NFR-10** | Lint/format | `ruff check` + `ruff format` chistый. CI обязателен. |

---

## 4. Acceptance criteria

### 4.1 Gap pack acceptance

| # | Criteria | How to verify |
|---|---|---|
| **AC-1** | `whilly --source gh:mshegolev/whilly-orchestrator` создаёт `tasks.json` из открытых issues с label `whilly:ready` | manual: запуск, проверка содержимого tasks.json |
| **AC-2** | Loop работает на этом tasks.json без падений (одна задача — `done`) | manual self-hosting demo |
| **AC-3** | `--pr-on-done` после task done открывает PR в репо, body содержит link на issue | проверить URL из JSONL `sink.pr.created` |
| **AC-4** | `--decision-gate` отфильтровывает 1 заведомо плохо описанный issue (skip + label flip) | ручной тест на fixture |
| **AC-5** | Все 3 новых модуля имеют unit-тесты, `pytest -q` зелёный | CI |
| **AC-6** | `ruff check whilly/ tests/` без ошибок | CI |
| **AC-7** | `docs/workshop/TUTORIAL.md` проходим за ≤ 90 минут (3 dry-run на чистой VM) | manual |
| **AC-8** | `examples/workshop/tasks.json` валиден против `validate_schema()` | `whilly --check examples/workshop/tasks.json` |
| **AC-9** | INDEX.md, BRD, PRD, READINESS, ROADMAP, ADR pack — присутствуют, кросс-линки работают | manual review |
| **AC-10** | Sync-скрипт копирует `docs/workshop/` в Obsidian без потерь | manual run |

### 4.2 Demo acceptance

Self-hosting bootstrap demo считается успешным, если:

1. `whilly --source gh:mshegolev/whilly-orchestrator --pr-on-done --decision-gate` стартует.
2. Dashboard показывает 5+ ready tasks.
3. Decision Gate refuses ≥ 1 задачу (отметить label).
4. Хотя бы 1 task → `done` → PR opened (видим event `sink.pr.created`).
5. Total cost < $5 на demo (комфортный budget).
6. Все события в `whilly_events.jsonl` валидный JSONL (jq parse-able).

---

## 5. Out of scope (для текущего релизного цикла)

- Linear / Jira / GitLab Issues адаптеры (планируется как отдельные ADR + sources/).
- Codex / Gemini / OpenAI backends (отдельный ADR-013, после Claude stabilization).
- MCP server интерфейс (research first).
- Web UI замена TUI (сохраняем `web_status.py` как read-only).
- Auto-merge PR (security blocker).
- Multi-tenant / SaaS (out of mission).
- Distributed queue (in-process supervisor покрывает).

---

## 6. Edge cases & error handling

| Scenario | Expected behavior |
|---|---|
| `gh` CLI отсутствует | `--source gh:` fails fast с понятным сообщением + ссылкой на install guide. |
| `gh` token истёк / unauth | Fails fast, exit 1, suggest `gh auth login`. |
| GitHub rate limit | Single retry с exp.backoff (5s, 30s), потом fail. |
| Issue body отсутствует / пустое | Adapter создаёт Task с минимальным `description = title`, `acceptance_criteria = []`. |
| Issue body содержит секреты | Adapter **не** фильтрует — это ответственность пользователя (issue tracker — открытое место). Best-effort warning при detection через regex (например, `AKIA[A-Z0-9]+`). |
| `gh pr create` падает (push reject, no permission) | Task остаётся `done`, event `sink.pr.failed` с reason. Loop не падает. |
| Decision Gate timeout | Fail-open (proceed) + warning в JSONL. |
| Decision Gate parse error | Fail-open + warning. |
| Whilly запущен на самом себе и task — модификация whilly source | Reuse existing per-task worktree isolation. PR в main (review human). |
| Network disconnect mid-run | Task in_progress → next iteration retry; budget tracking сохранён в state. |

---

## 7. Open questions (deferred, not blocking)

1. Нужен ли webhook-based source (GH webhooks → trigger `whilly`) вместо polling? — после workshop feedback.
2. Стоит ли добавить `--source linear:team_id` сразу после GH? — приоритет 2, после workshop.
3. Decision Gate prompt — на каком языке? RU vs EN — какой даёт лучший recall? — A/B на 50 issues после workshop.
4. PR template — параметризовать через файл `.whilly/pr_template.md`? — добавить в follow-up.
5. Codex backend — какие compatibility break'и? — ADR-013 в следующем цикле.

---

## 8. Cross-references

- BRD: [BRD-Whilly.md](BRD-Whilly.md)
- Readiness: [READINESS-REPORT.md](READINESS-REPORT.md)
- Roadmap: [ROADMAP.md](ROADMAP.md)
- ADR index: [adr/README.md](adr/README.md)
- Tutorial: [TUTORIAL.md](TUTORIAL.md)
- CLI reference: [../Whilly-Usage.md](../Whilly-Usage.md)
- Task schema reference: [../Whilly-Interfaces-and-Tasks.md](../Whilly-Interfaces-and-Tasks.md)
- Source: [../../whilly/](../../whilly/)
- Sample plan: [../../examples/workshop/tasks.json](../../examples/workshop/tasks.json)

---

**Status:** v1 · 2026-04-20 · pending review after gap pack delivery.
