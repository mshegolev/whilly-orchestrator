"""
Recovery механизмы для синхронизации статусов задач.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Set

from whilly.agents.base import COMPLETION_MARKER


def recover_task_statuses(task_manager, workspace_dir: Path) -> Dict[str, str]:
    """
    Восстанавливает статусы задач после сбоя orchestrator'а.

    Returns:
        Dict[task_id, status_change]: словарь изменений статусов
    """
    progress_file = workspace_dir / "progress.txt"
    logs_dir = workspace_dir / "whilly_logs"

    # 1. Извлекаем done задачи из progress.txt
    done_from_progress = _extract_done_from_progress(progress_file)

    # 2. Извлекаем done задачи из логов
    done_from_logs = _extract_done_from_logs(logs_dir)

    # 3. Объединяем источники
    all_done = done_from_progress | done_from_logs

    # 4. Обновляем статусы
    changes = {}
    for task in task_manager.tasks:
        if task.id in all_done and task.status != "done":
            old_status = task.status
            task.status = "done"
            changes[task.id] = f"{old_status} → done"

    # 5. Сохраняем изменения
    if changes:
        task_manager.save()

    return changes


def _extract_done_from_progress(progress_file: Path) -> Set[str]:
    """Извлекает ID завершенных задач из progress.txt."""
    if not progress_file.exists():
        return set()

    done_tasks = set()
    content = progress_file.read_text(encoding='utf-8')

    # Парсим строки вида: [task-id] DONE — description
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('[') and '] DONE' in line:
            match = re.match(r'\[([^\]]+)\] DONE', line)
            if match:
                done_tasks.add(match.group(1))

    return done_tasks


def _extract_done_from_logs(logs_dir: Path) -> Set[str]:
    """Извлекает ID завершенных задач из логов по маркеру completion."""
    if not logs_dir.exists():
        return set()

    done_tasks = set()

    for log_file in logs_dir.glob("*.log"):
        if log_file.name == "whilly_events.jsonl":
            continue

        try:
            content = log_file.read_text(encoding='utf-8')

            # Проверяем наличие completion marker
            if COMPLETION_MARKER in content:
                # Дополнительная проверка — парсим JSON если возможно
                try:
                    lines = content.split('\n')
                    for line in lines:
                        if line.startswith('{"type":"result"'):
                            result = json.loads(line)
                            if (not result.get('is_error', False) and
                                COMPLETION_MARKER in result.get('result', '')):
                                task_id = log_file.stem
                                done_tasks.add(task_id)
                                break
                except json.JSONDecodeError:
                    # Fallback на простой поиск маркера
                    task_id = log_file.stem
                    done_tasks.add(task_id)

        except Exception as e:
            # Логируем, но не падаем
            print(f"Warning: Could not process {log_file}: {e}")

    return done_tasks


def validate_task_consistency(task_manager, workspace_dir: Path) -> List[str]:
    """
    Проверяет консистентность между task statuses и реальным состоянием.

    Returns:
        List[str]: список предупреждений о несоответствиях
    """
    warnings = []

    progress_file = workspace_dir / "progress.txt"
    logs_dir = workspace_dir / "whilly_logs"

    done_from_progress = _extract_done_from_progress(progress_file)
    done_from_logs = _extract_done_from_logs(logs_dir)

    # Задачи, помеченные как done в task file
    done_in_tasks = {t.id for t in task_manager.tasks if t.status == "done"}

    # Проверяем несоответствия
    missing_in_tasks = (done_from_progress | done_from_logs) - done_in_tasks
    if missing_in_tasks:
        warnings.append(
            f"Tasks completed but not marked 'done': {sorted(missing_in_tasks)}"
        )

    extra_in_tasks = done_in_tasks - (done_from_progress | done_from_logs)
    if extra_in_tasks:
        warnings.append(
            f"Tasks marked 'done' but no completion evidence: {sorted(extra_in_tasks)}"
        )

    return warnings