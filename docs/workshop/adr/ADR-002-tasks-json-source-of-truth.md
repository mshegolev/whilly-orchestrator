# ADR-002 — `tasks.json` as the source of truth

- **Status:** accepted
- **Date:** 2026-04-19 (retroactive)
- **Deciders:** project author
- **Domain:** state model

## Context

Loop'у нужно знать "что сделано / что pending / что in_progress". Источник этого знания должен:

1. Переживать crash (агент упал → state не теряется).
2. Атомарно обновляться (несколько агентов могут читать/писать одновременно).
3. Быть человекочитаемым (debug, ручная правка).
4. Не требовать инфраструктуры (БД, очереди).
5. Поддерживать source adapters: даже если задачи из GitHub Issues, loop работает по тому же контракту.

## Decision

**`tasks.json` — единственный source of truth task state**, во всех режимах работы. Сторонние source-адаптеры (GitHub Issues, Linear, Jira) **конвертируют** свой формат в `tasks.json` при загрузке и обновляют его при изменениях.

## Considered alternatives

### SQLite

- ✅ ACID, конкурентные чтения.
- ✅ Богатые запросы (filter by status, etc).
- ❌ Не human-readable (нужен `sqlite3` CLI).
- ❌ Лишняя зависимость для простого use case.
- ❌ Сложнее показывать diff в PR.

### Redis / NATS distributed queue

- ✅ Production-grade распределённая очередь.
- ❌ Требует инфраструктуры — workshop friction.
- ❌ Overkill для in-process supervisor с ≤ 5 агентов.
- ❌ Не human-readable.

### GitHub Issues directly (no local file)

- ✅ Single source.
- ❌ Network call per status change → latency, rate limit.
- ❌ Не работает offline.
- ❌ Нет своего поля для `key_files`/`acceptance` без парсинга markdown каждый раз.

### `tasks.json` + atomic write via `os.replace`

- ✅ Human-readable, diff-friendly.
- ✅ POSIX atomic guarantee (`os.replace` is atomic on same filesystem).
- ✅ Нулевая инфраструктура.
- ✅ Source-адаптеры могут upserting в этот же файл.
- ❌ Не масштабируется на сотни параллельных писателей — но не наш сценарий.

## Decision details

- File path: каждый план — отдельный `tasks.json` (или `<name>_tasks.json`).
- Schema: см. PRD §1.3.3.
- Atomic write: `TaskManager.save()` пишет через `tempfile.mkstemp` в той же директории + `os.replace`.
- Read: `TaskManager.reload()` перечитывает файл целиком — дёшево даже на 1000+ tasks.
- Конкурентность: loop читает после каждой fairness-точки (после batch dispatch, после deadlock check). При параллельном external-write последний выигрывает.
- Source-адаптер block в schema: `{"source": {"type": "github_issues", "repo": "...", ...}}` — позволяет re-fetch при следующем запуске.

## Consequences

### Positive

- Workshop-участник может открыть `tasks.json` в редакторе и понять состояние мгновенно.
- Рестарт после crash тривиален (plus `--resume` для дополнительного контекста).
- PR-ревью изменений в plan видны как обычный JSON diff.
- Source-адаптер — отдельная ответственность, чистая модульность.

### Negative

- Не подходит для сотен writer'ов одновременно — но это не цель.
- Все ridurre к JSON — сложные графы dependencies парсятся вручную.

### Neutral

- Будущий "DB-backed" режим возможен через слой абстракции — но не запланирован.

## References

- `whilly/task_manager.py` — реализация.
- PRD §1.3.3 — schema.
- ADR-006 — GitHub Issues source upserts в tasks.json.
