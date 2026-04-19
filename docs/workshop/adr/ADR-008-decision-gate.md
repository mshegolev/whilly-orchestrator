# ADR-008 — Decision Gate before agent dispatch

- **Status:** accepted (gap pack)
- **Date:** 2026-04-20
- **Deciders:** project author
- **Domain:** quality / cost control

## Context

Не все задачи в очереди достойны того, чтобы агент тратил на них токены. Issues со скудным описанием, без acceptance criteria, противоречивые — лучше отбросить с label `needs-clarification`, чем потратить $0.50 и получить мусорный PR.

Идея взята из `stepango/grkr`, где есть **Decision Gate** — короткий вызов, в котором агент сам решает proceed / refuse до начала имплементации.

Требования:

1. **Дёшево** — Decision Gate должен стоить < $0.01 per task (в 50× меньше типичной задачи).
2. **Быстро** — < 60 секунд.
3. **Conservative** — лучше пропустить плохую задачу, чем отбросить хорошую (false positives дороже false negatives).
4. **Fail-open** — при timeout / parse error считаем proceed (не блокируем работу).
5. **Cost учитывается** в общем budget.
6. **Workshop UX** — refuse → label flip в GH issue, чтобы было видно почему задача в backlog.

## Decision

**Реализуем `whilly/decision_gate.py`** — модуль, активируемый через `--decision-gate` (или `WHILLY_DECISION_GATE=1`).

Перед основным prompt'ом (после batch planning, до dispatch'а) — короткий LLM-вызов:

- Prompt: «Дано описание задачи. Решишь ли её или откажешься?» + structured JSON output.
- Timeout: 60 секунд.
- На `proceed` → продолжаем нормальный flow.
- На `refuse` → `task.status = "skipped"`, JSONL event, label flip в GH (если source=GH).

## Considered alternatives

### Always proceed (без gate)

- ✅ Проще.
- ❌ Тратим токены на мусорные задачи.
- ❌ Получаем шумные PR на ревью.

### Pre-filter в source adapter (rule-based)

- ✅ Дёшево, без LLM.
- ❌ Жёсткие правила пропускают edge cases.
- ❌ Не понимают семантику задачи.

### Decision Gate как отдельный коммерческий evaluator

- ❌ Vendor lock-in.

### LLM Decision Gate (выбрано)

- ✅ Дёшево если использовать дешёвую модель (Haiku) или короткий prompt.
- ✅ Понимает семантику.
- ✅ Conservative-биас prompt'ом.
- ❌ Зависим от стабильности LLM.
- ❌ Дополнительная latency на каждую задачу.

## Decision details

### Prompt template (RU primary)

```
Ты — gate-агент, проверяющий задачи перед исполнением.

Задача:
- ID: {task.id}
- Описание: {task.description}
- Acceptance: {task.acceptance_criteria or "не задано"}
- key_files: {task.key_files or "не указаны"}

Реши: брать в работу или отказаться?

Откажись если:
- описание < 20 символов или явно бессмысленно
- противоречивые требования
- нужны секреты / доступы которых нет

Возьмись если:
- описание понятно, есть хотя бы 1 acceptance criterion
- ИЛИ описание простое и однозначное (badge, README fix, version bump)

Ответ строго JSON одной строкой:
{"decision":"proceed"|"refuse","reason":"≤120 chars"}
```

### Implementation contract

```python
@dataclass
class Decision:
    decision: str           # "proceed" | "refuse"
    reason: str
    cost_usd: float

def evaluate(task: Task, model: str = None, timeout_s: int = 60) -> Decision:
    """Run decision gate for a task. Fail-open."""
```

### Hook into main loop

```
for batch in batches:
    for task in batch:
        if config.decision_gate:
            d = decision_gate.evaluate(task, model=config.model)
            log_event("decision_gate", task_id=task.id, decision=d.decision, reason=d.reason, cost_usd=d.cost_usd)
            budget.add(d.cost_usd)

            if d.decision == "refuse":
                tm.mark_status([task.id], "skipped")
                if source_is_gh:
                    gh_label_flip(task, remove="whilly:ready", add="needs-clarification", comment=d.reason)
                continue
        # ... normal dispatch
```

### Fail-open

- Timeout → log warning, treat as proceed.
- JSON parse error → log warning, treat as proceed.
- Reasoning: false-refuse раздражает и блокирует прогресс. False-proceed только тратит ~$0.50.

### Cost considerations

- Используем тот же модель что и main loop (consistency).
- Prompt ~ 200 токенов, response ~ 50 токенов → ≈ $0.005 per task на Sonnet, ≈ $0.001 на Haiku.
- Можно опционально override через `WHILLY_DECISION_GATE_MODEL=claude-haiku-4-5-20251001`.

### Workshop UX

- На refuse → `gh issue edit {N} --remove-label whilly:ready --add-label needs-clarification`.
- Опционально: `gh issue comment {N} --body "Whilly Decision Gate refused: {reason}"` (controlled by `--decision-gate-comment` flag).

## Consequences

### Positive

- Cost saving — не тратим главный prompt на заведомо плохие задачи.
- Cleaner PR queue — мусорные задачи не доходят до агента.
- Workshop demo: показываем агенту 1 «плохую» задачу, видим refuse + label flip.
- JSONL event — измеримый KPI «decision gate refusal rate».

### Negative

- Дополнительные ~$0.001-0.005 на каждую задачу (даже на хорошие).
- Latency +1-5 sec на task.
- Conservative prompt может пропускать edge cases — нужна периодическая проверка accuracy.
- Зависим от стабильности модели.

### Neutral

- В будущем — A/B test prompt formulations для точности.
- Можно сделать opt-in только для GH source (config: `--decision-gate=gh-only`) — отложено.

## References

- `whilly/decision_gate.py` (gap pack).
- PRD §2.2.3 FR-DG-1 to FR-DG-6.
- Источник идеи: `stepango/grkr` README.
- ADR-006 — label flip требует GH source.
