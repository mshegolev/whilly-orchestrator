# ADR-011 — Mid-run task decomposer

- **Status:** accepted (retrospective)
- **Date:** 2026-04-19 (retroactive)
- **Deciders:** project author
- **Domain:** workflow

## Context

Часть задач в `tasks.json` оказываются "слишком большими" — `description` 500+ символов, 5+ acceptance criteria, hours-long expected work. Агент в такой задаче либо:

- Долго работает и тратит много токенов.
- Делает половину работы и помечает done преждевременно.
- Падает с timeout.

Хотелось бы разбивать такие задачи **во время выполнения** — при обнаружении oversized task разделить её на 2-4 подзадачи, добавить в plan, и продолжить.

## Decision

**Реализуем `whilly/decomposer.py`** — модуль, активирующийся раз в `WHILLY_DECOMPOSE_EVERY` итераций. Если в pending есть task с heuristic = oversized:

1. LLM-вызов: «Разбей задачу X на 2-4 подзадачи».
2. Подзадачи добавляются в `tasks.json` с `dependencies = [X]` или заменяют X.
3. Loop переходит к следующей итерации с обновлённым plan.

## Considered alternatives

### Не декомпозировать (полагаемся на пользователя)

- ✅ Простой control flow.
- ❌ Пользователи часто пишут крупные задачи.
- ❌ Падают runs из-за timeout.

### Декомпозировать при load (один раз)

- ✅ Проще.
- ❌ Не реагирует на новые задачи добавленные mid-run (от source adapter polling).

### Mid-run decomposer (выбрано)

- ✅ Адаптивно.
- ✅ Работает с новыми задачами от GH source.
- ❌ Дополнительный LLM cost.

## Decision details

- Heuristic для oversized: `description > 500 chars` OR `acceptance_criteria > 5` OR `key_files > 8`.
- Trigger: каждые `WHILLY_DECOMPOSE_EVERY` (default 5) итераций главного loop.
- LLM prompt: получает task, возвращает JSON list of subtasks.
- Subtasks merge: добавляются в `tasks.json` через `TaskManager.append`, parent task переходит в `skipped` со ссылкой на children.
- Cost: 1 LLM call per decomposition (~$0.01-0.05).

## Consequences

### Positive

- Большие задачи не блокируют loop.
- Адаптивный — реагирует на новые задачи (source pulls).
- Workshop edge case demo.

### Negative

- LLM-качество decomposition варьируется.
- Дополнительный cost на каждые N итераций.
- Возможен infinite decomposition loop если decomposer выдаёт только oversized subtasks — guarded by `MAX_DECOMPOSE_DEPTH=3`.

### Neutral

- Можно расширять heuristics (e.g., factor in cost history).

## References

- `whilly/decomposer.py`.
- `WHILLY_DECOMPOSE_EVERY`, `WHILLY_DECOMPOSE_THRESHOLD` env vars.
