from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from rich.console import Console

from whilly.cli.tui import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    TuiState,
    handle_tui_key,
    render_tui,
    run_tui_command,
)
from whilly.operator_views import (
    ComplianceSummary,
    EventRow,
    OperatorSnapshot,
    OperatorSurface,
    OperatorTaskRow,
    ReviewGap,
    WorkerRow,
)


def _render_to_text(renderable: Any) -> str:
    console = Console(record=True, width=120, force_terminal=False, no_color=True)
    console.print(renderable)
    return console.export_text()


def _snapshot() -> OperatorSnapshot:
    now = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
    return OperatorSnapshot(
        rendered_at=now,
        summary=ComplianceSummary(
            total_tasks=2,
            tasks_by_status={"PENDING": 1, "IN_PROGRESS": 1},
            workers_online=1,
            workers_total=1,
            failed_tasks=0,
            open_review_gaps=1,
        ),
        tasks=(
            OperatorTaskRow(
                task_id="T-alpha",
                plan_id="P-1",
                status="PENDING",
                priority="high",
                claimed_by=None,
                started_at=None,
                updated_at=now,
                acceptance_criteria=("done",),
                test_steps=("pytest",),
            ),
            OperatorTaskRow(
                task_id="T-human",
                plan_id="P-1",
                status="IN_PROGRESS",
                priority="critical",
                claimed_by="worker-a",
                started_at=now,
                updated_at=now,
                acceptance_criteria=(),
                test_steps=("human approval",),
            ),
        ),
        workers=(
            WorkerRow(
                worker_id="worker-a",
                hostname="alpha.local",
                owner_email="ops@example.com",
                status="online",
                last_heartbeat=now,
            ),
        ),
        events=(
            EventRow(
                event_id=1,
                task_id="T-human",
                plan_id="P-1",
                event_type="START",
                created_at=now,
                detail={"worker_id": "worker-a"},
            ),
        ),
        review_gaps=(ReviewGap(task_id="T-human", plan_id="P-1", reason="missing acceptance criteria"),),
    )


def test_render_tui_overview_includes_surfaces_and_hotkeys() -> None:
    rendered = _render_to_text(render_tui(_snapshot(), TuiState()))

    assert "Whilly operator" in rendered
    assert "Overview" in rendered
    assert "Compliance" in rendered
    assert "Plans/Tasks" in rendered
    assert "Workers" in rendered
    assert "Events" in rendered
    assert "Review gaps" in rendered
    assert "q=quit" in rendered
    assert "r=refresh" in rendered
    assert "/=filter" in rendered
    assert "p=pause" in rendered


def test_handle_tui_key_switches_views_filter_pause_refresh_and_quit() -> None:
    state = TuiState()

    handle_tui_key(state, "2")
    assert state.surface is OperatorSurface.COMPLIANCE

    handle_tui_key(state, "p")
    assert state.paused is True

    handle_tui_key(state, "r")
    assert state.immediate_refresh is True

    handle_tui_key(state, "/")
    assert state.searching is True
    handle_tui_key(state, "a")
    handle_tui_key(state, "b")
    handle_tui_key(state, "\b")
    handle_tui_key(state, "\n")
    assert state.searching is False
    assert state.filter_text == "a"

    handle_tui_key(state, "q")
    assert state.stop is True


def test_render_tui_filter_limits_task_rows() -> None:
    state = TuiState(filter_text="human", surface=OperatorSurface.PLANS_TASKS)

    rendered = _render_to_text(render_tui(_snapshot(), state))

    assert "T-human" in rendered
    assert "T-alpha" not in rendered
    assert "filter: human" in rendered


def test_run_tui_command_without_database_url_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    rc = run_tui_command([])

    assert rc == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert DATABASE_URL_ENV in captured.err
    assert captured.out == ""
