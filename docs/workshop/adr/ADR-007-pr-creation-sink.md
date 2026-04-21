# ADR-007 — PR creation sink via `gh` CLI

- **Status:** accepted (gap pack)
- **Date:** 2026-04-20
- **Deciders:** project author
- **Domain:** sink

## Context

После того, как агент закрыл задачу (`task.status = done`), результат должен попасть в общий процесс ревью. Для self-hosting demo — это PR в `mshegolev/whilly-orchestrator`. В производственном scenario — PR в любой проект, на котором whilly запущен.

Требования:

1. **Никогда не пушить в main напрямую** — security gate.
2. PR должен содержать **traceability** (issue link, task id, cost, duration).
3. Не блокировать loop при ошибке создания PR.
4. Совместимо с per-task worktree (`WHILLY_WORKTREE=1`) — push из правильной директории.
5. Совместимо с plan-level workspace (`.whilly_workspaces/{slug}/`) когда per-task worktree не используется.

## Decision

**Реализуем `whilly/sinks/github_pr.py`** — модуль, активируемый через `--pr-on-done` (или `WHILLY_PR_ON_DONE=1`). После каждой done task:

1. Внутри worktree: `git push origin HEAD:whilly/{task_id}` (force-with-lease).
2. `gh pr create --title "..." --body "..." --base main` (либо `--draft` если флаг).
3. Если task имеет `prd_requirement` = GH issue URL → `Closes #N` в body.
4. Логируем `sink.pr.created` или `sink.pr.failed`.

Loop **не падает** при ошибке sink — task остаётся `done`, PR-failure лог.

## Considered alternatives

### PyGithub.create_pull(...)

- ✅ Прямой API.
- ❌ Дополнительная dependency.
- ❌ Дублирует то, что уже делает `gh`.

### Direct git push to main

- ❌ Запрещено security gate.

### `gh` CLI (выбрано)

- ✅ Reuse auth.
- ✅ Использует пользовательскую конфигурацию (default branch, labels, reviewers).
- ✅ Один и тот же tool для source и sink — ноль cognitive load.

### Webhook/API custom

- ❌ Переусложнение.

## Decision details

### Branch naming

- `whilly/{task_id}` (e.g. `whilly/GH-42`, `whilly/TASK-001`).
- При collision (branch existed) — добавляется суффикс `-{timestamp}`.

### PR body template

```markdown
Implements [issue link or task id].

### Description
{task.description}

### Acceptance criteria
{ - "..." for each task.acceptance_criteria }

### Validation
{ - "..." for each task.test_steps }

### Whilly run
- task_id: {task.id}
- cost: ${cost_usd}
- duration: {duration_s}s
- agent log: `{log_file_path}`
- JSONL events: `whilly_logs/whilly_events.jsonl`

---
🤖 Opened by [whilly-orchestrator](https://github.com/mshegolev/whilly-orchestrator).
Human review required before merge.
```

### Push semantics

- `git push origin HEAD:whilly/{task_id} --force-with-lease`
- `--force-with-lease` защищает от перезаписи чужих коммитов.
- Если push fails (permission, conflict) → `sink.pr.failed` event, task остаётся done.

### `gh pr create` invocation

```bash
gh pr create \
  --base main \
  --head whilly/{task_id} \
  --title "{task.id}: {task.description[:60]}" \
  --body "$(cat /tmp/whilly_pr_body_{task.id}.md)" \
  [--draft]
```

### Closes #N injection

Если `task.prd_requirement` matches `github.com/.../issues/(\d+)` → injection в body выше списка acceptance: `Closes #{N}`.

## Consequences

### Positive

- Self-hosting demo замыкается: issue → agent → PR → human review → merge.
- Полная traceability в PR — ревьюер видит cost / duration / log.
- `--draft` удобен для review pile up без notification noise.
- Loop не ломается на sink errors — robust.

### Negative

- Зависим от `gh` CLI и git push permissions.
- PR title обрезается на 60 символов — длинные descriptions усекаются (acceptable).
- Не управляет labels / reviewers — но можно добавить флагами в follow-up.

### Neutral

- Будущие sinks (Slack notification, Jira comment) — отдельные модули по тому же контракту.

## References

- `whilly/sinks/github_pr.py` (gap pack).
- PRD §2.2.2 FR-PR-1 to FR-PR-5.
- ADR-006 — GH source.
- ADR-021 — Draft PR vs auto-merge policy (clarifies how the `draft` flag here composes with the e2e `--allow-auto-merge`).
