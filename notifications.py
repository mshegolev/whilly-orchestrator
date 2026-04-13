"""macOS voice notifications via `say` command."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger("ralph")

SAY_BIN: str | None = shutil.which("say")
VOICE = "Milena"
ENABLED = os.environ.get("RALPH_VOICE", "1").lower() not in ("0", "false", "no", "off")


def notify(text: str) -> None:
    """Speak text via macOS say. Noop if unavailable or disabled."""
    if not ENABLED or not SAY_BIN:
        return
    try:
        subprocess.Popen([SAY_BIN, "-v", VOICE, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def notify_decompose(count: int) -> None:
    notify(f"Декомпозиция: добавлено {count} задач.")


def notify_task_done() -> None:
    notify("Задача готова. Продолжаю работу.")


def notify_plan_done() -> None:
    notify("План завершён!")


def notify_all_done() -> None:
    notify("Хозяин, я всё сделалъ!")


def notify_budget_warning(pct: int) -> None:
    notify(f"Внимание! Бюджет израсходован на {pct} процентов.")


def notify_budget_exceeded() -> None:
    notify("Бюджет исчерпан! Останавливаю работу.")


def notify_deadlock(task_id: str) -> None:
    notify(f"Задача {task_id} заблокирована. Пропускаю.")
