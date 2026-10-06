"""Bounded, explicit prompt context helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def bounded_history(history: Sequence[dict[str, Any]], *, max_chars: int) -> list[dict[str, Any]]:
    """Keep chronology, newest user request, and an explicit omission marker."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    newest_user = next((m for m in reversed(history) if m.get("sender") == "user"), None)
    if newest_user and len(str(newest_user.get("body", ""))) > max_chars:
        raise ValueError("latest user request exceeds context limit")
    newest_index = max((i for i, m in enumerate(history) if m.get("sender") == "user"), default=None)
    selected_indices: set[int] = {newest_index} if newest_index is not None else set()
    used = len(str(newest_user.get("body", ""))) if newest_user else 0
    omitted = 0
    for index in range(len(history) - 1, -1, -1):
        if index == newest_index:
            continue
        message = history[index]
        size = len(str(message.get("body", "")))
        if used + size <= max_chars:
            selected_indices.add(index)
            used += size
        else:
            omitted += 1
    ordered_indices = sorted(selected_indices)
    result = [history[i] for i in ordered_indices]
    if omitted:
        marker = {"sender": "system", "body": f"[{omitted} earlier message(s) omitted for context budget]"}
        first_omitted = next(i for i in range(len(history)) if i not in selected_indices)
        insert_at = next((i for i, index in enumerate(ordered_indices) if index > first_omitted), len(result))
        result.insert(insert_at, marker)
    return result
