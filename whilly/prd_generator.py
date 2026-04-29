"""PRD & Task Plan generator for Whilly orchestrator.

Two workflows:
1. ``generate_prd(description)`` — Creates a PRD markdown file from a brief project description
2. ``generate_tasks(prd_path)`` — Creates tasks.json from an existing PRD

Both use Claude CLI agent to produce structured output.

Usage:
    from whilly.prd_generator import generate_prd, generate_tasks

    prd_path = generate_prd("CLI tool для автоматизации QA процессов")
    tasks_path = generate_tasks(prd_path)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from whilly.adapters.runner import proxy

log = logging.getLogger("whilly.prd")

_PRD_SYSTEM_PROMPT = """\
Ты — Senior Product Manager и Technical Architect.

Создай PRD (Product Requirements Document) на основе описания проекта.

Формат PRD (markdown):

# PRD: {Название}

| Поле | Значение |
|------|----------|
| Автор | {автор} |
| Дата | {дата} |
| Статус | Draft |

## 1. Контекст и Мотивация
Какую проблему решаем, текущее состояние, боли.

## 2. Целевая аудитория
Таблица: Роль | Что использует | Частота

## 3. User Stories
Таблица: # | User Story | Фаза

## 4. Функциональные требования
Для каждой фичи: ID, Требование, Acceptance Criteria (таблица).
Группировать по фазам (Phase 1, 2, 3).

## 5. Не-цели
Что НЕ входит в scope.

## 6. Архитектура
Структура файлов, зависимости.

## 7. Фазы реализации
Phase 1 (MVP), Phase 2, Phase 3 с effort оценками.

## 8. Метрики успеха
Таблица: Метрика | Текущее | Цель

## 9. Тестирование
Команды для проверки.

## 10. Зависимости
Пакеты, версии, статус.

## 11. Risks & Mitigations
Таблица: Риск | Вероятность | Влияние | Mitigation

ВАЖНО:
- Пиши на русском (технические термины на английском)
- Будь конкретным — ID функциональных требований (F1.1, F1.2, ...)
- Acceptance Criteria должны быть проверяемыми
- Каждая фаза — 1-2 недели работы
- Выдай ТОЛЬКО markdown, без пояснений
"""

_TASKS_SYSTEM_PROMPT = """\
Ты — Technical Project Manager, создающий executable task plan из PRD.

На вход — PRD документ. Создай JSON файл с задачами для whilly.py orchestrator.

Формат JSON:
{
  "project": "Название проекта",
  "prd_file": "путь к PRD файлу",
  "agent_instructions": {
    "before_start": ["Прочитай PRD ...", "Проверь зависимости ..."],
    "during_work": ["Следуй интерфейсам ...", "Делай коммиты ..."],
    "before_finish": ["Запусти lint/test ...", "Отметь задачу done ..."]
  },
  "tasks": [
    {
      "id": "TASK-001",
      "phase": "Phase 1",
      "category": "functional",
      "priority": "critical",
      "description": "Что сделать",
      "status": "pending",
      "dependencies": [],
      "key_files": ["file1.py", "file2.py"],
      "acceptance_criteria": ["AC 1", "AC 2"],
      "test_steps": ["ruff check ...", "pytest ..."],
      "prd_requirement": "F1.1"
    }
  ]
}

ПРАВИЛА:
1. Каждый функциональный req из PRD → 1-2 задачи
2. ID: TASK-001, TASK-002, ... (последовательно)
3. Phase из PRD → task.phase
4. priority: critical (блокирует production), high, medium, low
5. category: functional, test, docs, infra, refactor
6. dependencies: задачи, которые должны быть done до начала этой
7. key_files: файлы, которые будет менять агент
8. acceptance_criteria: 2-5 проверяемых критериев
9. test_steps: команды для проверки (ruff, pytest, curl, etc.)
10. prd_requirement: ID из PRD (F1.1, F2.3, etc.)

ВАЖНО:
- Порядок задач = порядок выполнения (с учётом зависимостей)
- Первая задача — всегда setup/config (без зависимостей)
- Последняя задача — integration test / smoke test
- Для каждой Phase добавь финальную задачу "Phase N Integration Test"
- Задачи должны быть атомарными (1 агент = 1 задача за 5-15 минут)

## РЕЖИМ ВЫВОДА (КРИТИЧНО)

ЗАПРЕЩЕНО использовать tools: Write, Edit, MultiEdit, Bash, NotebookEdit.
НЕ ПЫТАЙСЯ сохранить результат в файл — файл создаст вызывающий код сам.

Твоя ЕДИНСТВЕННАЯ задача — **напечатать готовый JSON прямо в ответ** (stdout).
Никаких пояснений, markdown fences, комментариев, summary — **только чистый JSON**,
начиная с `{` и заканчивая `}`. Первый символ ответа = `{`.
"""


def generate_prd(
    description: str,
    output_dir: str = "docs",
    model: str = "claude-opus-4-6[1m]",
    author: str = "",
    slug: str | None = None,
) -> Path:
    """Generate a PRD markdown file from a project description.

    Args:
        description: Brief project description (1-5 sentences).
        output_dir: Directory for output file.
        model: Claude model to use.
        author: Author name for PRD header.
        slug: Optional explicit slug to use as the PRD filename
            (``PRD-<slug>.md``). When ``None`` (legacy behaviour) — the
            slug is auto-derived from the first ~50 chars of
            ``description``. The new ``whilly init`` flow (TASK-104a-3)
            passes an explicit slug so the filename matches the
            ``plan_id`` that gets imported into Postgres — slug ownership
            lives in the CLI per PRD docs/PRD-v41-prd-wizard-port.md FR-3.

    Returns:
        Path to generated PRD file.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if slug is None:
        # Legacy behaviour preserved verbatim — first ~50 chars of the
        # description, alnum/_/- only. Existing callers (the v3
        # whilly --init flow via cli_legacy) keep working unchanged.
        slug = description.strip()[:50].replace(" ", "-").replace("/", "-")
        slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    filename = f"PRD-{slug}.md"
    out_path = out_dir / filename

    prompt = (
        f"{_PRD_SYSTEM_PROMPT}\n\n"
        f"Описание проекта:\n{description}\n\n"
        f"Автор: {author or 'QA Team'}\n"
        f"Рабочая директория: {Path.cwd()}\n"
    )

    log.info("Generating PRD: %s", filename)
    content = _call_claude(prompt, model)

    if not content:
        raise RuntimeError("Claude returned empty response for PRD generation")

    # Strip markdown fences if present
    if content.startswith("```markdown"):
        content = content[len("```markdown") :].strip()
    if content.startswith("```"):
        content = content[3:].strip()
    if content.endswith("```"):
        content = content[:-3].strip()

    out_path.write_text(content, encoding="utf-8")
    log.info("PRD saved: %s (%d bytes)", out_path, len(content))
    return out_path


def _build_tasks_payload(
    prd_path: Path,
    model: str,
    raw_dump_path: Path | None = None,
) -> dict:
    """Read PRD, call Claude, validate the JSON-tasks response — pure data flow.

    Extracted from :func:`generate_tasks` (TASK-104a-1) so the same
    pipeline can power both the v3 ``generate_tasks`` (writes
    ``<slug>_tasks.json``) and the v4 ``generate_tasks_dict`` (returns
    in-memory dict for direct ``import_plan_dict`` consumption).

    Args:
        prd_path: PRD markdown file. Must exist.
        model: Claude model to ask.
        raw_dump_path: Optional path where raw Claude output is saved if
            JSON parsing fails — gives the operator a forensics trail
            without making the caller manage it. Pass ``None`` to skip.

    Returns:
        Validated dict with at least keys ``project`` (str) and ``tasks``
        (list[dict]). Each task gets default values for ``status``,
        ``dependencies``, ``key_files``, ``acceptance_criteria``,
        ``test_steps`` and an auto-generated ``id`` if missing.

    Raises:
        FileNotFoundError: PRD file missing.
        RuntimeError: Claude returned empty response, output isn't valid
            JSON (even after ``json_repair`` fallback), or no tasks were
            generated.
    """
    if not prd_path.exists():
        raise FileNotFoundError(f"PRD not found: {prd_path}")

    prd_content = prd_path.read_text(encoding="utf-8")
    prompt = f"{_TASKS_SYSTEM_PROMPT}\n\nPRD файл: {prd_path}\n\nСодержимое PRD:\n```\n{prd_content}\n```\n"

    log.info("Generating tasks from PRD: %s", prd_path.name)
    content = _call_claude(prompt, model)

    if not content:
        raise RuntimeError("Claude returned empty response for task generation")

    content = content.strip()
    if content.startswith("```json"):
        content = content[len("```json") :].strip()
    if content.startswith("```"):
        content = content[3:].strip()
    if content.endswith("```"):
        content = content[:-3].strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        try:
            import json_repair

            data = json_repair.loads(content)
        except Exception:
            if raw_dump_path is not None:
                raw_dump_path.write_text(content, encoding="utf-8")
                raise RuntimeError(f"Invalid JSON from Claude. Raw saved to {raw_dump_path}") from None
            raise RuntimeError("Invalid JSON from Claude (no raw_dump_path provided)") from None

    tasks = data.get("tasks", [])
    if not tasks:
        raise RuntimeError("No tasks generated from PRD")

    for i, task in enumerate(tasks):
        task.setdefault("status", "pending")
        task.setdefault("dependencies", [])
        task.setdefault("key_files", [])
        task.setdefault("acceptance_criteria", [])
        task.setdefault("test_steps", [])
        if "id" not in task:
            task["id"] = f"TASK-{i + 1:03d}"

    return data


def generate_tasks(
    prd_path: str | Path,
    output_dir: str = ".planning",
    model: str = "claude-opus-4-6[1m]",
) -> Path:
    """Generate tasks.json from a PRD file (v3 file-based flow).

    Args:
        prd_path: Path to PRD markdown file.
        output_dir: Directory for output file.
        model: Claude model to use.

    Returns:
        Path to generated tasks.json file.
    """
    prd_file = Path(prd_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = prd_file.stem.lower().replace("prd-", "").replace("prd_", "")
    out_path = out_dir / f"{stem}_tasks.json"

    data = _build_tasks_payload(prd_file, model, raw_dump_path=out_path.with_suffix(".raw.txt"))

    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Tasks saved: %s (%d tasks)", out_path, len(data["tasks"]))
    return out_path


def generate_tasks_dict(
    prd_path: str | Path,
    plan_id: str,
    model: str = "claude-opus-4-6[1m]",
) -> dict:
    """Generate tasks payload from a PRD file — return dict, no disk write.

    v4 entry point (TASK-104a-1, PRD docs/PRD-v41-prd-wizard-port.md FR-4).
    The returned dict is the same shape as what ``generate_tasks`` writes
    to ``<slug>_tasks.json``, ready for direct consumption by
    :func:`whilly.adapters.filesystem.plan_io.import_plan_dict`. Skipping
    the disk round-trip means ``whilly init`` never has to materialise a
    ``tasks.json`` only to read it back — the wizard goes straight from
    PRD → Postgres.

    The ``plan_id`` is set explicitly on the returned dict (Claude isn't
    asked to pick one — the CLI owns slug derivation per FR-3).

    Args:
        prd_path: Path to PRD markdown file. Must exist.
        plan_id: Plan id to stamp into the payload's ``plan_id`` key.
        model: Claude model to use.

    Returns:
        Dict with keys ``project`` (str), ``plan_id`` (str), and
        ``tasks`` (list[dict] with all defaults applied).

    Raises:
        Same as :func:`_build_tasks_payload`.
    """
    # TASK-109 follow-up: real-world run on 2026-04-29 surfaced a
    # "Invalid JSON from Claude" with no forensics file — debugging
    # impossible. Save raw next to the PRD so the operator can see
    # what Claude actually returned and either patch the prompt or
    # paste the JSON in by hand. The path mirrors what
    # ``generate_tasks`` already does for the v3 file flow.
    prd_p = Path(prd_path)
    raw_dump = prd_p.with_suffix(".tasks.raw.txt")
    data = _build_tasks_payload(prd_p, model, raw_dump_path=raw_dump)
    data["plan_id"] = plan_id
    log.info("Tasks payload built: plan_id=%s tasks=%d", plan_id, len(data["tasks"]))
    # Happy path: remove the forensics file so a successful run leaves
    # no trash on disk. Only kept if _build_tasks_payload raised.
    if raw_dump.exists():
        raw_dump.unlink()
    return data


def _call_claude(prompt: str, model: str) -> str:
    """Call Claude CLI with a prompt and return the response text.

    Стримит stderr в реальном времени (видно прогресс/ошибки claude),
    stdout буферизуется и возвращается как строка. Heartbeat каждые 30с.

    Timeout настраивается через WHILLY_CLAUDE_TIMEOUT (default 1800s / 30 мин).
    """
    # --disallowedTools: запрещаем file-writing tools, иначе claude в -p режиме
    # пытается сохранить JSON через Write и вместо stdout-ответа печатает
    # "couldn't save — permissions blocked". Нам нужен чистый stdout.
    #
    # CLAUDE_BIN override (TASK-104a-5): тесты подменяют claude на shell-stub
    # (tests/fixtures/fake_claude_prd.sh) тем же способом, что и worker
    # runner делает в whilly/adapters/runner/claude_cli.py — через env var.
    # Default "claude" сохраняет v3 поведение для всех остальных вызовов.
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    cmd = [
        claude_bin,
        "--model",
        model,
        "--disallowedTools",
        "Write,Edit,MultiEdit,NotebookEdit,Bash",
        "-p",
        prompt,
    ]

    timeout = int(os.environ.get("WHILLY_CLAUDE_TIMEOUT", "1800"))
    log.info("Calling Claude CLI (model=%s, prompt=%d chars, timeout=%ds)...", model, len(prompt), timeout)
    t0 = time.time()

    # TASK-109-3: inject HTTPS_PROXY/NO_PROXY into the spawned env only.
    # The PRD-wizard caller (whilly init) holds Postgres / httpx
    # connections in this same parent process when the import phase
    # runs; we mustn't route those through the Claude proxy.
    child_env = proxy.spawn_env_for_claude()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )
    except FileNotFoundError:
        log.error("Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")
        return ""

    log.info("Claude CLI запущен (pid=%d), жду ответ... (timeout=%ds)", proc.pid, timeout)

    # Heartbeat — логи каждые 10с пока claude работает
    stop_hb = threading.Event()

    def heartbeat() -> None:
        while not stop_hb.wait(10):
            elapsed = time.time() - t0
            log.info("  ⏳ claude работает... %.0fs / %ds (pid=%d)", elapsed, timeout, proc.pid)
            sys.stderr.flush()

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    # Stream stderr live, collect stdout
    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            stderr_lines.append(line)
            log.info("[claude stderr] %s", line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_hb.set()
        proc.kill()
        proc.wait()
        log.error("Claude CLI timeout (%ds). Last stderr:", timeout)
        for line in stderr_lines[-10:]:
            log.error("  %s", line)
        return ""
    finally:
        stop_hb.set()
        stderr_thread.join(timeout=2)

    elapsed = time.time() - t0
    log.info(
        "Claude exit=%d in %.1fs (stdout=%d chars, stderr=%d lines)",
        proc.returncode,
        elapsed,
        len(stdout),
        len(stderr_lines),
    )

    if proc.returncode != 0:
        log.error("Claude CLI error exit=%d. Stderr tail:", proc.returncode)
        for line in stderr_lines[-10:]:
            log.error("  %s", line)
        return ""

    if not stdout.strip():
        log.warning("Claude returned empty stdout. Stderr tail:")
        for line in stderr_lines[-10:]:
            log.warning("  %s", line)

    return stdout.strip()
