---
title: HackSprint1 — Agents Orchestrator — BRD
type: brd
created: 2026-04-18
updated: 2026-04-20
status: v2 · team-locked after kickoff
audience: bilingual (RU primary, EN summary)
related:
  - PRD-Whilly.md
  - READINESS-REPORT.md
  - ROADMAP.md
  - adr/README.md
---

# HackSprint1 — Agents Orchestrator — BRD

> **Назначение:** объяснить команде HackSprint1 — что мы делаем за этот спринт, на какие правила сдаёмся, какие минимальные требования закрываем, какие опциональные блоки приоритетны, какие риски и кто кураторы. PRD (как именно строим) — отдельный документ.
>
> **EN:** Hackathon-grade BRD for the HackSprint1 sprint. Minimum requirements, optional blocks priority, curator stakeholders, voting prize. Not a corporate BRD.

> ⚠️ Контекст этого BRD — **не корпоративный продукт**, а 10-дневный спринт по правилам клуба. Метрики, бюджет и dеливерэблы соответствуют формату хакатона, а не enterprise-cycle.

---

## 1. Executive Summary

Команда (2-4 человека, опционально соло) реализует **Agents Orchestrator** — систему «задача → AI-агент пишет код → PR → retry при ошибке → демо», которая закрывает все 5 минимальных требований клуба HackSprint1 и максимально отрабатывает 2-3 опциональных блока.

- **Старт:** 18 апреля 2026 (регистрация 17 апреля).
- **Дедлайн демо-видео:** 4 мая 2026, 10:00 MSK.
- **Голосование:** 8 мая 2026 (приз — 3 месяца подписки победителю).
- **Длительность:** ~10 рабочих дней + буфер на видео.
- **Цель:** зачёт по правилам клуба + произвести впечатление на голосовании.

**Унесём с собой:** компетенция по orchestration AI-агентов, рабочий прототип, демо-видео для CV/портфолио, общий чат команды с накопленным контекстом.

---

## 2. Business Context

> Эта секция — для внутренней мотивации команды. Не входит в контракт спринта.

### Почему сейчас

1. **2025-2026 — год agent orchestration.** Codex, Claude Code, Cursor Composer, Devin, Replit Agent — все рынки переходят к "агенты закрывают задачи".
2. **Стоимость токенов упала в 10-20×** за два года.
3. **Vendor lock-in** становится реальной болью. Свой open-source orchestrator — страховка.
4. **Hackathon-формат** даёт permission поэкспериментировать с инструментом, который иначе не возьмут в работу.

---

## 3. Стейкхолдеры

| Роль | Кто | Что хочет |
|---|---|---|
| **Команда спринта** | 2-4 человека (или соло) | сдать проект по правилам, не сгореть, унести демо в портфолио |
| **Кураторы клуба** | Геннадий Евстратов · Степан Гончаров · Михаил Мужаровский | видеть прогресс на check-in'ах, помочь с демо-форматом, зачесть проект |
| **Зрители голосования** | community клуба | впечатлиться демо-видео, проголосовать |

Никаких Engineering Lead / Security Review / CFO / Junior'ов — этих ролей в хакатоне нет. Подписи и approvals не требуются.

---

## 4. Goals

### Primary

| # | Goal | Outcome |
|---|---|---|
| **G1** | Knowledge Foundation | каждый участник может на собесе/в чате объяснить архитектуру orchestrator'а |
| **G2** | Working MVP | end-to-end закрыт хотя бы 1 реальный issue в реальном репо без ручного вмешательства |
| **G3** | Scalability Path | команда понимает что нужно для production (что в MVP-scope, что нет) |

### Secondary

| # | Goal | Outcome |
|---|---|---|
| **G4** | Portfolio | каждый участник может добавить "Built agent orchestrator (HackSprint1, 2026)" в CV / LinkedIn / портфолио проектов |
| **G5** | Vendor independence | архитектура позволяет заменить Claude на другой backend без переписывания core |
| **G6** | Voting impression | произвести впечатление на голосовании 8 мая (приз — 3 мес подписки) |

### Non-goals

- Production-grade deployment (K8s, observability, on-call).
- Multi-tenant / SaaS для внешних клиентов.
- Автоматический merge в main (всегда human review).
- Поддержка >2 типов задач в MVP (выберем 1 на kickoff, см. D6).
- HackSprint2/3/4 как заранее запланированный roadmap (этих спринтов не существует — мы их не планируем).

---

## 5. Success Criteria (KPIs) и зачётный gate

### Зачётный gate (правила клуба)

> Проект **зачтён**, если выполнены **все 5 минимальных требований**:
>
> 1. Задача поступает из источника (GH Issues / Linear / ручной список).
> 2. Агент пишет код для задачи.
> 3. Агент создаёт PR.
> 4. **Retry-петля при ошибке** (агент не падает на первой неудаче — пытается ещё раз).
> 5. Запуск на **реальном репо** (sandbox/собственное), не на mock'е.
>
> + демо-видео записано и приложено до 10:00 MSK 4 мая.

### Метрики

| # | Метрика | Target | Тип | Как измеряем |
|---|---|---|---|---|
| **K1** | End-to-end demo | 1 issue полностью закрыт агентом (issue → PR → passing CI) | gate | live demo / видео |
| **K3** | Cost per issue | ≤ $0.50 среднее на задачи выбранного типа | informational | JSONL `agent.done` events |
| **K4** | Success rate | ≥ 40% (MVP) на конкретном типе задач (см. D6) | informational | % issues closed без ручной правки |
| **K5** | Team learning | каждый участник может ответить на 10 ключевых вопросов архитектуры | gate-soft | опрос после спринта |
| **K7** | Optional blocks | реализовано 2-3 из 8 опциональных блоков | informational | self-report + linkable code |
| **K8** | Demo video | записано и загружено до дедлайна (4 мая 10:00 MSK) | gate | URL приложен к submit |

KPI K2 (lead-time на boilerplate с 1ч до 15мин) и K6 (next-feature за следующий спринт) сознательно убраны: review-time автоматизацией не сокращается, и следующего планового спринта не существует.

---

## 6. Что мы унесём с собой

> Без ROI-таблиц и employer-branding. Хакатон — это про опыт, не про экономику команды.

| Что | Кто получает | Когда |
|---|---|---|
| Компетенция Ralph-loop / agent orchestration | каждый участник | в процессе |
| Рабочий прототип (репо + README) | команда / личное портфолио | к 4 мая |
| Демо-видео ~3 мин | каждый участник | к 4 мая |
| Дополнительные релевантные строки в CV / LinkedIn | каждый | бонус навсегда |
| 3 месяца подписки (приз победителю) | топ голосования | 8 мая |

---

## 7. Scope (бизнес-уровень)

### In scope (MVP — 5 минимальных требований)

1. **Один источник задач** (GitHub Issues — выбран по минимуму интеграций).
2. **Один AI backend** (TBD на kickoff — D3).
3. **Один тип задач** (выбираем на kickoff — D6).
4. **Retry-петля** при ошибке агента (exponential backoff, max N попыток).
5. **Запуск на реальном репо** (sandbox под HackSprint1).
6. **PR creation** обязателен (никаких direct push в main).

### Optional blocks — приоритет

| Приоритет | Блок | Почему |
|---|---|---|
| **High** | Observability (JSONL + cost tracking) | дёшево, эффектно для демо |
| **High** | Quality gates (линтер + тесты в CI/локально) | повышает success rate |
| **High** | Agentic code review (агент-ревьюер до merge) | wow-эффект для голосования |
| **Mid** | Параллельные реализации (несколько агентов на одной задаче, выбор лучшего) | технически интересно, дорого по токенам |
| **Mid** | Выбор стратегии по типу задачи (router для разных prompt strategies) | требует больше кода |
| **Low / skip** | Декомпозиция крупных задач | помогает на больших задачах, у нас MVP scope маленький |
| **Low / skip** | Merge conflicts resolution | редкий edge case |
| **Low / skip** | Долгосрочная память агента | вне нашего scope |

**Цель: реализовать 2-3 high-priority блока**.

### Out of scope (явно)

- Auto-merge PR без человека.
- Мульти-репо.
- Distributed queue (Redis/NATS).
- Web UI / SaaS.
- HackSprint2/3 — никаких запланированных продолжений.

---

## 8. Constraints

| Тип | Ограничение |
|---|---|
| **Время** | 10 рабочих дней (18 апреля — 1 мая) + буфер до 4 мая для видео |
| **Команда** | 2-4 человека (соло возможно, но scope тогда сокращается до MVP без optional блоков) |
| **Бюджет AI** | ~$300 личных средств на токены (compensation от клуба отсутствует — бюджет личный, не компенсируется). Рекомендуется hard-cap WHILLY_BUDGET_USD=300 на весь спринт |
| **Стек** | **Python**. Subprocess `claude -p` vs `claude-agent-sdk` — решается на kickoff (см. D7) |
| **Security** | агент **никогда** не пушит в main напрямую, только PR; sandbox-репо изолирован; secrets через `.env` + gitignore |
| **Tooling** | `gh` CLI у каждого участника, git CLI, общий sandbox-репо, общий чат команды |
| **Human gate** | каждый PR требует ручного merge (нет auto-merge) |
| **Время суток** | вечерами/выходными — реалистичный режим для хакатона; не "только в рабочие часы" |

---

## 9. Assumptions

- У команды есть доступ к **воркспейсу со спринтом** (Telegram чат клуба + чат команды).
- **Кураторы доступны для вопросов** через Telegram (response time — часы, не минуты).
- У каждого участника есть Anthropic / OpenAI API key (личный или corp-key, на личном бюджете).
- У каждого установлены `gh` CLI и git CLI.
- Есть **тестовый sandbox-репо** на GitHub, где можно безопасно экспериментировать.
- Интернет доступен (для API).
- Минимум 1 check-in с куратором запланирован на день 4-5 спринта.

---

## 10. Risks

| # | Риск | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| **R1** | Команда не успеет MVP за 10 дней | Средняя | Высокий | Scope aggressive cut: 1 источник, 1 backend, 1 тип задач. Жёсткий приоритет минимальных требований над optional блоками. |
| **R2** | Агент жжёт токены сверх личного бюджета | Высокая | Средний | Hard cap `WHILLY_BUDGET_USD=300` (или меньше per-задаче). Декомпозируем дорогие задачи или режем их. |
| **R3** | Агент ломает sandbox-репо | Средняя | Низкий (sandbox же) | Только worktree + PR, никогда direct push в main. |
| **R4** | **Утечка секретов в промпт → leak в API / в issue body / в PR description** | Высокая | **Высокий** | trufflehog/gitleaks pre-prompt scan покрывает только часть вектора. Дополнительно: ручной review issues перед label `whilly:ready`, никаких real env vars в sandbox-репо. **Обсудить отдельно на kickoff.** |
| **R5** | Команда не договорится о scope на kickoff | Средняя | Средний | BRD + PRD + 1-час facilitated kickoff с чёткой повесткой (D3, D6, D7). |
| **R6** | API rate limits (Anthropic/OpenAI) | Низкая | Низкий | Backoff (уже встроен в whilly), fallback на 2-й backend если есть время реализовать. |
| **R7** | Результат MVP не впечатляет на голосовании | Средняя | Средний | Демо готовится с **дня 5**, черновое видео ко дню 8, чистовое — за 2 дня до дедлайна. |
| **R8** | После спринта никто не дорабатывает | Высокая | Низкий | Это нормально для хакатона. Зафиксируем lessons learned, уйдём с портфолио. |
| **R9** | **Демо-видео не записано / записано плохо** | Средняя | **Высокий** (проект не зачтётся) | Скелет демки со дня 5, прогон с куратором день 8-9, чистовая запись за 2 дня до дедлайна. Pre-recorded backup всегда лучше live demo. |
| **R10** | PR review bottleneck на sandbox-репо (агент нагенерит больше чем успеваем смотреть) | Средняя | Средний | Decision Gate (отказ от мусорных issues), draft PR mode, ограничить max in-flight PRs. |
| **R11** | **Prompt injection через issue body** | Средняя | Высокий | Ограничить агенту `--allowedTools` (запретить exec на критичных системах), sandbox-репо изолирован, не подключаем к нему secrets. |

---

## 11. Decision Log

| # | Вопрос | Решение | Дата |
|---|---|---|---|
| **D1** | Язык реализации | Python (не Go/bash) — команде ближе | 2026-04-18 |
| **D2** | Референс-проекты + материалы кураторов | grkr (минимальная модель), yolo-runner (как цель), 3 сессии кураторов в клубе (см. Notebook) | 2026-04-18 |
| **D3** | AI backend на MVP | TBD на kickoff (Claude vs Codex) | — |
| **D4** | Источник задач | GitHub Issues (минимум интеграций — `gh` CLI уже у всех) | 2026-04-18 |
| **D5** | Изоляция | git worktree (взято у grkr) | 2026-04-18 |
| **D6** | **Какой тип задачи берём первым** (определяет K4) | TBD на kickoff. Кандидаты: README-badge, healthcheck endpoint, dependency bump, README typo fix, test scaffold | — |
| **D7** | **Subprocess `claude -p` vs `claude-agent-sdk`** | TBD на kickoff. Subprocess проще и наследует пользовательскую конфигурацию; SDK даёт более тонкий контроль | — |

> D3, D6, D7 — обязательно решены до конца Day 1 (kickoff).

---

## 12. Timeline (10 рабочих дней)

> Старт регистрации: 17 апреля. Старт работы: 18 апреля. Hard deadline submit: **4 мая 10:00 MSK**. Голосование: 8 мая.

| Day | Дата | Цель | Deliverable |
|---|---|---|---|
| **1** | Sat 18 апр | Kickoff | Roles assigned, scope locked, D3 / D6 / D7 решены, sandbox-репо создан |
| **2-3** | Sun 19 — Mon 20 апр | MVP-скелет | Runner + worktree + publisher (PR creator) — компилируется и запускается на sample |
| **4** | Tue 21 апр | Первое e2e | Прогон на 1 реальной задаче в sandbox: issue → агент → PR (retry может быть mock) |
| **5** | Wed 22 апр | Retry-петля + check-in куратора | Real retry on failure, минимальное требование #4 закрыто. Show curator → feedback. |
| **6-7** | Thu 23 — Fri 24 апр | 1-2 опциональных блока (high priority) | Observability + Quality gates — JSONL логи и линтер/тесты в pipeline |
| **8** | Sat 25 апр | 3-й опциональный блок / стабилизация | Agentic code review ИЛИ buffer для багфиксов |
| **9** | Sun 26 апр | Черновик демо-видео | Записать draft, показать куратору, собрать feedback |
| **10** | Mon 27 апр | Чистовое видео + README + submit-prep | Финальная запись, README репо обновлён, готов к submit |
| **buffer** | 28 апр — 4 мая | Polishing, rollback фиксы | Никаких новых фич — только стабилизация |
| **Submit** | Mon 4 мая 10:00 MSK | **Hard deadline** | Видео + repo URL присланы кураторам |
| **Voting** | Thu 8 мая | Голосование | Result announcement |

> ⚠️ Сегодня **20 апреля** = Day 3. Если читаете этот BRD позже — пересчитайте позицию относительно дедлайна 4 мая.

> 💡 **Check-in с куратором (Day 4-5)** — самый недооценённый ресурс. 30 минут разговора стоит часов догадок. **Запланировать обязательно.**

---

## 13. Что бы доделали сами, если бы продолжили

> Это не roadmap клуба — это наши собственные идеи на «если когда-нибудь захотим вернуться».

- Multi-backend (Claude + Codex + Gemini) с fallback'ом.
- DAG зависимостей между задачами.
- Production deploy (K8s + dashboard + team-wide rollout).
- Multi-repo адаптеры (Linear / Jira / GitLab Issues).
- Self-healing с другим промптом при повторяющейся ошибке.
- MCP-server интерфейс (whilly tools published as MCP).

Если кто-то из команды захочет — это подходящие follow-up issues для backlog.

---

## 14. Approval

> Approval-блок не требуется. HackSprint1 — community-format, без формальных подписей.

Единственный «approval-эквивалент»: **зачтено / не зачтено** кураторами после submit'а 4 мая.

---

## 15. Глоссарий

- **Ralph Wiggum loop** — техника непрерывного цикла, где AI-агент берёт задачу за задачей. Названа по фразе "I'm helping!" из Симпсонов. См. [Ghuntley's post](https://ghuntley.com/ralph/).
- **Agent / AI agent** — LLM с доступом к tools (file system, git, subprocess), способная выполнять многошаговые задачи.
- **Orchestrator** — программа, управляющая циклом «источник задач → агент → проверка → commit/PR».
- **Worktree** — git-механизм, позволяющий иметь несколько рабочих копий одного репо.
- **Decision Gate** — точка перед имплементацией, где агент решает «берусь / отказываюсь» (экономия токенов).
- **MCP (Model Context Protocol)** — стандарт от Anthropic для подключения внешних tools к AI-агентам.
- **JSONL event log** — построчный JSON-лог событий (1 объект на строку), удобно парсить.
- **Agent SDK** — `claude-agent-sdk` (Python пакет от Anthropic), даёт programmatic agent loop без CLI.
- **stream-json** — потоковый формат вывода Claude CLI (`--output-format stream-json`), позволяет читать события агента в реальном времени.
- **Quality gate** — автоматическая проверка (линтер, тесты, security scan), без прохождения которой PR не считается ready.
- **Retry-loop** — встроенная в orchestrator петля «попробовать → fail → подождать → попробовать снова с exp.backoff». Минимальное требование клуба №4.
- **gh CLI** — официальный GitHub CLI tool. Используется и для чтения issues, и для создания PR.
- **Sandbox-репо** — отдельный публичный/приватный GitHub репозиторий, выделенный под эксперименты HackSprint1. Не production.

---

## Приложения

### A. Связанные документы

- [PRD-Whilly.md](PRD-Whilly.md) — что и как строим (технически).
- [READINESS-REPORT.md](READINESS-REPORT.md) — gap analysis whilly vs HackSprint1 scope.
- [ROADMAP.md](ROADMAP.md) — декомпозиция задач.
- [adr/](adr/) — Architecture Decision Records (12 записей).
- [TUTORIAL.md](TUTORIAL.md) — пошаговое руководство.

### B. Внешние материалы

- [stepango/grkr](https://github.com/stepango/grkr) — bash-референс.
- [egv/yolo-runner](https://github.com/egv/yolo-runner) — Go-референс.
- [Anthropic — Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) — обязательная статья.
- [NotebookLM с материалами](https://notebooklm.google.com/notebook/d4c73023-591e-4583-b26f-864be367749f) — спрашивай на русском.
- 3 сессии кураторов клуба (записи доступны в Telegram-чате).
- Telegram-чат клуба: общие объявления, кураторы.
- Telegram кураторы: [@jewpacabra](https://t.me/jewpacabra), [@stepango](https://t.me/stepango), [@miky_muz](https://t.me/miky_muz).

---

**Status:** v2 · 2026-04-20 · team-locked after kickoff. Будет финализирован после check-in'а с куратором (Day 4-5).
