#!/usr/bin/env python3
"""
Утилита восстановления статусов задач из логов.
Использует progress.txt и логи задач для синхронизации tasks-from-github.json.
"""

import json
import re
from pathlib import Path
from typing import Dict, Set


def extract_done_from_progress(progress_file: Path) -> Set[str]:
    """Извлекает ID завершенных задач из progress.txt."""
    if not progress_file.exists():
        return set()

    done_tasks = set()
    content = progress_file.read_text(encoding="utf-8")

    # Парсим строки вида: [task-id] DONE — description
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("[") and "] DONE" in line:
            match = re.match(r"\[([^\]]+)\] DONE", line)
            if match:
                done_tasks.add(match.group(1))

    return done_tasks


def extract_done_from_logs(logs_dir: Path) -> Set[str]:
    """Извлекает ID завершенных задач из логов по маркеру <promise>COMPLETE</promise>."""
    if not logs_dir.exists():
        return set()

    done_tasks = set()
    completion_marker = "<promise>COMPLETE</promise>"

    for log_file in logs_dir.glob("*.log"):
        if log_file.name == "whilly_events.jsonl":
            continue

        try:
            content = log_file.read_text(encoding="utf-8")
            if completion_marker in content:
                # Извлекаем task_id из имени файла
                task_id = log_file.stem
                done_tasks.add(task_id)
        except Exception as e:
            print(f"⚠️  Warning: Could not read {log_file}: {e}")

    return done_tasks


def sync_task_status(task_file: Path, workspace_dir: Path = None) -> Dict[str, str]:
    """Синхронизирует статусы задач в JSON файле."""

    if workspace_dir is None:
        # Автоопределение workspace
        workspace_dir = task_file.parent / ".whilly_workspaces"
        # Найдем подходящую workspace
        for ws in workspace_dir.glob("*/"):
            if ws.is_dir() and (ws / "progress.txt").exists():
                workspace_dir = ws
                break

    progress_file = workspace_dir / "progress.txt"
    logs_dir = workspace_dir / "whilly_logs"

    print(f"📁 Workspace: {workspace_dir}")
    print(f"📄 Progress: {progress_file}")
    print(f"📂 Logs: {logs_dir}")

    # Извлекаем завершенные задачи
    done_from_progress = extract_done_from_progress(progress_file)
    done_from_logs = extract_done_from_logs(logs_dir)

    print(f"✅ Done from progress.txt: {sorted(done_from_progress)}")
    print(f"✅ Done from logs: {sorted(done_from_logs)}")

    # Объединяем источники (приоритет progress.txt)
    all_done = done_from_progress | done_from_logs

    if not all_done:
        print("❌ No completed tasks found")
        return {}

    # Читаем и обновляем task file
    if not task_file.exists():
        print(f"❌ Task file not found: {task_file}")
        return {}

    with open(task_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    changes = {}
    for task in data.get("tasks", []):
        task_id = task.get("id")
        current_status = task.get("status")

        if task_id in all_done and current_status != "done":
            task["status"] = "done"
            changes[task_id] = f"{current_status} → done"

    if changes:
        # Создаем backup
        backup_file = task_file.with_suffix(".json.backup")
        task_file.rename(backup_file)
        print(f"💾 Backup created: {backup_file}")

        # Записываем обновленный файл
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"✅ Updated {task_file}")
        for task_id, change in changes.items():
            print(f"   {task_id}: {change}")
    else:
        print("✅ All statuses are already correct")

    return changes


def main():
    """CLI entry point."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python sync_task_status.py <task_file.json> [workspace_dir]")
        print("Example: python sync_task_status.py tasks-from-github.json")
        sys.exit(1)

    task_file = Path(sys.argv[1])
    workspace_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    print(f"🔄 Synchronizing task status: {task_file}")
    changes = sync_task_status(task_file, workspace_dir)

    if changes:
        print(f"\n🎉 Successfully updated {len(changes)} task(s)")
    else:
        print("\n✅ No changes needed")


if __name__ == "__main__":
    main()
