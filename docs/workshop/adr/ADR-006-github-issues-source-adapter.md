# ADR-006 — GitHub Issues source adapter

- **Status:** accepted (gap pack)
- **Date:** 2026-04-20
- **Deciders:** project author
- **Domain:** source

## Context

Workshop demo требует "self-hosting bootstrap" — whilly запускается на репо `mshegolev/whilly-orchestrator` и закрывает свои же issues. Нужен модуль, который:

1. Читает open issues с конкретного label (`whilly:ready`).
2. Конвертирует issue → `Task` без потерь (priority, files, acceptance, traceability).
3. Записывает результат в `tasks.json`, чтобы дальше работал стандартный loop.
4. Idempotent — повторный запуск не теряет статус уже взятых задач.
5. Не вводит новых runtime dependency кроме `gh` CLI.

## Decision

**Реализуем `whilly/sources/github_issues.py`** — модуль, который:

- Вызывает `gh issue list --repo {owner/repo} --label {label} --state open --json number,title,body,labels,url --limit 50`.
- Парсит каждый issue в `Task` с детерминированным `id = f"GH-{number}"`.
- Записывает `tasks.json` в текущую директорию (или путь из `--out`).
- Сохраняет `source` блок в JSON для re-fetch / трейсабилити.

CLI: `whilly --source gh:owner/repo` или `whilly --source gh:owner/repo:custom-label`.

## Considered alternatives

### PyGithub Python SDK

- ✅ Удобный объектный API.
- ❌ Дополнительная dependency (PyGithub).
- ❌ Дублирует работу `gh` (которая у пользователя обычно уже стоит для других task).
- ❌ Auth handling — нужно отдельно учить токен из gh keyring.

### `gh issue list --json` + Python json (выбрано)

- ✅ Zero new Python deps.
- ✅ Reuse user's existing `gh auth` (keyring, env, gh config).
- ✅ Workshop participants скорее всего уже имеют `gh` (любой минимальный onboarding для GH require'ит).
- ❌ Subprocess overhead — но это однократный fetch, не critical path.

### Webhook-based (push GH issues → whilly endpoint)

- ✅ Real-time.
- ❌ Требует public endpoint — workshop friction.
- ❌ Переусложнение для текущего scale.

## Decision details

### CLI integration

```bash
# basic
whilly --source gh:mshegolev/whilly-orchestrator

# custom label
whilly --source gh:owner/repo:custom-label

# combined with other gap pack flags
whilly --source gh:mshegolev/whilly-orchestrator --pr-on-done --decision-gate
```

После fetch создаётся `tasks.json`, дальше loop работает обычным образом.

### Issue → Task mapping

| Task field | Source from issue |
|---|---|
| `id` | `f"GH-{number}"` |
| `phase` | `"GH-Issues"` |
| `category` | `"github-issue"` |
| `priority` | label `priority:critical/high/medium/low` (default `medium`) |
| `description` | issue title + first 500 chars of body |
| `dependencies` | parsed from issue body `**Depends:** GH-N, GH-M` |
| `key_files` | parsed from issue body `**Files:** path1, path2` |
| `acceptance_criteria` | parsed from `## Acceptance` section bullets |
| `test_steps` | parsed from `## Test` section bullets |
| `prd_requirement` | issue URL |

### Idempotent re-fetch logic

```
existing_tasks = load tasks.json (if exists)
fetched = parse gh issue list

for fetched_task in fetched:
    if existing has fetched_task.id:
        # Update mutable fields, preserve status
        existing.update(description, priority, key_files, acceptance, test_steps)
        keep status
    else:
        existing.append(fetched_task with status="pending")

# Mark "skipped" if previously open issue is no longer in fetched
for old in existing where source=GH:
    if old.id not in fetched_ids and old.status in ("pending", "in_progress"):
        old.status = "skipped"
        old.note = "issue closed externally"
```

### `source` block in tasks.json

```json
{
  "project": "github-mshegolev/whilly-orchestrator",
  "source": {
    "type": "github_issues",
    "repo": "mshegolev/whilly-orchestrator",
    "label": "whilly:ready",
    "fetched_at": "2026-04-20T03:30:00Z"
  },
  "tasks": [/* ... */]
}
```

### Errors

| Scenario | Behavior |
|---|---|
| `gh` отсутствует | exit 1, hint to install |
| `gh auth status` fails | exit 1, hint `gh auth login` |
| Repo не существует | exit 1, repo URL in error |
| Network timeout | single retry (5s, 30s), then exit 1 |
| Issue body содержит секреты (`AKIA...` regex match) | warning в JSONL, продолжаем |

## Consequences

### Positive

- Self-hosting demo работает.
- Reuse `gh` auth — никакого нового token management.
- Loop остаётся source-agnostic — любые будущие adapters встают в эту же модель.
- Idempotent re-fetch позволяет периодический pull без потери прогресса.

### Negative

- Polling-based (не push) — задержка между creation issue и его pickup whilly.
- `gh` CLI должен быть установлен (но это уже подразумевается для workshop).
- Парсинг markdown body — fragile; documenting expected format в TUTORIAL.

### Neutral

- В будущем: Linear / Jira / GitLab adapters следуют этому же паттерну.

## References

- `whilly/sources/github_issues.py` (gap pack).
- PRD §2.2.1 FR-GH-1 to FR-GH-6.
- ADR-002 — source-агностичный state model.
- ADR-007 — PR sink, использует тот же `gh` CLI.
