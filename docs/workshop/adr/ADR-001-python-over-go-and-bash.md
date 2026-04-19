# ADR-001 — Python over Go and Bash

- **Status:** accepted
- **Date:** 2026-04-19 (retroactive — was the original choice)
- **Deciders:** project author
- **Domain:** stack choice

## Context

Когда whilly стартовал, на столе лежали три референсные реализации Ralph-loop:

- **`stepango/grkr`** — bash, ~200 LOC, минималистичный, single-agent.
- **`egv/yolo-runner`** — Go, ~30K LOC, multi-backend, production-minded, DAG, TUI.
- Свои наработки автора в Python (claudeproxy, pytest-плагины, prd-генераторы).

Целевая аудитория — Python-команды, которые делают AI-tooling и хотят расширять оркестратор под себя без обучения новому языку. Workshop-формат добавляет требование: код должен **читаться за один день**.

## Decision

**Используем Python 3.10+** как единственный язык реализации whilly. Bash остаётся только как командные хелперы (git, tmux), Go не используем.

## Considered alternatives

### Bash (как grkr)

- ✅ Минимум зависимостей, мгновенный старт.
- ✅ Близко к git/`gh`/`tmux` без оберток.
- ❌ Структурированный код > 500 LOC становится нечитаемым.
- ❌ Нет нормальных datatypes — JSON парсится через `jq`, ошибки молчат.
- ❌ Нет тест-фреймворка приличного уровня (bats работает, но это не pytest).
- ❌ Workshop-команды не пишут на bash на повседневной основе.

### Go (как yolo-runner)

- ✅ Быстро, статически типизированно, production-friendly.
- ✅ Хорошая модель concurrency (goroutines + channels).
- ✅ Single binary деплой.
- ❌ Кривая входа высокая для Python-команд.
- ❌ AI/LLM экосистема в Go догоняет Python (langchain-go, openai-go), но не паритет.
- ❌ Меньше готовых интеграций (Rich-equivalent TUI = Bubble Tea, но другой стиль).
- ❌ Workshop за 1 день на Go-кодовой базе нереалистично без предыдущего опыта Go.

### Python

- ✅ Команды AI-инженерии знают Python; самые активные SDK именно Python (anthropic, openai).
- ✅ Богатые TUI библиотеки (`rich`, `textual`).
- ✅ pytest, ruff, mypy — зрелый toolchain.
- ✅ Subprocess-управление достаточно для wrapping Claude CLI и `gh`.
- ❌ Performance ниже Go на тысячах задач — не критично для целевого scale (1-100 tasks/run).
- ❌ Деплой не single binary — но pip-install достаточен.

## Consequences

### Positive

- Workshop participants могут читать и форкать код в первый же час.
- Используем `rich` для dashboard — выглядит современно без TUI-фреймворка.
- Расширяемость через obvious модули (`whilly/sources/`, `whilly/sinks/`).
- Тесты пишутся быстро на pytest + standard mocking.

### Negative

- Не получаем "single binary" — нужно `pip install`.
- На действительно больших масштабах (1000+ параллельных tasks/sec) уперлись бы в GIL — но whilly не targeting это.
- Нужна Python 3.10+ среда.

### Neutral

- Возможен будущий `whilly-core` на Go как стрейч-цель (после стабилизации API), но не в этом цикле.

## References

- [grkr](https://github.com/stepango/grkr) — bash-вариант.
- [yolo-runner](https://github.com/egv/yolo-runner) — Go-вариант.
- BRD §8 Constraints (стек = Python).
- PRD §1.2 Module map.
