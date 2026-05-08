"""Browserless operator TUI for Whilly."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.db.repository import TaskRepository
from whilly.operator_views import (
    EventRow,
    OperatorSnapshot,
    OperatorSurface,
    OperatorTaskRow,
    ReviewGap,
    WorkerRow,
    fetch_operator_snapshot,
    filter_snapshot,
)
from whilly.pipeline.human_review_decisions import (
    HumanReviewDecisionCommand,
    record_human_review_decision as record_review_decision,
)

try:
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:  # pragma: no cover
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    _HAS_TERMIOS = False


DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"
REVIEWER_ENV: Final[str] = "WHILLY_OPERATOR_EMAIL"
DEFAULT_POLL_INTERVAL: Final[float] = 1.0
EXIT_OK: Final[int] = 0
EXIT_ENVIRONMENT_ERROR: Final[int] = 2

KeySource = Callable[[], Awaitable[str | None]]

_SURFACE_BY_KEY: Final[dict[str, OperatorSurface]] = {
    "1": OperatorSurface.OVERVIEW,
    "2": OperatorSurface.COMPLIANCE,
    "3": OperatorSurface.PLANS_TASKS,
    "4": OperatorSurface.WORKERS,
    "5": OperatorSurface.EVENTS,
}

_SURFACE_LABEL: Final[dict[OperatorSurface, str]] = {
    OperatorSurface.OVERVIEW: "Overview",
    OperatorSurface.COMPLIANCE: "Compliance",
    OperatorSurface.PLANS_TASKS: "Plans/Tasks",
    OperatorSurface.WORKERS: "Workers",
    OperatorSurface.EVENTS: "Events",
}


@dataclass
class TuiState:
    surface: OperatorSurface = OperatorSurface.OVERVIEW
    filter_text: str = ""
    searching: bool = False
    immediate_refresh: bool = False
    stop: bool = False
    last_error: str | None = None
    selected_review_index: int = 0
    pending_review_action: str | None = None
    pending_control_action: str | None = None


def build_tui_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly tui",
        description=(
            "Browserless operator interface. Hotkeys: q=quit, r=refresh, R=resume workers, "
            "1-5=switch, /=filter, p=pause workers."
        ),
    )
    parser.add_argument("--plan", dest="plan_id", default=None, help="Optional plan id filter.")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Seconds between refreshes.")
    parser.add_argument("--max-iterations", type=int, default=None, help="Test hook: stop after N polling ticks.")
    parser.add_argument("--no-color", action="store_true", help="Force plain output.")
    parser.add_argument(
        "--reviewer",
        default=None,
        help=f"Reviewer identity for human-review hotkeys (env: {REVIEWER_ENV}).",
    )
    return parser


def run_tui_command(
    argv: Sequence[str],
    *,
    key_source: KeySource | None = None,
) -> int:
    parser = build_tui_parser()
    args = parser.parse_args(list(argv))
    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(f"whilly tui: {DATABASE_URL_ENV} is not set.", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    use_color = not args.no_color and _stream_supports_color()
    try:
        asyncio.run(
            _async_run(
                dsn=dsn,
                plan_id=args.plan_id,
                interval=args.interval,
                max_iterations=args.max_iterations,
                use_color=use_color,
                key_source=key_source or _default_key_source(),
                reviewer=(args.reviewer or os.environ.get(REVIEWER_ENV) or "").strip(),
            )
        )
    except OSError as exc:
        print(f"whilly tui: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR
    return EXIT_OK


def handle_tui_key(state: TuiState, key: str) -> None:
    """Apply one hotkey to mutable TUI state."""

    if state.searching:
        if key in {"\n", "\r"}:
            state.searching = False
            return
        if key in {"\b", "\x7f"}:
            state.filter_text = state.filter_text[:-1]
            return
        if key == "\x1b":
            state.searching = False
            return
        if key.isprintable():
            state.filter_text += key
        return

    if key in {"q", "Q", "\x03"}:
        state.stop = True
    elif key == "r":
        state.immediate_refresh = True
    elif key == "R":
        state.pending_control_action = "resume"
        state.immediate_refresh = True
    elif key in {"p", "P"}:
        state.pending_control_action = "pause"
        state.immediate_refresh = True
    elif key == "/":
        state.searching = True
    elif key in _SURFACE_BY_KEY:
        state.surface = _SURFACE_BY_KEY[key]
    elif state.surface is OperatorSurface.COMPLIANCE and key in {"j", "J"}:
        state.selected_review_index += 1
    elif state.surface is OperatorSurface.COMPLIANCE and key in {"k", "K"}:
        state.selected_review_index = max(0, state.selected_review_index - 1)
    elif state.surface is OperatorSurface.COMPLIANCE and key in {"a", "A"}:
        state.pending_review_action = "approved"
        state.immediate_refresh = True
    elif state.surface is OperatorSurface.COMPLIANCE and key in {"x", "X"}:
        state.pending_review_action = "rejected"
        state.immediate_refresh = True
    elif state.surface is OperatorSurface.COMPLIANCE and key in {"c", "C"}:
        state.pending_review_action = "changes_requested"
        state.immediate_refresh = True


def render_tui(snapshot: OperatorSnapshot, state: TuiState) -> Group:
    """Render the current operator surface as Rich values."""

    visible = filter_snapshot(snapshot, state.filter_text)
    header = _header(snapshot, state)
    queue_health = _queue_health_table(snapshot)
    if state.surface is OperatorSurface.OVERVIEW:
        body = _overview_table(visible)
    elif state.surface is OperatorSurface.COMPLIANCE:
        body = _compliance_table(visible.review_gaps, state)
    elif state.surface is OperatorSurface.PLANS_TASKS:
        body = _tasks_table(visible.tasks)
    elif state.surface is OperatorSurface.WORKERS:
        body = _workers_table(visible.workers)
    else:
        body = _events_table(visible.events)
    return Group(header, queue_health, body)


async def _async_run(
    *,
    dsn: str,
    plan_id: str | None,
    interval: float,
    max_iterations: int | None,
    use_color: bool,
    key_source: KeySource,
    reviewer: str,
) -> None:
    pool = await create_pool(dsn)
    state = TuiState()
    snapshot = await _empty_snapshot()
    console = Console(file=sys.stdout, force_terminal=use_color, no_color=not use_color, highlight=False)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_listen_for_keys(state, key_source))
            tg.create_task(
                _poll_loop(
                    pool,
                    plan_id,
                    state,
                    snapshot=snapshot,
                    console=console,
                    interval=interval,
                    max_iterations=max_iterations,
                    reviewer=reviewer,
                )
            )
    finally:
        await close_pool(pool)


async def _poll_loop(
    pool: Any,
    plan_id: str | None,
    state: TuiState,
    *,
    snapshot: OperatorSnapshot,
    console: Console,
    interval: float,
    max_iterations: int | None,
    reviewer: str = "",
) -> None:
    iteration = 0
    with Live(render_tui(snapshot, state), console=console, refresh_per_second=4, screen=False) as live:
        while not state.stop:
            iteration += 1
            state.immediate_refresh = False
            if state.pending_control_action is not None:
                await _apply_pending_control_action(pool, state, operator=reviewer)
            try:
                snapshot = await fetch_operator_snapshot(pool, plan_id=plan_id)
                state.last_error = None
            except (OSError, RuntimeError) as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"
            if state.pending_review_action is not None:
                await _apply_pending_review_action(pool, snapshot, state, reviewer=reviewer)
            live.update(render_tui(snapshot, state))
            if max_iterations is not None and iteration >= max_iterations:
                state.stop = True
                break
            slept = 0.0
            slice_size = min(0.05, interval)
            while slept < interval and not state.stop and not state.immediate_refresh:
                await asyncio.sleep(slice_size)
                slept += slice_size


async def _apply_pending_control_action(pool: Any, state: TuiState, *, operator: str) -> bool:
    action = state.pending_control_action
    state.pending_control_action = None
    if action is None:
        return False
    repo = TaskRepository(pool)
    operator = operator.strip() or "tui"
    try:
        if action == "pause":
            await repo.pause_workers(reason="Paused from TUI", operator=operator)
        elif action == "resume":
            await repo.resume_workers(operator=operator)
        else:
            state.last_error = f"unknown control action: {action}"
            return False
    except (OSError, RuntimeError) as exc:
        state.last_error = f"{type(exc).__name__}: {exc}"
        return False
    state.last_error = None
    return True


async def _listen_for_keys(state: TuiState, key_source: KeySource) -> None:
    while not state.stop:
        key = await key_source()
        if key is None:
            return
        handle_tui_key(state, key)


async def _apply_pending_review_action(
    pool: Any,
    snapshot: OperatorSnapshot,
    state: TuiState,
    *,
    reviewer: str,
) -> bool:
    """Apply the pending review action to the selected actionable gap."""

    decision = state.pending_review_action
    state.pending_review_action = None
    if decision is None:
        return False
    reviewer = reviewer.strip()
    if not reviewer:
        state.last_error = f"reviewer required: pass --reviewer or set {REVIEWER_ENV}"
        return False
    selected = _selected_review_gap(filter_snapshot(snapshot, state.filter_text).review_gaps, state)
    if selected is None:
        state.last_error = "no actionable human review gap selected"
        return False
    await _record_human_review_decision(pool, selected, decision, reviewer)
    state.last_error = None
    state.immediate_refresh = True
    return True


async def _record_human_review_decision(pool: Any, gap: ReviewGap, decision: str, reviewer: str) -> None:
    requested_changes: tuple[str, ...] = ()
    if decision == "changes_requested":
        requested_changes = ("Requested from TUI operator controls.",)
    await record_review_decision(
        TaskRepository(pool),
        HumanReviewDecisionCommand(
            task_id=gap.task_id,
            decision=decision,  # type: ignore[arg-type]
            reviewer=reviewer,
            source="tui",
            stage_id=gap.stage_id,
            requested_changes=requested_changes,
        ),
    )


async def _empty_snapshot() -> OperatorSnapshot:
    from whilly.operator_views import ComplianceSummary

    return OperatorSnapshot(
        rendered_at=datetime.now().astimezone(),
        summary=ComplianceSummary(
            total_tasks=0,
            tasks_by_status={},
            workers_online=0,
            workers_total=0,
            failed_tasks=0,
            open_review_gaps=0,
        ),
        tasks=(),
        workers=(),
        events=(),
        review_gaps=(),
    )


def _header(snapshot: OperatorSnapshot, state: TuiState) -> Table:
    title = "Whilly operator - compact control plane"
    if snapshot.control_state.paused:
        title += " [WORKERS PAUSED]"
    table = Table(title=title, title_justify="left", expand=True, show_header=False, box=None, padding=(0, 1))
    for _ in range(5):
        table.add_column()
    table.add_row(
        *[_surface_tab(surface, index, state.surface) for index, surface in enumerate(OperatorSurface, start=1)]
    )
    mode = "search" if state.searching else "live"
    filter_part = f"filter: {state.filter_text}" if state.filter_text else "filter: -"
    error_part = f" error: {state.last_error}" if state.last_error else ""
    table.caption = (
        f"hotkeys: q=quit  r=refresh  R=resume workers  1-5=switch  /=filter  p=pause workers  "
        f"j/k=select  a=approve  x=reject  c=changes  {filter_part}  "
        f"mode: {mode}  rendered: {snapshot.rendered_at.strftime('%H:%M:%S')}{error_part}"
    )
    table.caption_justify = "left"
    return table


def _surface_tab(surface: OperatorSurface, index: int, active: OperatorSurface) -> Text:
    label = f"{index} {_SURFACE_LABEL[surface]}"
    if surface is active:
        return Text(label, style="bold reverse")
    return Text(label)


def _queue_health_table(snapshot: OperatorSnapshot) -> Table:
    summary = snapshot.summary
    active = summary.tasks_by_status.get("IN_PROGRESS", 0) + summary.tasks_by_status.get("CLAIMED", 0)
    table = Table(
        title="Queue health",
        title_justify="left",
        expand=True,
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )
    for label in ("Tasks", "Active", "Pending", "Done", "Failed", "Review", "Workers"):
        table.add_column(label, justify="right")
    table.add_row(
        str(summary.total_tasks),
        str(active),
        str(summary.tasks_by_status.get("PENDING", 0)),
        str(summary.tasks_by_status.get("DONE", 0)),
        str(summary.failed_tasks),
        str(summary.open_review_gaps),
        f"{summary.workers_online}/{summary.workers_total}",
    )
    return table


def _overview_table(snapshot: OperatorSnapshot) -> Table:
    table = Table(title="Overview", title_justify="left", expand=True, box=box.SIMPLE)
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Tasks", str(snapshot.summary.total_tasks))
    table.add_row("Workers", f"{snapshot.summary.workers_online}/{snapshot.summary.workers_total} online")
    table.add_row("Failed tasks", str(snapshot.summary.failed_tasks))
    table.add_row("Review gaps", str(snapshot.summary.open_review_gaps))
    for status, count in sorted(snapshot.summary.tasks_by_status.items()):
        table.add_row(status, str(count))
    return table


def _compliance_table(gaps: Sequence[ReviewGap], state: TuiState) -> Table:
    table = Table(
        title="Compliance - Human review / verification gaps",
        title_justify="left",
        expand=True,
        box=box.SIMPLE,
    )
    table.add_column("Sel")
    table.add_column("Task")
    table.add_column("Plan")
    table.add_column("Reason")
    table.add_column("Stage")
    table.add_column("Reviewer")
    table.add_column("Actions")
    if not gaps:
        table.add_row("", "(no gaps)", "", "", "", "", "")
        return table
    selected = _selected_review_gap(gaps, state)
    for gap in gaps:
        table.add_row(
            ">" if gap == selected else "",
            gap.task_id,
            gap.plan_id,
            gap.reason,
            gap.stage_id or "-",
            gap.reviewer or "-",
            "a/x/c" if gap.actionable else "-",
        )
    return table


def _selected_review_gap(gaps: Sequence[ReviewGap], state: TuiState) -> ReviewGap | None:
    actionable = tuple(gap for gap in gaps if gap.actionable)
    if not actionable:
        return None
    index = min(max(state.selected_review_index, 0), len(actionable) - 1)
    return actionable[index]


def _tasks_table(tasks: Sequence[OperatorTaskRow]) -> Table:
    table = Table(title="Plans/Tasks", title_justify="left", expand=True, box=box.SIMPLE)
    table.add_column("Task")
    table.add_column("Plan")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Worker")
    table.add_column("Review")
    if not tasks:
        table.add_row("(no tasks)", "", "", "", "", "")
        return table
    for task in tasks:
        table.add_row(
            task.task_id,
            task.plan_id,
            _status_text(task.status),
            _priority_text(task.priority),
            task.claimed_by or "-",
            _human_review_text(task.human_review),
        )
    return table


def _workers_table(workers: Sequence[WorkerRow]) -> Table:
    table = Table(title="Workers", title_justify="left", expand=True, box=box.SIMPLE)
    table.add_column("Worker")
    table.add_column("Host")
    table.add_column("Owner")
    table.add_column("Status")
    table.add_column("Heartbeat")
    if not workers:
        table.add_row("(no workers)", "", "", "", "")
        return table
    for worker in workers:
        table.add_row(
            worker.worker_id,
            worker.hostname,
            worker.owner_email or "-",
            _worker_status_text(worker.status),
            worker.last_heartbeat.strftime("%H:%M:%S"),
        )
    return table


def _events_table(events: Sequence[EventRow]) -> Table:
    table = Table(title="Events", title_justify="left", expand=True, box=box.SIMPLE)
    table.add_column("Id")
    table.add_column("Task")
    table.add_column("Plan")
    table.add_column("Type")
    table.add_column("At")
    if not events:
        table.add_row("(no events)", "", "", "", "")
        return table
    for event in events:
        table.add_row(
            str(event.event_id),
            event.task_id or "-",
            event.plan_id or "-",
            event.event_type,
            event.created_at.strftime("%H:%M:%S"),
        )
    return table


def _status_text(status: str) -> Text:
    style = {
        "DONE": "green",
        "FAILED": "red",
        "IN_PROGRESS": "yellow",
        "CLAIMED": "cyan",
        "PENDING": "dim",
        "SKIPPED": "dim",
    }.get(status.upper(), "")
    return Text(status, style=style)


def _priority_text(priority: str) -> Text:
    style = {
        "critical": "bold red",
        "high": "yellow",
        "medium": "",
        "low": "dim",
    }.get(priority.lower(), "")
    return Text(priority, style=style)


def _worker_status_text(status: str) -> Text:
    style = "green" if status.lower() == "online" else "red"
    return Text(status, style=style)


def _human_review_text(state: Any) -> Text:
    if not getattr(state, "required", False):
        return Text("-")
    decision = getattr(state, "decision", None)
    label = str(decision).replace("_", " ") if decision else "pending"
    stage_id = getattr(state, "stage_id", "")
    if stage_id:
        label = f"{label} ({stage_id})"
    style = {
        "approved": "green",
        "pending": "yellow",
        "changes requested": "yellow",
        "rejected": "red",
    }.get(str(decision or "pending").replace("_", " "), "")
    return Text(label, style=style)


def _stream_supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _default_key_source() -> KeySource:
    if not _HAS_TERMIOS:
        return _no_op_key_source
    fd = _resolve_stdin_fd()
    if fd is None:
        return _no_op_key_source
    return _make_termios_key_source(fd)


async def _no_op_key_source() -> str | None:
    return None


def _resolve_stdin_fd() -> int | None:
    try:
        fd = sys.stdin.fileno()
    except (io.UnsupportedOperation, AttributeError, ValueError):
        return None
    try:
        return fd if os.isatty(fd) else None
    except OSError:
        return None


def _make_termios_key_source(fd: int) -> KeySource:
    if termios is None or tty is None:  # pragma: no cover
        return _no_op_key_source
    saved_attrs = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    async def _read_one_key() -> str | None:
        def _blocking_read() -> str | None:
            try:
                raw = os.read(fd, 1)
            except OSError:
                return None
            if not raw:
                return None
            return raw.decode("utf-8", errors="ignore")

        try:
            return await asyncio.to_thread(_blocking_read)
        finally:
            if sys.stdin.closed:
                with contextlib.suppress(termios.error):
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved_attrs)

    return _read_one_key


__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_POLL_INTERVAL",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "REVIEWER_ENV",
    "TuiState",
    "build_tui_parser",
    "handle_tui_key",
    "render_tui",
    "run_tui_command",
]
