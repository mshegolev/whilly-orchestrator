# Ralph Orchestrator

Python реализация техники Ralph Wiggum — continuous AI agent loops для автоматизации задач разработки через LLM.

## Модули

- `orchestrator.py` — главный цикл, координация агентов
- `agent_runner.py`, `tmux_runner.py` — запуск LLM-агентов
- `dashboard.py` — TUI/Web дашборд прогресса
- `decomposer.py`, `task_manager.py`, `state_store.py` — управление задачами
- `prd_generator.py`, `prd_wizard.py` — генерация PRD из идей
- `triz_analyzer.py` — TRIZ-анализ задач
- `history.py`, `reporter.py`, `verifier.py`, `notifications.py`, `config.py` — инфраструктура

## Установка

```bash
pip install ralph-orchestrator
```

## Использование

См. `docs/Ralph-Usage.md` в репозитории qa_assistant.
