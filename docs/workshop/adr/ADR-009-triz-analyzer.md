# ADR-009 — TRIZ analyzer for ambiguous tasks

- **Status:** accepted (retrospective)
- **Date:** 2026-04-19 (retroactive — fixture in v3.0.0)
- **Deciders:** project author
- **Domain:** quality / planning

## Context

В реальном task board часть задач формулируется как противоречия: «нужно быстро, но безопасно», «нужно гибко, но просто». Стандартный агент в такой задаче бьётся о trade-off без понимания методологии. **TRIZ** (Theory of Inventive Problem Solving) — методология, формализующая противоречия и предлагающая 40 inventive principles для их разрешения. Идея: использовать LLM-based TRIZ-агента для предварительного анализа таких задач — дать агенту-имплементатору лучший контекст.

## Decision

**Реализуем `whilly/triz_analyzer.py`** как опциональный pre-step для задач, помеченных `category=triz` или с явным флагом `--triz`. Анализатор:

1. Извлекает противоречие (technical / physical) из задачи.
2. Применяет TRIZ matrix → возвращает 2-4 inventive principles.
3. Расширяет prompt задачи: добавляет «contradiction summary» + «suggested principles».

## Considered alternatives

### Не использовать TRIZ

- ✅ Проще.
- ❌ На задачах с явными противоречиями — снижение качества PR.

### TRIZ как обязательный шаг

- ❌ Overhead на каждую задачу.

### TRIZ только при флаге (выбрано)

- ✅ Opt-in.
- ✅ Образовательно для workshop — участники видят TRIZ в действии.
- ✅ Не блокирует основной flow.

## Decision details

- Module: `whilly/triz_analyzer.py` (390 LOC).
- API: `analyze(task: Task) -> TRIZResult` (contradictions, principles, recommended_actions).
- Cost: 1 LLM call (~ $0.005-0.02 per task).
- Trigger: `category="triz"` в task ИЛИ CLI флаг `--triz` для всего plan.
- Output: расширенный prompt в `build_task_prompt()`.

## Consequences

### Positive

- Workshop hour 5 demo: TRIZ применяется к задаче «сделать API быстрым и безопасным» — видим 2-4 принципа, лучший PR.
- Образовательный эффект.
- Не блокирует обычный flow.

### Negative

- Дополнительный cost.
- Зависит от качества TRIZ knowledge в LLM.
- Не всегда применимо — TRIZ-агент может выдать generic советы.

### Neutral

- Можно расширять matrix evidence (custom principles per project).

## References

- `whilly/triz_analyzer.py`.
- TRIZ background: G.S. Altshuller, "And Suddenly the Inventor Appeared".
