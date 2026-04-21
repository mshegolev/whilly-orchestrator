"""Unit tests for per-task audio announcements (issue #162).

Exercises the title-parsing logic in :mod:`whilly.notifications` without
actually invoking macOS `say` — every test stubs the `notify()` sink so we
capture the phrase that would be spoken.
"""

from __future__ import annotations

import pytest

from whilly import notifications


@pytest.fixture
def spoken(monkeypatch):
    """Capture calls to ``notifications.notify()``."""
    phrases: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda text: phrases.append(text))
    return phrases


# ─── classifier ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected_category,expected_clean",
    [
        ("[feature] add audio for task names", "Фичу", "add audio for task names"),
        ("[Feat] rename thing", "Фичу", "rename thing"),
        ("[NFR-5] Cost panel in dashboard", "Нефункциональное требование", "Cost panel in dashboard"),
        ("[FR-9] PR body composition", "Функциональное требование", "PR body composition"),
        ("[bug] deadlock on budget cap", "Баг", "deadlock on budget cap"),
        ("[epic] revamp orchestrator loop", "Эпик", "revamp orchestrator loop"),
        ("[ADR-017] draft decision", "АДР", "draft decision"),
        ("[docs] README update", "Документацию", "README update"),
        ("no bracket prefix", None, "no bracket prefix"),
        ("", None, ""),
        (None, None, ""),
    ],
)
def test_classify_and_strip(title, expected_category, expected_clean):
    cat, clean = notifications._classify_and_strip(title)
    assert cat == expected_category
    assert clean == expected_clean


def test_summarise_truncates_long_titles():
    long_title = "[feature] " + ("word " * 80)
    summary = notifications._summarise_task(long_title, max_chars=60)
    assert summary.startswith("Фичу: ")
    # Check the clean part (after "Фичу: ") is ≤ max_chars + 1 (for the ellipsis).
    assert len(summary.split(": ", 1)[1]) <= 61


def test_summarise_handles_multiline_body():
    summary = notifications._summarise_task("[bug] crash\n\nsome body here\nmore")
    assert summary == "Баг: crash"


def test_summarise_empty():
    assert notifications._summarise_task(None) == "задачу без названия"
    assert notifications._summarise_task("") == "задачу без названия"


# ─── notify_task_done ──────────────────────────────────────────────────────────


def test_notify_task_done_without_title_is_generic(spoken):
    notifications.notify_task_done()
    assert spoken == ["Задача готова. Продолжаю работу."]


def test_notify_task_done_with_categorised_title(spoken):
    notifications.notify_task_done("[feature] добавь аудио с названием задачи")
    assert len(spoken) == 1
    assert spoken[0].startswith("Готово — Фичу: добавь аудио с названием задачи.")


def test_notify_task_done_with_untagged_title(spoken):
    notifications.notify_task_done("Refactor the orchestrator")
    assert spoken == ["Готово — Refactor the orchestrator. Продолжаю работу."]


# ─── notify_plan_done / notify_all_done ────────────────────────────────────────


def test_notify_plan_done_without_titles(spoken):
    notifications.notify_plan_done()
    assert spoken == ["План завершён!"]


def test_notify_plan_done_with_last_titles(spoken):
    notifications.notify_plan_done(["[docs] README refresh"])
    assert spoken == ["План завершён! Последняя задача — Документацию: README refresh."]


def test_notify_all_done_without_context(spoken):
    notifications.notify_all_done()
    assert spoken == ["Хозяин, я всё сделалъ!"]


def test_notify_all_done_with_count_and_title(spoken):
    notifications.notify_all_done(completed_count=3, last_title="[bug] deadlock on budget cap")
    assert spoken == ["Хозяин, я всё сделалъ! Выполнено 3 задач, последняя — Баг: deadlock on budget cap."]


def test_notify_all_done_with_just_title(spoken):
    notifications.notify_all_done(last_title="Refactor the orchestrator")
    assert spoken == ["Хозяин, я всё сделалъ! Последняя задача — Refactor the orchestrator."]
