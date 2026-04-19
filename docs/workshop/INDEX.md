# Whilly Workshop Kit — Index / Индекс

> **EN:** Self-paced and instructor-led workshop kit for the **Ralph Wiggum loop**: drive Claude Code agents around a task board until the work is done. Built on top of `whilly-orchestrator`.
>
> **RU:** Воркшоп-кит для **Ralph Wiggum loop**: непрерывный цикл «задача → агент → ревью → следующая». Базируется на `whilly-orchestrator`.

---

## Reading order / Порядок чтения

| # | Doc | Read when / Когда читать |
|---|---|---|
| 1 | [TUTORIAL.md](TUTORIAL.md) | Hands-on first run / Первый запуск, hands-on (~60 min) |
| 2 | [BRD-Whilly.md](BRD-Whilly.md) | Why we built it, KPIs / Зачем, бизнес-цели, KPI |
| 3 | [PRD-Whilly.md](PRD-Whilly.md) | What we built, contracts / Архитектура, контракты, scope |
| 4 | [READINESS-REPORT.md](READINESS-REPORT.md) | Workshop fitness check / Готовность к воркшопу |
| 5 | [ROADMAP.md](ROADMAP.md) | What's next / Дорожная карта расширений |
| 6 | [adr/](adr/) | Why these decisions / Почему такие архитектурные решения |

---

## Quick links / Быстрые ссылки

- **Source code:** [`/whilly/`](../../whilly/) — orchestrator package
- **CLI reference:** [`Whilly-Usage.md`](../Whilly-Usage.md)
- **Task schema:** [`Whilly-Interfaces-and-Tasks.md`](../Whilly-Interfaces-and-Tasks.md)
- **Sample tasks:** [`examples/workshop/tasks.json`](../../examples/workshop/tasks.json)
- **Demo issues:** open issues with label `whilly:ready` in this repo

---

## What you can build in one workshop day / Что соберёте за один день воркшопа

```
┌─────────────────────────────────────────────────────┐
│                ONE-DAY WORKSHOP TARGET              │
└─────────────────────────────────────────────────────┘

  Hour 1:   Install + first run on tasks.json
            (Ralph loop for 1 task, dashboard appears)

  Hour 2-3: Add GitHub Issues source — agent picks
            real open issue from your repo

  Hour 4:   Add PR sink — agent opens a PR you can review

  Hour 5:   Add Decision Gate — agent refuses unclear
            issues with `needs-clarification`

  Hour 6:   Self-hosting bootstrap — point whilly at
            its own repo, watch it close 1 issue end-to-end

  ─── "I'm helping!" ──────────────────────────────────
```

---

## Audience / Аудитория

- **Engineers** who never built an agent loop and want a 1-day on-ramp.
- **Tech leads** evaluating Ralph-style automation against Devin/Cursor/Codex.
- **Workshop facilitators** who need a working reference + lecture material.

No prior agent experience required. Python 3.10+, git, `gh` CLI, Anthropic API key.

---

**Status:** v1 · 2026-04-20 · maintained alongside the codebase.
