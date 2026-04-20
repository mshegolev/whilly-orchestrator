#!/usr/bin/env python3
"""
Проверка синхронизации статусов задач.
Можно запускать в CI/CD или как health check.
"""

import sys
from pathlib import Path

# Добавляем whilly в PATH для импорта
sys.path.insert(0, str(Path(__file__).parent.parent))

from whilly.recovery import validate_task_consistency
from whilly.task_manager import TaskManager


def main():
    """CLI entry point for status consistency check."""
    if len(sys.argv) < 2:
        print("Usage: python check_status_sync.py <task_file.json> [workspace_dir]")
        sys.exit(1)

    task_file = Path(sys.argv[1])
    if not task_file.exists():
        print(f"❌ Task file not found: {task_file}")
        sys.exit(2)

    # Определяем workspace
    if len(sys.argv) > 2:
        workspace_dir = Path(sys.argv[2])
    else:
        workspace_dir = task_file.parent / ".whilly_workspaces"
        for ws in workspace_dir.glob("*/"):
            if ws.is_dir() and (ws / "progress.txt").exists():
                workspace_dir = ws
                break

    if not workspace_dir.exists():
        print(f"❌ Workspace not found: {workspace_dir}")
        sys.exit(3)

    # Загружаем task manager
    try:
        tm = TaskManager(task_file)
    except Exception as e:
        print(f"❌ Failed to load task file: {e}")
        sys.exit(4)

    # Проверяем консистентность
    warnings = validate_task_consistency(tm, workspace_dir)

    if not warnings:
        print("✅ Task statuses are consistent")
        print(f"   Total tasks: {tm.total_count}")
        print(f"   Done: {tm.done_count}")
        print(f"   Pending: {tm.pending_count}")
        sys.exit(0)
    else:
        print("⚠️  Status inconsistencies found:")
        for warning in warnings:
            print(f"   • {warning}")

        print("\n🔧 Run this to fix:")
        script_path = Path(__file__).parent / "sync_task_status.py"
        print(f"   python {script_path} {task_file}")

        sys.exit(1)


if __name__ == "__main__":
    main()
