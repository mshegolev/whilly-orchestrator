"""Unit tests for :mod:`whilly.cli.dashboard` — the ``whilly dashboard`` subcommand (TASK-027).

What we cover
-------------
- Argparse surface: ``--plan`` is required; the optional flags
  (``--interval``, ``--max-iterations``, ``--no-color``) all parse cleanly.
- DSN resolution: missing ``WHILLY_DATABASE_URL`` exits 2 with a
  diagnostic message rather than crashing inside asyncio.
- Pure renderer (:func:`render_dashboard`) produces the right column
  headers, status badges, claimed_by / started_at fallbacks, and
  paused-state title decoration.
- Status / priority colour palettes match the AC mandate:
  PENDING=grey50, CLAIMED=cyan, IN_PROGRESS=yellow, DONE=green,
  FAILED=red.
- Hotkey listener (:func:`_listen_for_keys`) flips the right state
  fields for ``q`` / ``r`` / ``p`` (and uppercase variants).
- Dispatcher wiring: ``whilly.cli.main(["dashboard", ...])`` routes
  into :func:`run_dashboard_command` rather than the legacy v3 parser.

What we deliberately *don't* cover here
---------------------------------------
End-to-end Live rendering against a real Postgres belongs in
:mod:`tests.integration.test_dashboard`, which spins up a container via
testcontainers. These unit tests stop at the boundary where
``asyncio.run`` would invoke ``create_pool`` — anything past that needs
a real DB.

Why we render via :class:`Console.export_text` for assertions
-------------------------------------------------------------
:class:`~rich.table.Table` is a renderable object, not a string. To
make byte-level assertions stable across Rich versions we render the
table into an in-memory :class:`~rich.console.Console`'s recording
buffer and then export plain text (no ANSI). That gives us a
deterministic snapshot we can substring-check against — same approach
the official Rich docs recommend for testing tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from rich.console import Console

from whilly.cli import main as cli_main
from whilly.cli.dashboard import (
    DATABASE_URL_ENV,
    DEFAULT_POLL_INTERVAL,
    EXIT_ENVIRONMENT_ERROR,
    DashboardRow,
    DashboardState,
    _listen_for_keys,
    _STATUS_COLOR,
    build_dashboard_parser,
    render_dashboard,
    run_dashboard_command,
)
from whilly.core.models import Priority, TaskStatus


# ─── argparse surface ────────────────────────────────────────────────────


def test_build_dashboard_parser_requires_plan() -> None:
    """``--plan`` is mandatory; argparse exits with code 2 (its convention).

    Pinning the contract here means a future refactor that loosens
    ``required=True`` is loud at test time.
    """
    parser = build_dashboard_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([])
    assert exc_info.value.code == 2


def test_build_dashboard_parser_accepts_all_optional_flags() -> None:
    """All optional flags parse without choking — surface check, not behaviour.

    A typo in ``add_argument`` (``--no_color`` vs ``--no-color``) is
    caught here before it leaks into the integration tests.
    """
    parser = build_dashboard_parser()
    args = parser.parse_args(
        [
            "--plan",
            "p1",
            "--interval",
            "0.25",
            "--max-iterations",
            "10",
            "--no-color",
        ]
    )
    assert args.plan_id == "p1"
    assert args.interval == 0.25
    assert args.max_iterations == 10
    assert args.no_color is True


def test_build_dashboard_parser_default_interval_is_none() -> None:
    """``--interval`` defaults to ``None`` so the handler can substitute :data:`DEFAULT_POLL_INTERVAL`.

    Pinning ``None`` (instead of ``DEFAULT_POLL_INTERVAL`` directly)
    makes "user did not specify" distinguishable from "user explicitly
    asked for the default" — a future audit log could surface that.
    """
    parser = build_dashboard_parser()
    args = parser.parse_args(["--plan", "p1"])
    assert args.interval is None
    assert args.max_iterations is None
    assert args.no_color is False
    assert DEFAULT_POLL_INTERVAL == 1.0  # AC pin: "обновлением каждую секунду"


# ─── DSN missing fast-fail ───────────────────────────────────────────────


def test_run_dashboard_command_without_database_url_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing ``WHILLY_DATABASE_URL`` exits 2 (no DB connection attempted).

    Doubles as a guard that we don't accidentally dereference a None
    DSN inside :func:`asyncio.run`.
    """
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    rc = run_dashboard_command(["--plan", "any-plan-id"])
    assert rc == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert DATABASE_URL_ENV in captured.err
    assert captured.out == ""


# ─── pure renderer ───────────────────────────────────────────────────────


def _render_to_text(table: Any) -> str:
    """Render a Rich :class:`Table` to plain text via a recording Console.

    Uses ``record=True`` so the console captures every ``print`` call
    and ``export_text`` returns ANSI-stripped output suitable for
    substring assertions. Width is pinned to a wide value so column
    headers don't wrap mid-word and break our assertions.
    """
    console = Console(record=True, width=120, force_terminal=False, no_color=True)
    console.print(table)
    return console.export_text()


def test_render_dashboard_empty_rows_shows_no_tasks_marker() -> None:
    """An empty plan renders ``(no tasks)`` rather than a blank table."""
    table = render_dashboard(
        plan_id="empty-plan",
        rows=(),
        paused=False,
        last_refresh=None,
        last_error=None,
        use_color=False,
    )
    rendered = _render_to_text(table)
    assert "Plan: empty-plan" in rendered
    assert "(no tasks)" in rendered
    # Hotkey hint always present. We render bindings as ``q=quit`` rather
    # than ``[q]uit`` because Rich's caption is markup-parsed and would
    # treat ``[q]`` as an unknown tag (silently dropped).
    assert "q=quit" in rendered
    assert "r=refresh" in rendered
    assert "p=pause" in rendered


def test_render_dashboard_populated_rows_show_all_columns() -> None:
    """Every dashboard column appears in the output for a populated plan."""
    rows = (
        DashboardRow(
            task_id="T-001",
            status=TaskStatus.DONE,
            priority=Priority.CRITICAL,
            claimed_by="worker-A",
            started_at=datetime(2026, 4, 28, 10, 15, 30, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 28, 10, 16, 0, tzinfo=timezone.utc),
        ),
        DashboardRow(
            task_id="T-002",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            claimed_by="worker-B",
            started_at=datetime(2026, 4, 28, 10, 17, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 28, 10, 17, 5, tzinfo=timezone.utc),
        ),
        DashboardRow(
            task_id="T-003",
            status=TaskStatus.PENDING,
            priority=Priority.LOW,
            claimed_by=None,
            started_at=None,
            updated_at=datetime(2026, 4, 28, 10, 14, 0, tzinfo=timezone.utc),
        ),
    )
    table = render_dashboard(
        plan_id="big-plan",
        rows=rows,
        paused=False,
        last_refresh=datetime(2026, 4, 28, 10, 17, 30, tzinfo=timezone.utc),
        last_error=None,
        use_color=False,
    )
    rendered = _render_to_text(table)

    # Title.
    assert "Plan: big-plan" in rendered
    assert "[PAUSED]" not in rendered

    # Headers.
    assert "Task" in rendered
    assert "Status" in rendered
    assert "Claimed by" in rendered
    assert "Started" in rendered
    assert "Priority" in rendered

    # Each task id.
    for tid in ("T-001", "T-002", "T-003"):
        assert tid in rendered, f"task id {tid} missing from render"

    # Status badges (plain text in no-color mode).
    assert "DONE" in rendered
    assert "IN_PROGRESS" in rendered
    assert "PENDING" in rendered

    # Priority badges.
    assert "critical" in rendered
    assert "high" in rendered
    assert "low" in rendered

    # Worker ids and the unowned em-dash placeholder.
    assert "worker-A" in rendered
    assert "worker-B" in rendered
    assert "—" in rendered  # PENDING row has no claimed_by

    # last_refresh timestamp surfaces in the caption.
    assert "last refresh:" in rendered


def test_render_dashboard_paused_marker_in_title() -> None:
    """``paused=True`` decorates the title so the operator knows polling stopped."""
    table = render_dashboard(
        plan_id="any",
        rows=(),
        paused=True,
        last_refresh=None,
        last_error=None,
        use_color=False,
    )
    rendered = _render_to_text(table)
    assert "[PAUSED]" in rendered


def test_render_dashboard_last_error_in_caption() -> None:
    """A non-None ``last_error`` surfaces in the caption so the operator can react."""
    table = render_dashboard(
        plan_id="any",
        rows=(),
        paused=False,
        last_refresh=None,
        last_error="ConnectionRefusedError: connection refused",
        use_color=False,
    )
    rendered = _render_to_text(table)
    assert "ConnectionRefusedError" in rendered
    assert "error:" in rendered


def test_render_dashboard_started_at_renders_as_hms() -> None:
    """Owned rows render the ``started_at`` clock time, unowned render ``—``."""
    rows = (
        DashboardRow(
            task_id="T-1",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
            claimed_by="w1",
            started_at=datetime(2026, 4, 28, 12, 34, 56, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 28, 12, 35, 0, tzinfo=timezone.utc),
        ),
        DashboardRow(
            task_id="T-2",
            status=TaskStatus.PENDING,
            priority=Priority.LOW,
            claimed_by=None,
            started_at=None,
            updated_at=datetime(2026, 4, 28, 12, 30, 0, tzinfo=timezone.utc),
        ),
    )
    table = render_dashboard(
        plan_id="x",
        rows=rows,
        paused=False,
        last_refresh=None,
        last_error=None,
        use_color=False,
    )
    rendered = _render_to_text(table)
    assert "12:34:56" in rendered  # owned row's claim time
    assert "—" in rendered  # unowned placeholder


# ─── colour palette pin ──────────────────────────────────────────────────


def test_status_palette_matches_ac_mandate() -> None:
    """The status colour palette honours the PRD AC verbatim.

    AC: "Цвета статусов: PENDING=серый, CLAIMED=голубой,
    IN_PROGRESS=жёлтый, DONE=зелёный, FAILED=красный".

    Pinning the dictionary protects the contract from a casual
    refactor that swaps colours without realising it's user-facing.
    """
    assert _STATUS_COLOR[TaskStatus.PENDING] == "grey50"
    assert _STATUS_COLOR[TaskStatus.CLAIMED] == "cyan"
    assert _STATUS_COLOR[TaskStatus.IN_PROGRESS] == "yellow"
    assert _STATUS_COLOR[TaskStatus.DONE] == "green"
    assert _STATUS_COLOR[TaskStatus.FAILED] == "red"
    # SKIPPED is not in the AC list but must still render — guard against
    # a KeyError regression by confirming the entry exists.
    assert TaskStatus.SKIPPED in _STATUS_COLOR


# ─── hotkey listener ─────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["q", "Q", "\x03"])
async def test_listener_q_sets_stop(key: str) -> None:
    """``q`` / ``Q`` / Ctrl-C all flip ``state.stop`` and exit the listener."""
    state = DashboardState()
    keys: list[str | None] = [key]

    async def src() -> str | None:
        return keys.pop(0) if keys else None

    await _listen_for_keys(state, src)
    assert state.stop is True
    assert state.paused is False


@pytest.mark.parametrize("key", ["p", "P"])
async def test_listener_p_toggles_paused(key: str) -> None:
    """``p`` / ``P`` flip ``state.paused`` and the listener keeps reading."""
    state = DashboardState()
    # First press pauses, second press unpauses, then None terminates.
    keys: list[str | None] = [key, key, None]

    async def src() -> str | None:
        return keys.pop(0) if keys else None

    await _listen_for_keys(state, src)
    assert state.stop is False
    assert state.paused is False  # toggled twice → back to default


async def test_listener_p_pause_persists_after_one_press() -> None:
    """A single ``p`` press leaves the dashboard paused (until next ``p``)."""
    state = DashboardState()
    keys: list[str | None] = ["p", None]

    async def src() -> str | None:
        return keys.pop(0) if keys else None

    await _listen_for_keys(state, src)
    assert state.paused is True


@pytest.mark.parametrize("key", ["r", "R"])
async def test_listener_r_sets_immediate_refresh(key: str) -> None:
    """``r`` / ``R`` flip ``state.immediate_refresh`` and keep listening."""
    state = DashboardState()
    keys: list[str | None] = [key, None]

    async def src() -> str | None:
        return keys.pop(0) if keys else None

    await _listen_for_keys(state, src)
    assert state.immediate_refresh is True
    assert state.stop is False


async def test_listener_unknown_key_is_ignored() -> None:
    """Unknown keys don't mutate state; the listener keeps reading.

    AC enumerates only three hotkeys; everything else must be a no-op
    so a fat-fingered keypress doesn't pause / quit the dashboard.
    """
    state = DashboardState()
    keys: list[str | None] = ["x", "1", " ", None]

    async def src() -> str | None:
        return keys.pop(0) if keys else None

    await _listen_for_keys(state, src)
    assert state.stop is False
    assert state.paused is False
    assert state.immediate_refresh is False


async def test_listener_none_immediately_returns() -> None:
    """An immediate ``None`` from the source ends the listener cleanly.

    Production fallback: a non-TTY stdin yields ``None`` on the first
    call, the listener exits, the polling loop runs in keystroke-less
    mode (operator quits via SIGINT or ``--max-iterations``).
    """
    state = DashboardState()

    async def src() -> str | None:
        return None

    await _listen_for_keys(state, src)
    assert state.stop is False  # no key was pressed; stop stays default-false


# ─── dispatcher wiring ──────────────────────────────────────────────────


def test_dispatcher_routes_dashboard_to_run_dashboard_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``whilly dashboard ...`` routes through the v4 dispatcher, not legacy v3.

    We exercise the routing without booting a pool by relying on the
    DSN-missing fast-fail: with ``WHILLY_DATABASE_URL`` cleared, the
    handler exits 2 before opening any connection. The test asserts
    that exit code surfaces from ``cli.main`` — proving the dispatcher
    routed to :func:`run_dashboard_command` rather than the legacy v3
    ``whilly`` parser (which would either reject the unknown
    subcommand or route into the old loop).
    """
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    rc = cli_main(["dashboard", "--plan", "p1"])
    assert rc == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    # Diagnostic must come from the v4 handler, not the v3 legacy parser.
    assert "whilly dashboard:" in captured.err
    assert DATABASE_URL_ENV in captured.err
