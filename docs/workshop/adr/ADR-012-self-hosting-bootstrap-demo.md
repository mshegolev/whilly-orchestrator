# ADR-012 — Self-hosting bootstrap demo

- **Status:** accepted (gap pack)
- **Date:** 2026-04-20
- **Deciders:** project author
- **Domain:** workshop / demo

## Context

Workshop требует впечатляющего demo. Возможные сценарии:

1. **Sample tasks.json:** показать `whilly` запущенным на синтетическом sample plan. Безопасно, повторимо, но "игрушечно".
2. **Real third-party repo:** запустить `whilly` на open-source проекте. Реалистично, но сложно подготовить (нужны safe issues).
3. **Self-hosting bootstrap:** `whilly` запускается на собственном репо `mshegolev/whilly-orchestrator`, читает свои же issues, делает свои же PRs.

## Decision

**Канонический demo — self-hosting bootstrap**:

```bash
whilly --source gh:mshegolev/whilly-orchestrator \
       --pr-on-done \
       --decision-gate
```

Демонстрирует:
- Реальную интеграцию с GH Issues.
- Ralph loop в действии.
- Decision Gate (refusing 1 «плохо описанный» issue).
- PR creation с traceability.
- Dashboard / TUI работает.

## Considered alternatives

### Sample plan only

- ✅ Safe.
- ❌ Не показывает GH integration.
- ❌ "Игрушечный" — снижает доверие.

### Real third-party repo

- ✅ Realistic.
- ❌ Подготовка: нужно найти подходящий проект, заведённые issues, разрешение от maintainers.
- ❌ Side effects невозможно предсказать (PR в чужое репо могут не понравиться).

### Self-hosting bootstrap (выбрано)

- ✅ Полный контроль над issues и PR.
- ✅ Драматический эффект «whilly решает свои собственные tasks».
- ✅ Workshop participants могут потом по тому же паттерну запустить на своих репо.
- ✅ Issues и PRs остаются в whilly repo как examples.
- ❌ Реальные изменения в whilly могут затронуть пользователей (но review гейт защищает).

## Decision details

### Подготовка

- В `mshegolev/whilly-orchestrator` заводится 5-10 issues с label `whilly:ready`. Примеры:
  - `Add CONTRIBUTING.md badge to README`
  - `Bump pyproject.toml version pin for ruff`
  - `Add /healthcheck endpoint to web_status.py`
  - `Fix typo in dashboard.py docstring`
  - `Add example tasks.json for sandbox testing`
  - 1 заведомо «плохой» issue без описания — для показа Decision Gate refuse
- README.md имеет секцию "Workshop kit" с CTA на TUTORIAL.md.

### Demo flow (~3 минуты)

```
0:00 — Open terminal, cd whilly-orchestrator
0:10 — Show README "Workshop kit" section
0:30 — Run command
       whilly --source gh:mshegolev/whilly-orchestrator --pr-on-done --decision-gate

0:45 — Source adapter fetches issues, dashboard appears
1:00 — Decision Gate rejects 1 issue (label flip visible in browser)
1:15 — 2 agents launch in parallel tmux sessions
1:30 — First task done, PR appears in browser
2:30 — Second task done, second PR appears
3:00 — Stop demo, show JSONL events file
```

### Safety gates

- **Always human review** PR before merge — никаких auto-merge.
- **Worktree isolation** — agents не модифицируют main checkout.
- **Budget cap** `WHILLY_BUDGET_USD=10` на demo.
- **Per-PR draft option** — `--draft` если хотим preview без notification noise.

### Recovery if demo fails

- Pre-recorded screencast (target: `docs/workshop/demo.gif`, 3 min).
- JSONL replay через `dashboard --replay events.jsonl` (future feature).

## Consequences

### Positive

- Сильное "wow" впечатление: agent работает на orchestrator'е, в котором он живёт.
- Issues и PRs в репо — постоянная exhibition workshop'а.
- Workshop participants легко reproduces — fork → label → run.

### Negative

- Зависимость от стабильности `mshegolev/whilly-orchestrator` (issue lifecycle).
- Может создать "PR noise" в репо если запускать часто.
- Required: issues должны быть подготовлены и обновляться.

### Neutral

- Альтернативный demo на sample plan остаётся как Plan B (для участников без `gh` auth).

## References

- BRD §10 D6 — выбор demo сценария.
- PRD §4.2 Demo acceptance.
- README workshop section.
- TUTORIAL.md последняя секция.
