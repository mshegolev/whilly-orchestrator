# ADR-005 — JSONL events as the observability layer

- **Status:** accepted
- **Date:** 2026-04-19 (retroactive)
- **Deciders:** project author
- **Domain:** observability

## Context

Loop делает много мелких событий: старт plan, старт iteration, dispatch task, agent done, budget warning, deadlock, finish. Нужен способ:

1. **Долгоживуще логировать** для post-hoc анализа (cost analytics, debug).
2. **Стримить в реальном времени** в dashboard / TUI / dashboard-like CI tools.
3. **Парсить простыми инструментами** (`jq`, `grep`, Python one-liners).
4. **Не зависеть от внешней инфраструктуры** (без Loki, Datadog, Sentry).
5. **Workshop-friendly** — участник видит лог в терминале и понимает, что происходит.

## Decision

**Все события loop'а пишутся в `whilly_logs/whilly_events.jsonl`** — построчный JSON (один JSON object на строку). Дополнительно: в headless mode этот же поток пишется на stdout.

## Considered alternatives

### Plain text logs (`logging` module)

- ✅ Привычно.
- ❌ Не парсится без regex.
- ❌ Поля сваливаются в один string.
- ❌ Сложно фильтровать по cost / event type / task_id.

### structlog → JSON formatter

- ✅ Те же JSONL events.
- ✅ Богатые processors.
- ❌ Дополнительная dependency.
- ❌ Overkill для нашего объёма.

### OpenTelemetry / OTLP

- ✅ Industry standard.
- ❌ Требует collector.
- ❌ Не workshop-friendly.

### SQLite log table

- ✅ Запросы.
- ❌ Не human-readable.
- ❌ Не stream-friendly.

### JSONL (выбрано)

- ✅ Standard ad-hoc формат.
- ✅ `jq`-friendly: `cat events.jsonl | jq '. | select(.event=="task.done") | .cost_usd'`.
- ✅ Append-only, не нужен lock на чтение.
- ✅ Streaming — строка пишется → следующая утилита её читает.
- ✅ Schema эволюционирует свободно (новые поля игнорируются старыми парсерами).
- ❌ Нет встроенной типизации (но есть документированный schema).

## Decision details

### File location

- `whilly_logs/whilly_events.jsonl` (relative to plan workspace cwd).
- Одна строка на событие, append-only.
- При rotate — переименовать `events.jsonl` → `events.jsonl.YYYYMMDD`, начать новый файл.

### Required fields per event

```jsonc
{
  "ts": "ISO 8601 UTC",          // обязательно
  "event": "string",             // обязательно, snake_case
  // ... event-specific payload
}
```

### Event taxonomy (текущая)

| Event | Payload | Emitter |
|---|---|---|
| `plan.start` | plan_path, total tasks | cli |
| `plan.end` | done, failed, skipped, cost_total | cli |
| `iteration.start` | iter_num, ready | cli |
| `iteration.end` | iter_num, done_count_delta | cli |
| `task.start` | task_id, worker (tmux/subprocess) | cli |
| `task.done` | task_id, cost_usd, duration_s, tokens | cli |
| `task.failed` | task_id, reason, retry_count | cli |
| `task.skipped` | task_id, reason | cli |
| `budget.warning` | spent, cap | cli |
| `budget.exceeded` | spent, cap | cli |
| `deadlock.detected` | task_id, iterations_stuck | cli |
| `auth.error` | task_id | cli |
| `source.fetch` | source_type, repo, new, updated | source adapter (gap pack) |
| `decision_gate` | task_id, decision, reason, cost_usd | decision_gate (gap pack) |
| `sink.pr.created` | task_id, pr_url, branch | pr sink (gap pack) |
| `sink.pr.failed` | task_id, reason | pr sink (gap pack) |

### Headless mode

При `WHILLY_HEADLESS=1` ИЛИ `not sys.stdout.isatty()`:

- TUI отключается (`NullDashboard`).
- События дополнительно пишутся на stdout (по строке на event) — для CI можно `whilly --headless | jq …`.

## Consequences

### Positive

- `jq` + grep — единственные инструменты нужны для post-hoc analysis.
- Workshop participants видят живой stream когда run в headless.
- Schema можно расширять (gap pack добавил 4 новых event без поломки старого).
- Test friendly — assert на содержимое JSONL легко.

### Negative

- На сотнях тысяч событий файл растёт — нет авто-rotate (будет добавлено при необходимости).
- Не индексируется для random access — только sequential.
- Нет схемы валидации (можно добавить через `pydantic` опционально).

### Neutral

- Можно подключить collector (Vector, Fluentd) без изменения emitter — просто tail JSONL.

## References

- `whilly/cli.py::_log_event`, `_emit_json`.
- ADR-006, ADR-007, ADR-008 — все вводят новые JSONL event types.
