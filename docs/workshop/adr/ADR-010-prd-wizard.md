# ADR-010 — PRD wizard pipeline

- **Status:** accepted (retrospective)
- **Date:** 2026-04-19 (retroactive)
- **Deciders:** project author
- **Domain:** workflow

## Context

Часть пользователей приходит к whilly без готового `tasks.json` — у них есть только идея фичи. Стандартный flow требует:

1. Написать PRD.
2. Превратить PRD в декомпозированный список задач.
3. Заполнить `tasks.json` со всеми полями (id, dependencies, key_files, acceptance, test_steps).

Это значимый порог входа. Workshop участник, желающий за час получить «ага-эффект», уйдёт ещё на этапе написания PRD.

## Decision

**Включаем PRD pipeline в whilly как first-class feature**:

- `whilly --prd-wizard [slug]` — interactive Claude CLI session с master prompt'ом, выдаёт `PRD-{slug}.md`.
- `whilly --plan PRD-foo.md` — non-interactive генерация `tasks.json` из существующего PRD.
- `whilly --init "описание идеи" [--plan] [--go]` — fast path: idea → PRD → tasks → optional auto-run.

## Considered alternatives

### Не включать PRD pipeline в whilly

- ✅ Меньше scope.
- ❌ Workshop friction — нужны внешние инструменты.

### Только non-interactive `--plan`

- ✅ Просто.
- ❌ Не помогает тем, у кого нет PRD.

### Полный wizard с auto-run (выбрано)

- ✅ От идеи до running агентов в одной команде.
- ✅ Workshop hour 1: `whilly --init "add /health endpoint" --plan --go` — wow-effect.
- ❌ Сложнее код.

## Decision details

- `whilly/prd_wizard.py` (372 LOC) — orchestrates Claude CLI in interactive mode, hands master prompt for PRD generation.
- `whilly/prd_generator.py` (375 LOC) — non-interactive PRD generation when no Claude TTY available.
- `whilly/prd_launcher.py` (148 LOC) — splits master prompt by sections, manages session.
- Output: `PRD-{slug}.md` in `.planning/` dir.
- `--plan PRD.md` запускает task generation через LLM, output → `{slug}_tasks.json`.

## Consequences

### Positive

- Полный pipeline: idea → PRD → tasks → execution.
- Workshop demo «from zero to running agents in 5 minutes» possible.
- Reusable: PRD format совместим с follow-up редактирования вручную.

### Negative

- 895 LOC дополнительно.
- LLM-quality зависит от модели — на слабых моделях PRD получается посредственный.
- Wizard режим требует TTY и user attention — не для headless.

### Neutral

- В будущем — поддержка других PRD форматов (template variants).

## References

- `whilly/prd_wizard.py`, `prd_generator.py`, `prd_launcher.py`.
- `--init`, `--plan`, `--prd-wizard` CLI flags.
