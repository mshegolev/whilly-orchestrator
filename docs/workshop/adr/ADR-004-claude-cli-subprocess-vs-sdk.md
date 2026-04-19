# ADR-004 — Claude CLI subprocess instead of Anthropic SDK

- **Status:** accepted
- **Date:** 2026-04-19 (retroactive)
- **Deciders:** project author
- **Domain:** agent backend integration

## Context

Whilly запускает Claude как агента. У нас есть выбор:

1. Использовать Anthropic Python SDK напрямую (`anthropic.Anthropic().messages.create(...)`).
2. Запускать Claude CLI (`claude -p "prompt"`) как subprocess и парсить JSON вывод.

Контекст требует:

- **Полноценный coding agent** с tools (Read, Edit, Bash, Glob, Grep) — не просто chat completion.
- **Permission model** для опасных операций (Bash, Edit) — либо TTY-prompts, либо `--dangerously-skip-permissions`.
- **Cost / token tracking** на каждый run.
- **Совместимость с user-side Claude Code конфигурацией** (CLAUDE.md, hooks, MCP servers).

## Decision

**Используем Claude CLI как subprocess** (`claude --output-format json -p <prompt>`). НЕ интегрируемся с Anthropic SDK напрямую.

Бинарник CLI ищется как `claude` в PATH или через override `CLAUDE_BIN`.

## Considered alternatives

### Anthropic SDK (`anthropic` Python package)

- ✅ Прямой API контроль, тонкие настройки модели.
- ✅ Стриминг responses доступен.
- ❌ **Нет встроенного coding agent loop** — нужно реализовывать tools (Read/Edit/Bash) самим.
- ❌ Нужно управлять permission model самостоятельно.
- ❌ Не использует пользовательский CLAUDE.md / hooks / MCP servers.
- ❌ Дублируем то, что уже есть в Claude CLI.

### Claude CLI subprocess (выбрано)

- ✅ Coding agent loop "из коробки" — Claude CLI уже умеет читать/писать файлы, запускать bash, использовать MCP.
- ✅ Учитывает пользовательский `~/.claude/CLAUDE.md`, settings, hooks, slash commands.
- ✅ `--output-format json` даёт стабильный machine-readable summary с `total_cost_usd`, `usage`, `result`.
- ✅ `<promise>COMPLETE</promise>` маркер в результате — простой контракт «агент закрыл задачу».
- ❌ Subprocess overhead (~50ms старт CLI).
- ❌ Зависим от того, что у пользователя установлен и настроен Claude CLI.
- ❌ Streaming output сложнее (используем file-based лог).

### Codex CLI / Aider / другие

- Не на уровне Claude по качеству для нашей целевой аудитории — отложено в ADR следующего цикла.

## Decision details

- **Сборка команды:** `claude [permission_args] --output-format json --model <model> -p <prompt>`.
- **Permission args:** по умолчанию `--dangerously-skip-permissions` (полный non-interactive). `WHILLY_CLAUDE_SAFE=1` → `--permission-mode acceptEdits` (interactive — для локальной отладки с TTY).
- **Output parsing:** `_parse_claude_output(raw)` извлекает `result`, `total_cost_usd`, `usage` из JSON.
- **Completion signal:** строка `<promise>COMPLETE</promise>` в `result_text` → `is_complete=True`. Prompt builder инструктирует агента эту строку выдавать.
- **Retry logic:** на API errors (403/500/529) — exp.backoff в `cli.py`. На auth errors — fail-fast.
- **Model id:** конфигурируется через `WHILLY_MODEL` (default `claude-opus-4-6[1m]`).

## Consequences

### Positive

- Whilly наследует все возможности Claude CLI без переизобретения.
- Пользователь может настраивать поведение агента через `CLAUDE.md` в репо — workshop-friendly.
- MCP servers работают автоматически.
- Можно скриптить "просто запусти агента вручную" (без whilly) той же командой — debugging easy.

### Negative

- Тяжелее юнит-тестировать (нужно мокать subprocess).
- Зависим от стабильности JSON-формата CLI (формат менялся между версиями — мы парсим defensively).
- Нет доступа к streaming (но есть к файловому логу — `dashboard l` hotkey).

### Neutral

- В будущем добавить SDK-backend как опцию — возможно, но не приоритет.

## References

- `whilly/agent_runner.py`.
- ADR-007 — PR sink тоже использует CLI (`gh`) по тем же причинам.
