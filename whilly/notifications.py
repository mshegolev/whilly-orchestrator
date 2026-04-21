"""macOS voice notifications via `say` command."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("whilly")

SAY_BIN: str | None = shutil.which("say")
VOICE = "Milena"
ENABLED = os.environ.get("WHILLY_VOICE", "1").lower() not in ("0", "false", "no", "off")


# Classification prefix → spoken word. Honoured when the task title starts with
# `[feature]`, `[epic]`, `[bug]`, `[fix]`, `[FR-x]`, `[NFR-y]`, `[docs]`, `[chore]`.
_CATEGORY_PREFIXES: dict[str, str] = {
    "feature": "Фичу",
    "feat": "Фичу",
    "epic": "Эпик",
    "story": "Историю",
    "bug": "Баг",
    "fix": "Фикс",
    "docs": "Документацию",
    "chore": "Задачу",
    "refactor": "Рефакторинг",
    "test": "Тест",
    "nfr": "Нефункциональное требование",
    "fr": "Функциональное требование",
    "adr": "АДР",
}


def notify(text: str) -> None:
    """Speak text via macOS say. Noop if unavailable or disabled."""
    if not ENABLED or not SAY_BIN:
        return
    try:
        subprocess.Popen([SAY_BIN, "-v", VOICE, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _classify_and_strip(title: str) -> tuple[str | None, str]:
    """Return (spoken_category, cleaned_title).

    Pulls a leading ``[category]`` (or ``[FR-12]`` / ``[NFR-3]``) tag from the
    title, maps it to a Russian spoken noun via :data:`_CATEGORY_PREFIXES`,
    and strips it from the title so the announcement reads naturally.
    """
    if not title:
        return None, ""
    title = title.strip()
    match = re.match(r"^\[([A-Za-z]+)(?:[-/][\w.-]+)?\]\s*(.*)$", title)
    if not match:
        return None, title
    tag = match.group(1).lower()
    remainder = match.group(2).strip()
    category = _CATEGORY_PREFIXES.get(tag)
    return category, remainder or title


def _summarise_task(title: str | None, *, max_chars: int = 120) -> str:
    """Return the announcement fragment — category prefix + short title."""
    if not title:
        return "задачу без названия"
    # First line only, trimmed — `say` rambles forever otherwise and the
    # classifier regex only recognises a `[tag]` at the start of that line.
    first_line = title.splitlines()[0].strip() if title else ""
    if not first_line:
        return "задачу без названия"
    category, clean = _classify_and_strip(first_line)
    if len(clean) > max_chars:
        clean = clean[: max_chars - 1].rstrip() + "…"
    if category:
        return f"{category}: {clean}"
    return clean


def notify_decompose(count: int) -> None:
    notify(f"Декомпозиция: добавлено {count} задач.")


def notify_task_done(task_title: str | None = None) -> None:
    """Announce a single task completion. Speaks the task title when given."""
    if task_title:
        notify(f"Готово — {_summarise_task(task_title)}. Продолжаю работу.")
    else:
        notify("Задача готова. Продолжаю работу.")


def notify_plan_done(last_titles: list[str] | None = None) -> None:
    """Announce plan completion. Mentions up to two trailing task titles when given."""
    if last_titles:
        sample = [_summarise_task(t) for t in last_titles[-2:] if t]
        if sample:
            notify(f"План завершён! Последняя задача — {sample[-1]}.")
            return
    notify("План завершён!")


def notify_all_done(completed_count: int | None = None, last_title: str | None = None) -> None:
    """Announce everything-is-done. Mentions the last completed task when given."""
    if completed_count is not None and last_title:
        notify(f"Хозяин, я всё сделалъ! Выполнено {completed_count} задач, последняя — {_summarise_task(last_title)}.")
    elif last_title:
        notify(f"Хозяин, я всё сделалъ! Последняя задача — {_summarise_task(last_title)}.")
    else:
        notify("Хозяин, я всё сделалъ!")


def notify_budget_warning(pct: int) -> None:
    notify(f"Внимание! Бюджет израсходован на {pct} процентов.")


def notify_budget_exceeded() -> None:
    notify("Бюджет исчерпан! Останавливаю работу.")


def notify_deadlock(task_id: str) -> None:
    notify(f"Задача {task_id} заблокирована. Пропускаю.")
