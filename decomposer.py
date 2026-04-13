"""Task decomposition — анализ pending задач и split через LLM."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from ralph.task_manager import TaskManager

log = logging.getLogger("ralph")


def needs_decompose(tm: TaskManager) -> bool:
    """Check if any pending task needs decomposition.

    Criteria (only pending tasks):
    - 6+ acceptance_criteria
    - description contains 2+ " и "
    - description contains 1+ " + "
    """
    for task in tm.tasks:
        if task.status != "pending":
            continue
        if len(task.acceptance_criteria) >= 6:
            return True
        if task.description.count(" и ") >= 2:
            return True
        if task.description.count(" + ") >= 1:
            return True
    return False


def build_decompose_prompt(tasks_file: str) -> str:
    """Build prompt for LLM decomposition agent."""
    return (
        f"@{tasks_file}\n\n"
        'Ты — планировщик задач. Проанализируй файл и определи, есть ли задачи со статусом "pending"\n'
        "которые слишком крупные, нечёткие или содержат несколько независимых шагов.\n\n"
        "Критерии для декомпозиции:\n"
        "- Задача описывает 2+ независимых действия\n"
        "- Описание слишком высокоуровневое, нет конкретных шагов\n"
        "- acceptance_criteria содержит 5+ пунктов из разных областей\n"
        "- Задача охватывает несколько файлов/модулей из разных доменов\n\n"
        "Если находишь такие задачи:\n"
        "1. Разбей каждую на 2-5 подзадач\n"
        "2. Новые подзадачи: ID формата TASK-XXXa, TASK-XXXb, ...\n"
        "3. Наследуют phase, category, priority родителя\n"
        "4. Добавь dependencies между подзадачами если нужно\n"
        "5. Замени родителя на подзадачи (удали оригинал, вставь подзадачи)\n"
        "6. Обнови total_tasks в корне JSON\n\n"
        "ПРАВИЛА:\n"
        '- НЕ трогай задачи со статусом "done", "in_progress", "failed"\n'
        "- НЕ меняй ID на которые ссылаются dependencies\n"
        "- Сохрани валидный JSON\n\n"
        "После анализа:\n"
        "- Если были изменения: <promise>DECOMPOSED N</promise> (N=новых подзадач)\n"
        "- Если изменений нет: <promise>NO_DECOMPOSE</promise>"
    )


# Module-level cache
_last_decompose_hash: str = ""
_last_decompose_result: str = ""  # "NO_DECOMPOSE" or "DECOMPOSED"


def _tasks_hash(tm: TaskManager) -> str:
    """SHA256 of pending task IDs + descriptions for cache."""
    parts = sorted(f"{t.id}:{t.description}" for t in tm.tasks if t.status == "pending")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def run_decompose(tm: TaskManager, agent_model: str, use_tmux: bool, log_dir: Path) -> int:
    """Run LLM decomposition. Returns number of added tasks (0 if no changes)."""
    global _last_decompose_hash, _last_decompose_result

    current_hash = _tasks_hash(tm)
    if current_hash == _last_decompose_hash and _last_decompose_result == "NO_DECOMPOSE":
        log.info("Decompose: skipped (cache hit, hash=%s)", current_hash[:8])
        return 0

    before_count = tm.total_count
    prompt = build_decompose_prompt(str(tm.path))

    from ralph.agent_runner import run_agent

    result = run_agent(prompt, model=agent_model)

    _last_decompose_hash = current_hash

    if "<promise>NO_DECOMPOSE</promise>" in result.result_text:
        _last_decompose_result = "NO_DECOMPOSE"
        log.info("Decompose: no changes needed")
        return 0

    # Agent may have modified the file directly
    tm.reload()
    after_count = tm.total_count
    delta = after_count - before_count

    if delta > 0:
        _last_decompose_result = "DECOMPOSED"
        log.info("Decompose: +%d tasks (%d → %d)", delta, before_count, after_count)
    else:
        _last_decompose_result = "NO_DECOMPOSE"
        log.info("Decompose: no task count change")

    return max(0, delta)
