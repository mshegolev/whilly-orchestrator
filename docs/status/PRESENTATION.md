<!--
  Whilly Orchestrator — slide deck
  =================================
  Гамма-импорт: gamma.app → Create new → Import → Paste in text →
  вставить весь этот файл целиком. `---` = разделитель слайдов.
  H1 (`# `) = заголовок слайда. Bullets = пункты.

  Альтернативно: Marp/Slidev/reveal-md тоже понимают этот формат.
-->

---

# Whilly Orchestrator
## Ralph Wiggum's smarter brother — для AI-агентов

**Промежуточный отчёт · неделя 1**
2026-04-19 → 2026-04-25
v3.3.0 в PyPI · 661 тест · 12k LOC

---

# В одном предложении

> Whilly = continuous agent loop по технике Ghuntley
> **+ TRIZ + Decision Gate + PRD-визард + self-heal.**

«I'm helping — and I've read TRIZ.»

---

# Зачем мы это делаем

- **Ralph-loop** хорош, но «нем» — гоняет агента по любой ерунде.
- Нужна **обвязка с мозгами:**
  - откажись от мусорной задачи **до** LLM-расхода (Decision Gate)
  - найди противоречие, прежде чем кодить (TRIZ)
  - сначала PRD, потом patches
  - падай предсказуемо и чинись сам (self-heal)
- Курс vNext: **Issue → Draft PR за один прогон** (Whilly Forge).

---

# Архитектура (high-level)

```
Issue ──► Intake ──► Normalize ──► Readiness ──► Strategy ──► Plan
              ──► Execute ──► Verify ──► Repair ──► Compose PR ──► Timeline
```

- **Source** — Issues / Projects v2 / Jira / tasks.json / PRD-wizard
- **Pre-flight** — TRIZ · Decision Gate · Decomposer
- **Orchestrator** — TaskManager + StateStore + plan_batches
- **Runners** — tmux / git-worktree / subprocess
- **Backends** (Protocol) — Claude CLI · OpenCode · claude_handoff
- **Post** — verify · self-heal · compose PR · board sync

---

# Что уже сделали — релизы за 6 дней

| Версия | Дата | Главное |
|---|---|---|
| **v3.0.0** | 19.04 | Базовый loop + TUI + tmux/worktree |
| **v3.1.0** | 20.04 | **Self-Healing System** (NameError/ImportError/TypeError) |
| **v3.2.0** | 22.04 | Layered config · Jira · Projects v2 · claude_handoff · 9-матриц CI |
| **v3.2.1** | 22.04 | **Self-healed release** — loop пережил собственный баг |
| **v3.2.2** | 24.04 | `doctor`: ghost-plan detector |
| **v3.3.0** | 24.04 | **BREAKING:** workspace off by default |

---

# Метрики недели

- **6** релизов в PyPI за **6** дней
- **12 125** строк Python в `whilly/`
- **661** тест (+35% за неделю)
- **9** CI-матриц зелёных на каждый PR (Linux/macOS/Win × 3.10/3.11/3.12)
- **3** agent-backends за общим Protocol
- **4** источника задач + PRD-визард

---

# Технологический стек

- **Python 3.10+** · Linux / macOS / Windows
- **Runtime:** `rich`, `psutil`, `platformdirs`, `keyring`, `tomli`
- **Принцип:** **никаких `requests`** — только stdlib `urllib`
- **External CLIs (subprocess):** Claude CLI · OpenCode · tmux · git worktree · gh
- **Dev:** pytest · ruff 0.11.5 · mypy 1.8
- **CI:** GitHub Actions × 9 матриц
- **Distribution:** PyPI · pipx-friendly · GitHub Pages docs

---

# Самое больное место №1
## Subprocess spawn под нагрузкой (macOS)

- Параллельные агенты упирались в `EAGAIN` / `RLIMIT_NPROC`.
- На macOS лимит процессов на пользователя пробивается **раньше**, чем `ulimit -n`.
- Решение: retry-loop при спавне ([#213](https://github.com/mshegolev/whilly-orchestrator/pull/213)).
- **Урок:** на macOS нужно мерить лимиты **до** загрузки.

---

# Самое больное место №2
## Workspace-by-default — анти-фича

- Plan-level git worktree выглядел «безопасным умолчанием».
- В пилотах с pending-changes и абсолютными путями в `.venv` — **ломал больше, чем защищал**.
- Откатили в v3.3.0 (**BREAKING**) — болезненное, но честное решение.
- **Урок:** «безопасный по умолчанию» нужно проверять на проде, а не на смоук-тестах.

---

# Самое больное место №3
## Парсинг внешних CLI

- `gh pr create` иногда печатает `Warning:` строкой stdout-а.
- Whole-stdout-capture закрыл чистый PR (#204).
- Self-healing loop **сам это поймал** и переоткрыл PR (#205).
- Решение: строгий regex на PR-URL + отдельный `gh pr merge`.
- **Урок:** «никаких whole-stdout-capture для CLI-вывода».

---

# Self-healed release — главная история недели

- v3.2.1 — релиз, **в котором починен баг, обнаруженный в этом же релизе**.
- Сценарий:
  1. `whilly-auto-loop.sh` запускает Whilly на собственных issue'ах.
  2. Bug в `gh pr create` парсере → PR #204 закрыт по ошибке.
  3. Loop **сам обнаружил** инцидент, открыл новый PR #205 с фиксом.
  4. PR #205 мёрджнут → ушёл в PyPI.
- **Документировано:** [POSTMORTEM-PR-204.md](../workshop/POSTMORTEM-PR-204.md).

---

# Что дальше

- **vNext / Forge** — FR-1 … FR-11 (по PR на каждый):
  Intake · Normalize · Readiness · Strategy · Plan · Verify · Repair · Compose PR · Timeline
- **Quality gate v2** — structured verdict + repo-profile aware
- **Demo screencast (NFR-2)** — 3-минутное видео для PyPI-лендинга
- **Backend coverage** — dogfood OpenCode на собственных issue'ах
- **Onboarding** — Workshop kit → видео-туториал

---

# Ссылки

- **Repo:** https://github.com/mshegolev/whilly-orchestrator
- **PyPI:** https://pypi.org/project/whilly-orchestrator/
- **Docs:** https://mshegolev.github.io/whilly-orchestrator/
- **Workshop kit (HackSprint1):** docs/workshop/INDEX.md
- **Постмортем PR-204:** docs/workshop/POSTMORTEM-PR-204.md

---

# Спасибо

«I'm helping!» — Whilly Wiggum

*Вопросы?*
