# PRD: Whilly demo plan

> Минимальный синтетический план для демонстрации distributed-режима Whilly v4.
> Используется в `DEMO.md` (русская и английская версии).

## Цель

Показать на 4 простых задачах полный жизненный цикл:

1. `whilly plan import` → строки в `plans` / `tasks`.
2. `whilly-worker` claim'ит задачу из БД через HTTP.
3. Задача меняет статус `PENDING → CLAIMED → IN_PROGRESS → DONE`.
4. В таблице `events` оседает append-only audit log с CLAIM/COMPLETE.

## Задачи

| ID         | Цель                                       | Зависимости       |
|------------|--------------------------------------------|-------------------|
| DEMO-001   | Файл `DEMO-HELLO.md` с одной строкой        | —                 |
| DEMO-002   | Файл `NOTES.md` (заголовок + 3 bullet'а)    | —                 |
| DEMO-003   | pytest smoke `tests/test_demo_smoke.py`     | DEMO-001          |
| DEMO-004   | `SUMMARY.md` со списком из 3 пунктов        | DEMO-001, DEMO-002 |

## Acceptance

- Все 4 задачи переходят в `DONE` без ошибок.
- В `events` присутствуют CLAIM + COMPLETE на каждую задачу.
- DAG (см. `whilly plan show demo`) корректно отражает зависимости.

## Out of scope

- Реальный Claude (демо использует stub `tests/fixtures/fake_claude.sh`).
- Decision Gate / TRIZ — выключены, чтобы демо было детерминированным.
- GitHub-интеграция (`whilly forge intake`).
