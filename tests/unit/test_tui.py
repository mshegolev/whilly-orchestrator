from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import pytest
from rich.console import Console

from whilly.cli import tui as tui_module
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
    HumanReviewState,
    OperatorControlState,
    OperatorSnapshot,
    OperatorSurface,
    OperatorTable,
    OperatorTableColumn,
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
                human_review=HumanReviewState(required=True, stage_id="release_review", reason="stage_human_gate"),
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
        review_gaps=(
            ReviewGap(
                task_id="T-human",
                plan_id="P-1",
                reason="awaiting human review",
                stage_id="release_review",
                actionable=True,
            ),
        ),
    )


def test_render_tui_overview_includes_surfaces_and_hotkeys() -> None:
    rendered = _render_to_text(render_tui(_snapshot(), TuiState()))

    assert "Whilly operator" in rendered
    assert "Queue health" in rendered
    assert "Active" in rendered
    assert "Overview" in rendered
    assert "Compliance" in rendered
    assert "Plans/Tasks" in rendered
    assert "Workers" in rendered
    assert "Events" in rendered
    assert "Review gaps" in rendered
    assert "q=quit" in rendered
    assert "r=refresh" in rendered
    assert "R=resume workers" in rendered
    assert "/=filter" in rendered
    assert "p=pause workers" in rendered
    assert "j/k=select" in rendered
    assert "a=Approve review" in rendered
    assert "x=Reject review" in rendered
    assert "c=Changes" in rendered


def test_render_tui_tables_read_headers_from_operator_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    labels_by_table = {
        OperatorTable.TASKS: ("Task*", "Plan*", "Status*", "Priority*", "Worker*", "Review*"),
        OperatorTable.WORKERS: ("Worker*", "Host*", "Owner*", "Status*", "Heartbeat*"),
        OperatorTable.REVIEW_GAPS: ("Sel*", "Task*", "Plan*", "Reason*", "Stage*", "Reviewer*", "Actions*"),
        OperatorTable.EVENTS: ("Id*", "Task*", "Plan*", "Type*", "At*"),
    }
    calls: list[tuple[OperatorTable, str]] = []

    def fake_operator_table_columns(table: OperatorTable, medium: str) -> tuple[OperatorTableColumn, ...]:
        calls.append((table, medium))
        return tuple(OperatorTableColumn(f"field_{index}", label) for index, label in enumerate(labels_by_table[table]))

    monkeypatch.setattr(tui_module, "operator_table_columns", fake_operator_table_columns, raising=False)

    surface_cases = (
        (OperatorSurface.PLANS_TASKS, OperatorTable.TASKS),
        (OperatorSurface.WORKERS, OperatorTable.WORKERS),
        (OperatorSurface.COMPLIANCE, OperatorTable.REVIEW_GAPS),
        (OperatorSurface.EVENTS, OperatorTable.EVENTS),
    )
    for surface, table in surface_cases:
        rendered = _render_to_text(render_tui(_snapshot(), TuiState(surface=surface)))
        for label in labels_by_table[table]:
            assert label in rendered

    assert calls == [(table, "tui") for _, table in surface_cases]


def test_render_tui_tasks_surface_uses_contract_labels_without_updated() -> None:
    rendered = _render_to_text(render_tui(_snapshot(), TuiState(surface=OperatorSurface.PLANS_TASKS)))

    for label in ("Task", "Plan", "Status", "Priority", "Worker", "Review"):
        assert label in rendered
    assert "Updated" not in rendered


def test_render_tui_workers_surface_uses_contract_labels() -> None:
    rendered = _render_to_text(render_tui(_snapshot(), TuiState(surface=OperatorSurface.WORKERS)))

    for label in ("Worker", "Host", "Owner", "Status", "Heartbeat"):
        assert label in rendered


def test_render_tui_compliance_surface_uses_contract_labels() -> None:
    rendered = _render_to_text(render_tui(_snapshot(), TuiState(surface=OperatorSurface.COMPLIANCE)))

    for label in ("Sel", "Task", "Plan", "Reason", "Stage", "Reviewer", "Actions"):
        assert label in rendered


def test_render_tui_events_surface_uses_contract_labels() -> None:
    rendered = _render_to_text(render_tui(_snapshot(), TuiState(surface=OperatorSurface.EVENTS)))

    for label in ("Id", "Task", "Plan", "Type", "At"):
        assert label in rendered


def test_handle_tui_key_switches_views_filter_pause_refresh_review_actions_and_quit() -> None:
    state = TuiState()

    handle_tui_key(state, "2")
    assert state.surface is OperatorSurface.COMPLIANCE

    handle_tui_key(state, "j")
    assert state.selected_review_index == 1
    handle_tui_key(state, "k")
    assert state.selected_review_index == 0
    handle_tui_key(state, "a")
    assert state.pending_review_action == "approved"
    assert state.immediate_refresh is True
    state.pending_review_action = None
    state.immediate_refresh = False
    handle_tui_key(state, "x")
    assert state.pending_review_action == "rejected"
    state.pending_review_action = None
    handle_tui_key(state, "c")
    assert state.pending_review_action == "changes_requested"

    handle_tui_key(state, "p")
    assert state.pending_control_action == "pause"
    assert state.immediate_refresh is True
    state.pending_control_action = None
    state.immediate_refresh = False

    handle_tui_key(state, "r")
    assert state.immediate_refresh is True
    state.immediate_refresh = False
    handle_tui_key(state, "R")
    assert state.pending_control_action == "resume"
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


@pytest.mark.asyncio
async def test_poll_loop_applies_pending_control_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []
    control_actions: list[tuple[str, str, str]] = []

    async def fake_fetch_operator_snapshot(pool: Any, *, plan_id: str | None) -> OperatorSnapshot:
        calls.append(plan_id)
        return _snapshot()

    class FakeTaskRepository:
        def __init__(self, pool: Any) -> None:
            self.pool = pool

        async def pause_workers(self, *, reason: str | None = None, operator: str | None = None) -> object:
            control_actions.append(("pause", reason or "", operator or ""))
            return object()

        async def resume_workers(self, *, operator: str | None = None) -> object:
            control_actions.append(("resume", "", operator or ""))
            return object()

    monkeypatch.setattr(tui_module, "fetch_operator_snapshot", fake_fetch_operator_snapshot)
    monkeypatch.setattr(tui_module, "TaskRepository", FakeTaskRepository)
    state = TuiState(pending_control_action="pause", immediate_refresh=True)
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)

    await tui_module._poll_loop(
        object(),
        "P-1",
        state,
        snapshot=_snapshot(),
        console=console,
        interval=0,
        max_iterations=1,
        reviewer="lead@example.com",
    )

    assert calls == ["P-1"]
    assert control_actions == [("pause", "Paused from TUI", "lead@example.com")]


def test_render_tui_filter_limits_task_rows() -> None:
    state = TuiState(filter_text="human", surface=OperatorSurface.PLANS_TASKS)

    rendered = _render_to_text(render_tui(_snapshot(), state))

    assert "T-human" in rendered
    assert "T-alpha" not in rendered
    assert "filter: human" in rendered


def test_render_tui_compliance_shows_clear_review_action_help() -> None:
    state = TuiState(surface=OperatorSurface.COMPLIANCE)

    rendered = _render_to_text(render_tui(_snapshot(), state))

    assert "Queue health" in rendered
    assert "Compliance - Human review / verification gaps" in rendered
    assert ">" in rendered
    assert "awaiting human review" in rendered
    assert "release_review" in rendered
    assert "Actions" in rendered
    assert "a/x/c" in rendered
    assert "a=Approve review" in rendered
    assert "x=Reject review" in rendered
    assert "c=Changes" in rendered


def test_render_tui_header_marks_workers_paused() -> None:
    snapshot = _snapshot()
    paused_snapshot = OperatorSnapshot(
        rendered_at=snapshot.rendered_at,
        summary=snapshot.summary,
        tasks=snapshot.tasks,
        workers=snapshot.workers,
        events=snapshot.events,
        review_gaps=snapshot.review_gaps,
        control_state=OperatorControlState(paused=True, pause_reason="release gate"),
    )

    rendered = _render_to_text(render_tui(paused_snapshot, TuiState()))

    assert "WORKERS PAUSED" in rendered


@pytest.mark.asyncio
async def test_apply_pending_review_action_records_selected_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[ReviewGap, str, str]] = []

    async def fake_record(pool: Any, gap: ReviewGap, decision: str, reviewer: str) -> None:
        recorded.append((gap, decision, reviewer))

    monkeypatch.setattr(tui_module, "_record_human_review_decision", fake_record)
    state = TuiState(surface=OperatorSurface.COMPLIANCE, pending_review_action="approved")

    applied = await tui_module._apply_pending_review_action(object(), _snapshot(), state, reviewer="lead@example.com")

    assert applied is True
    assert recorded == [(_snapshot().review_gaps[0], "approved", "lead@example.com")]
    assert state.pending_review_action is None
    assert state.immediate_refresh is True
    assert state.last_error is None


@pytest.mark.asyncio
async def test_apply_pending_review_action_requires_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[ReviewGap, str, str]] = []

    async def fake_record(pool: Any, gap: ReviewGap, decision: str, reviewer: str) -> None:
        recorded.append((gap, decision, reviewer))

    monkeypatch.setattr(tui_module, "_record_human_review_decision", fake_record)
    state = TuiState(surface=OperatorSurface.COMPLIANCE, pending_review_action="approved")

    applied = await tui_module._apply_pending_review_action(object(), _snapshot(), state, reviewer="")

    assert applied is False
    assert recorded == []
    assert state.pending_review_action is None
    assert state.immediate_refresh is False
    assert state.last_error == "reviewer required: pass --reviewer or set WHILLY_OPERATOR_EMAIL"


@pytest.mark.asyncio
async def test_apply_pending_review_action_requires_selected_actionable_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[ReviewGap, str, str]] = []

    async def fake_record(pool: Any, gap: ReviewGap, decision: str, reviewer: str) -> None:
        recorded.append((gap, decision, reviewer))

    monkeypatch.setattr(tui_module, "_record_human_review_decision", fake_record)
    snapshot = _snapshot()
    no_review_snapshot = OperatorSnapshot(
        rendered_at=snapshot.rendered_at,
        summary=snapshot.summary,
        tasks=snapshot.tasks,
        workers=snapshot.workers,
        events=snapshot.events,
        review_gaps=(),
        control_state=snapshot.control_state,
    )
    state = TuiState(surface=OperatorSurface.COMPLIANCE, pending_review_action="rejected")

    applied = await tui_module._apply_pending_review_action(
        object(),
        no_review_snapshot,
        state,
        reviewer="lead@example.com",
    )

    assert applied is False
    assert recorded == []
    assert state.pending_review_action is None
    assert state.immediate_refresh is False
    assert state.last_error == "no actionable human review gap selected"


@pytest.mark.asyncio
async def test_record_human_review_decision_uses_shared_service(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[Any] = []

    async def fake_record(repo: Any, command: Any) -> None:
        commands.append(command)

    monkeypatch.setattr(tui_module, "record_review_decision", fake_record)

    await tui_module._record_human_review_decision(
        object(),
        _snapshot().review_gaps[0],
        "changes_requested",
        "lead@example.com",
    )

    assert len(commands) == 1
    command = commands[0]
    assert command.task_id == "T-human"
    assert command.decision == "changes_requested"
    assert command.reviewer == "lead@example.com"
    assert command.source == "tui"
    assert command.stage_id == "release_review"
    assert command.requested_changes == ("Requested from TUI operator controls.",)


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
