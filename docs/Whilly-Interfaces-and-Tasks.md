---
title: Interfaces & Tasks
nav_order: 5
---

# Whilly v2: Интерфейсы, стек, задачи

## 1. Технический стек

### Что уже есть и оставляем

| Компонент | Пакет | Версия | Зачем |
|-----------|-------|--------|-------|
| TUI | `rich` | >=13.0 | Dashboard, Live display, Tables, Progress |
| Config | `dataclasses` | stdlib | WhillyConfig |
| Task state | `json` + atomic write | stdlib | TaskManager |
| Process mgmt | `subprocess` | stdlib | Agent launch |
| Parallel isolation | `tmux` | 3.6a (system) | Per-agent sessions |
| Logging | `logging` | stdlib | whilly logger |

### Что добавляем

| Компонент | Решение | Почему |
|-----------|---------|--------|
| Keyboard input | `threading.Thread` + `sys.stdin` | Без stty хаков, non-blocking |
| Voice alerts | `subprocess.run(["say", ...])` | Нативный macOS, 0 зависимостей |
| JSON repair (orchestrator) | `json-repair` | Уже в requirements.txt, для LLM JSON |

### Что НЕ добавляем

- `textual` — overkill для нашего TUI, `rich.Live` достаточно
- `libtmux` — `subprocess` + tmux CLI проще и уже работает
- `keyboard` — требует root на macOS, `threading` + stdin проще
- `asyncio` — threading достаточно для наших 3-5 параллельных процессов
- `curses` — Rich абстрагирует терминал лучше

---

## 2. Интерфейсные контракты

### 2.1 Новый модуль: `whilly/decomposer.py`

```python
"""Task decomposition — анализ pending задач и split через LLM."""

from whilly.task_manager import TaskManager
from whilly.agent_runner import AgentResult

def needs_decompose(tm: TaskManager) -> bool:
    """Эвристика: есть ли задачи, требующие декомпозиции.

    Критерии:
    - 6+ acceptance_criteria
    - description содержит 2+ " и " или 1+ " + "

    Returns: True если хотя бы одна pending задача подходит.
    """

def build_decompose_prompt(tasks_file: str) -> str:
    """Промпт для LLM-агента декомпозиции.

    Инструкции агенту:
    - Анализировать pending задачи по критериям
    - Разбить крупные на 2-5 подзадач (TASK-XXXa, TASK-XXXb)
    - НЕ трогать done/in_progress/failed
    - Обновить dependencies
    - Вернуть <promise>DECOMPOSED N</promise> или <promise>NO_DECOMPOSE</promise>
    """

def run_decompose(
    tm: TaskManager,
    agent_model: str,
    use_tmux: bool,
    log_dir: Path,
) -> int:
    """Запустить LLM декомпозицию.

    Returns: количество добавленных задач (0 если без изменений).
    Side effects: модифицирует tasks JSON файл.
    """

# Cache — не повторять decompose если задачи не изменились
_last_decompose_hash: str = ""

def _tasks_hash(tm: TaskManager) -> str:
    """SHA256 от pending task IDs + descriptions. Для cache."""
```

### 2.2 Новый модуль: `whilly/notifications.py`

```python
"""macOS voice notifications via `say` command."""

import shutil
import subprocess
import logging

log = logging.getLogger("whilly")

SAY_BIN: str | None = shutil.which("say")
VOICE = "Milena"  # Russian voice
ENABLED = True  # Overridden by WHILLY_VOICE=0

def notify(text: str) -> None:
    """Произнести текст через macOS say. Noop если недоступно."""

# Convenience shortcuts
def notify_decompose(count: int) -> None:
    """'Декомпозиция: добавлено N задач.'"""

def notify_task_done() -> None:
    """'Задача готова. Продолжаю работу.'"""

def notify_plan_done() -> None:
    """'План завершён!'"""

def notify_all_done() -> None:
    """'Хозяин, я всё сделалъ!'"""
```

### 2.3 Обновление: `whilly/dashboard.py` — добавить keyboard handler

```python
# Новый класс в dashboard.py

class KeyboardHandler:
    """Non-blocking keyboard input через threading."""

    def __init__(self, dashboard: Dashboard):
        self._dashboard = dashboard
        self._thread: threading.Thread | None = None
        self._running = False
        self._callback: dict[str, Callable] = {}

    def register(self, key: str, callback: Callable) -> None:
        """Зарегистрировать callback для клавиши."""

    def start(self) -> None:
        """Запустить listener thread."""

    def stop(self) -> None:
        """Остановить listener thread."""

    def _listen_loop(self) -> None:
        """Внутренний цикл чтения stdin в отдельном thread."""

# Overlay views (возвращают текст для отображения)

def render_task_detail(tm: TaskManager, task_id: str) -> str:
    """Полная информация о задаче: description, AC, test_steps, deps."""

def render_log_view(log_file: Path, lines: int = 30) -> str:
    """Последние N строк лога."""

def render_all_tasks(tm: TaskManager) -> str:
    """Таблица всех задач с иконками статуса."""

def render_help(config: WhillyConfig) -> str:
    """Hotkeys + configuration + file paths."""
```

### 2.4 Обновление: `whilly/config.py` — новые поля

```python
@dataclass
class WhillyConfig:
    # Существующие
    MAX_ITERATIONS: int = 0
    MAX_PARALLEL: int = 3
    HEARTBEAT_INTERVAL: int = 1
    DECOMPOSE_EVERY: int = 5
    AGENT: str = ""
    USE_TMUX: bool = True
    LOG_DIR: str = "whilly_logs"
    MODEL: str = "claude-opus-4-6[1m]"

    # Новые
    VOICE: bool = True              # F5: voice notifications
    ORCHESTRATOR: str = "file"      # F4: "file" | "llm"
    RICH_DASHBOARD: bool = True     # F1: use Rich Live vs ANSI fallback
```

### 2.5 Обновление: `whilly/orchestrator.py` — LLM режим

```python
# Добавить к существующему

def plan_batches_llm(
    ready_tasks: list[Task],
    max_parallel: int,
    tasks_file: str,
    agent_model: str,
) -> list[list[Task]]:
    """LLM-based orchestration с fallback на file-based.

    1. Формирует промпт с ready tasks
    2. Запускает agent
    3. Парсит JSON ответ (с json-repair для robustness)
    4. Валидирует task IDs
    5. При ошибке — fallback на plan_batches()
    """

def build_orchestrator_prompt(ready_tasks: list[Task], max_parallel: int) -> str:
    """Промпт для LLM orchestrator."""

def build_interface_agreement_prompt(module: str, task_ids: list[str], tasks_file: str) -> str:
    """Промпт для interface agreement между parallel задачами."""

def run_interface_agreement(
    module: str,
    task_ids: list[str],
    tasks_file: str,
    agent_model: str,
    log_dir: Path,
) -> None:
    """Запустить LLM для определения интерфейсного контракта.
    Результат сохраняется в .planning/interfaces/{module}_contract.md
    """
```

### 2.6 Обновление: `whilly.py` — интеграция новых модулей

```python
# Новые импорты
from whilly.decomposer import needs_decompose, run_decompose
from whilly.notifications import notify_task_done, notify_plan_done, notify_all_done
from whilly.dashboard import Dashboard, KeyboardHandler

# Изменения в run_plan():
# 1. Перед main loop: initial decompose check
# 2. В main loop: periodic decompose (DECOMPOSE_EVERY)
# 3. После batch: voice notification
# 4. Rich Dashboard вместо ANSI fallback (если RICH_DASHBOARD=True)
# 5. KeyboardHandler для hotkeys
```

---

## 3. Граф зависимостей (после изменений)

```
whilly.py
├── whilly.config           (standalone)
├── whilly.task_manager     (standalone)
├── whilly.agent_runner     (standalone)
├── whilly.tmux_runner      (standalone)
├── whilly.orchestrator     → agent_runner (для LLM mode)
│                          → task_manager (для Task type)
├── whilly.reporter         (standalone)
├── whilly.dashboard        → reporter (CostTotals, fmt_*)
│                          → task_manager (TaskManager, PRIORITY_ORDER)
│                          → config (WhillyConfig, для help view)
│                          → rich (external)
├── whilly.decomposer  NEW  → task_manager (TaskManager)
│                          → agent_runner (run_agent / run_agent_async)
└── whilly.notifications NEW (standalone, subprocess only)
```

---

## 4. Декомпозиция на задачи

### Phase 1: Rich Dashboard + Hotkeys

| ID | Описание | Приоритет | Зависимости | key_files | AC |
|----|---------|-----------|------------|-----------|------|
| R2-001 | Подключить Rich Dashboard к main loop | critical | - | `whilly.py`, `whilly/dashboard.py` | Dashboard.start()/stop() вызываются; ANSI fallback класс удалён; screen=True работает |
| R2-002 | Keyboard handler: threading + stdin listener | critical | R2-001 | `whilly/dashboard.py` | KeyboardHandler class; non-blocking read; start/stop lifecycle |
| R2-003 | Hotkey `q`: graceful shutdown | high | R2-002 | `whilly/dashboard.py`, `whilly.py` | Kill tmux sessions; save report; предложить Resume/Exit; terminal restored |
| R2-004 | Hotkey `d`: task detail overlay | high | R2-002 | `whilly/dashboard.py` | Ввод task ID; показ description + AC + test_steps + deps + status; dismiss by any key |
| R2-005 | Hotkey `l`: log viewer overlay | high | R2-002 | `whilly/dashboard.py` | Последние 30 строк whilly.log; dismiss by any key |
| R2-006 | Hotkey `t`: all tasks table | medium | R2-002 | `whilly/dashboard.py` | Таблица с иконками статуса; все задачи; сортировка по phase |
| R2-007 | Hotkey `h`: help screen | medium | R2-002 | `whilly/dashboard.py` | Hotkeys + config values + file paths; dismiss by any key |
| R2-008 | Spinner animation для active agents | medium | R2-001 | `whilly/dashboard.py` | Rotating `⣾⣽⣻⢿⡿⣟⣯⣷` per agent; elapsed time; log file size |
| R2-009 | Unit-тесты Phase 1: dashboard rendering | high | R2-001..008 | `tests/test_whilly_dashboard.py` | Mock TaskManager; verify render output contains sections; keyboard handler start/stop |

### Phase 2: Decomposition + Error Handling

| ID | Описание | Приоритет | Зависимости | key_files | AC |
|----|---------|-----------|------------|-----------|------|
| R2-010 | Создать `whilly/decomposer.py`: needs_decompose() | critical | - | `whilly/decomposer.py` | Эвристика: 6+ AC, 2+ " и "; returns bool; только pending задачи |
| R2-011 | decomposer: build_decompose_prompt() | critical | R2-010 | `whilly/decomposer.py` | Промпт с @tasks_file; инструкции по split; DECOMPOSED/NO_DECOMPOSE promise |
| R2-012 | decomposer: run_decompose() с кешем | critical | R2-010, R2-011 | `whilly/decomposer.py` | SHA256 cache; skip если NO_DECOMPOSE + hash не изменился; return delta count |
| R2-013 | Интеграция decomposer в main loop | high | R2-012 | `whilly.py` | Initial decompose перед loop; periodic через DECOMPOSE_EVERY; critical task trigger |
| R2-014 | Error handling: exponential backoff | high | - | `whilly.py`, `whilly/agent_runner.py` | Backoff 5→15→30→60s при API errors; sleep между retry |
| R2-015 | Error handling: global error rate limit | high | R2-014 | `whilly.py` | 5+ consecutive failed tasks → pause 60s + dashboard alert |
| R2-016 | Error handling: auth error detection (no retry) | high | R2-014 | `whilly/agent_runner.py` | 403 + "failed to authenticate" → не ретраить, mark failed immediately |
| R2-017 | Unit-тесты Phase 2: decomposer + error handling | high | R2-010..016 | `tests/test_whilly_decomposer.py`, `tests/test_whilly_error_handling.py` | needs_decompose True/False; cache skip; backoff delays; auth error detect |

### Phase 3: LLM Orchestrator + Notifications

| ID | Описание | Приоритет | Зависимости | key_files | AC |
|----|---------|-----------|------------|-----------|------|
| R2-018 | Config: добавить VOICE, ORCHESTRATOR, RICH_DASHBOARD | medium | - | `whilly/config.py` | 3 новых поля; from_env() парсит их; defaults: True, "file", True |
| R2-019 | Создать `whilly/notifications.py` | low | R2-018 | `whilly/notifications.py` | say check; 4 notify_* functions; WHILLY_VOICE=0 disable; noop если нет say |
| R2-020 | Интеграция notifications в main loop | low | R2-019 | `whilly.py` | notify_task_done после COMPLETE; notify_plan_done после loop; notify_all_done в конце |
| R2-021 | LLM orchestrator: plan_batches_llm() | medium | - | `whilly/orchestrator.py` | Промпт → agent → JSON parse (json-repair) → validate IDs → fallback на file-based |
| R2-022 | LLM orchestrator: interface agreement | medium | R2-021 | `whilly/orchestrator.py` | detect_module_overlap → build prompt → run agent → save to .planning/interfaces/ |
| R2-023 | Интеграция LLM orchestrator (ORCHESTRATOR=llm) | medium | R2-021, R2-022 | `whilly.py` | Switch по config; по умолчанию file; при llm — fallback на file при ошибке |
| R2-024 | Unit-тесты Phase 3: notifications + LLM orch | medium | R2-019..023 | `tests/test_whilly_notifications.py`, `tests/test_whilly_orchestrator.py` | notify mock; LLM parse; fallback; interface agreement file creation |

### Phase 4: Logging + Polish + Cleanup

| ID | Описание | Приоритет | Зависимости | key_files | AC |
|----|---------|-----------|------------|-----------|------|
| R2-025 | Structured JSON logging (jsonl) | medium | - | `whilly.py` | JSON lines формат; whilly_events.jsonl; key events с timestamps |
| R2-026 | Log rotation (max 10MB, 5 backups) | low | R2-025 | `whilly.py` | RotatingFileHandler; 10MB per file; 5 backup count |
| R2-027 | Удалить ANSI fallback Dashboard из whilly.py | medium | R2-001 | `whilly.py` | Класс Dashboard (простой) удалён; только Rich Dashboard |
| R2-028 | Интеграционный тест: dry run 1 iteration | high | R2-001..027 | `tests/test_whilly_integration.py` | WHILLY_MAX_ITERATIONS=1 + mock agent; verify: task transitions, report, logs |
| R2-029 | Документация: README whilly usage | low | R2-028 | `docs/Whilly-Usage.md` | CLI usage, env vars, tmux setup, troubleshooting |

---

## 5. Граф зависимостей задач

```
Phase 1 (Dashboard + Hotkeys):
  R2-001 ─┬─ R2-002 ─┬─ R2-003
           │          ├─ R2-004
           │          ├─ R2-005
           │          ├─ R2-006
           │          └─ R2-007
           └─ R2-008
  R2-009 ← R2-001..008

Phase 2 (Decompose + Errors):
  R2-010 → R2-011 → R2-012 → R2-013
  R2-014 ─┬─ R2-015
           └─ R2-016
  R2-017 ← R2-010..016

Phase 3 (LLM Orch + Voice):
  R2-018 → R2-019 → R2-020
  R2-021 → R2-022 → R2-023
  R2-024 ← R2-019..023

Phase 4 (Logging + Polish):
  R2-025 → R2-026
  R2-027 ← R2-001
  R2-028 ← all
  R2-029 ← R2-028
```

## 6. Параллелизация (для Whilly self-execution)

```
Batch 1: [R2-001, R2-010, R2-014, R2-018]  — разные файлы, независимые
Batch 2: [R2-002, R2-011, R2-015, R2-019]  — зависят от batch 1
Batch 3: [R2-003, R2-004, R2-005, R2-012, R2-016, R2-021]
Batch 4: [R2-006, R2-007, R2-008, R2-013, R2-020, R2-022]
Batch 5: [R2-009, R2-017, R2-023, R2-024]  — тесты
Batch 6: [R2-025, R2-027]
Batch 7: [R2-026, R2-028]
Batch 8: [R2-029]
```

---

## 7. Module contract: `whilly/cli/jira_watch_loop.py`

Phase-20 addition. Synchronous foreground watch-loop daemon for `whilly jira watch`.

### Exported symbols

| Symbol | Kind | Description |
|--------|------|-------------|
| `_run_jira_watch(args, *, snapshot_collector, environ, stop_event, install_signal_handlers, pause_control, dispatch_runner)` | function | Main watch loop. Resolves interval, acquires PID lock, runs `while not stop.is_set()` with interruptible sleep per-issue collector calls and optional dispatch. Returns 0 on graceful stop, 1 on single-instance refusal. |
| `_run_watch_status(args, *, environ)` | function | Reads `_status_path()` and prints human-readable status (default) or JSON (`args.json`). Verifies the recorded PID when `state=running` and reports `stale (pid N not running)` for a dead watcher. Returns EXIT_OK in found, missing-file, and unreadable-file cases. |
| `_resolve_interval(args_interval, env)` | function | Priority: `--interval` arg > `WHILLY_JIRA_WATCH_INTERVAL` env > 300 s default. |
| `_interruptible_sleep(stop, seconds)` | function | `threading.Event.wait`; returns True if stop fired. Never uses `time.sleep`. |
| `_write_status(status, status_path)` | function | Atomic tempfile + `os.replace` status file write (T-20-05 model). |
| `_acquire_pid_lock(pid_path)` | function | `os.kill(pid, 0)` liveness probe; returns True if acquired, False if live instance found. |
| `_release_pid_lock(pid_path)` | function | Unlinks PID file only if it still holds our PID. |
| `_persist_watch_event(*, dsn, issue_key, event_type, payload, repo)` | async function | Best-effort DB audit event; warn-not-fail. |
| `_read_watch_readiness(plan_path)` | function | Reads `jira_work.readiness` from plan JSON (local re-implementation; no import from `whilly.cli.jira`). |
| `_evaluate_watch_readiness(readiness_repo_path)` | function | Resolves `--readiness-repo-path`: repo directory → `probe_code_readiness`, plan JSON file → `_read_watch_readiness`; returns `None` when undeterminable (callers must fail closed). |
| `_run_dispatch_if_ready(...)` | function | Fail-closed readiness gate + dispatch_runner invocation; only called when `wants_dispatch=True`. Blocks with `verdict=unknown` when readiness is undeterminable unless `--allow-unready-run`. |
| `EVENT_CYCLE = "watch.cycle"` | constant | Audit event type for successful poll cycle. |
| `EVENT_FAILURE = "watch.failure"` | constant | Audit event type for failed poll cycle. |
| `EVENT_PAUSED = "watch.paused"` | constant | Audit event type when global pause gate fires. |
| `EVENT_BLOCK = "watch.block"` | constant | Audit event type when readiness gate blocks dispatch. |
| `EVENT_DISPATCH = "watch.dispatch"` | constant | Audit event type when a dispatch SUCCEEDED (rc == 0). Failed dispatch attempts emit `EVENT_FAILURE` with the real rc instead. |

### Status file schema

Path: `whilly_logs/watch/jira-watch-status.json` (honoring `WHILLY_LOG_DIR`)

```python
{
    "state": "running" | "stopped",
    "pid": int,
    "issues": list[str],
    "interval_seconds": int,
    "cycle_count": int,
    "error_count": int,
    "last_poll_at": str | None,   # ISO-8601 UTC
    "last_poll_result": str | None,  # "ok" | "error" | "partial" | "paused" | "blocked"
    "last_error": str | None,     # exception class name of the last poll failure
    "backoff_seconds": int,
    "last_dispatch_rc": int | None,  # exit code of the most recent dispatch attempt
    "dispatched": dict[str, str],    # issue key → combined_hash of last successful dispatch
    "started_at": str,            # ISO-8601 UTC
    "stopped_at": str | None,     # ISO-8601 UTC
}
```

Secret-free: no token, no DSN value, no credentials (T-20-03 / T-20-11).

### Key constraints

- No `from whilly.cli.jira` import — circular-import clean (Pitfall 5).
- `dispatch_runner` call site appears exactly once, inside `_run_dispatch_if_ready`,
  which is only called when `wants_dispatch=True` (T-20-06 / T-20-10).
- `_run_watch_status` uses `EXIT_OK` for both found and not-found paths — missing
  status file is not an error.
