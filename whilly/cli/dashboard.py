"""``whilly dashboard`` subcommand — Rich Live TUI over the ``tasks`` table (TASK-027, PRD NFR-3, Module structure).

Composition root for the read-only operator view. Boots an asyncpg pool,
polls Postgres every ``--interval`` seconds (default 1.0s), and rewrites
a Rich :class:`~rich.table.Table` in place via :class:`~rich.live.Live`.
Hotkeys:

* ``q`` — quit (clean shutdown of the polling loop and the pool).
* ``r`` — request an immediate refresh (skip the remaining sleep).
* ``p`` — pause / unpause polling. While paused, the on-screen snapshot
  is whatever the last fetch saw; the title bar carries a ``[PAUSED]``
  marker so the operator knows the data is stale on purpose.

Why this lives next to ``whilly plan show``
-------------------------------------------
``plan show`` is the *one-shot* read view (snapshot the plan, render the
DAG, exit). The dashboard is the *streaming* read view (re-poll the same
SELECT every second). Both share:

* the same SELECT-from-Postgres entry seam (here it's
  :data:`_SELECT_DASHBOARD_ROWS_SQL`; ``plan show`` re-uses
  :func:`whilly.cli.plan._select_plan_with_tasks`);
* the same status → colour palette (intentionally duplicated to keep the
  two callers decoupled — a future Rich theme refactor only needs to
  touch one map per file rather than ripple across both).

Putting them in sibling files under :mod:`whilly.cli` matches the v4
hexagonal layering: this module is a thin adapter that composes the DB
pool, a pure renderer, and a Rich Live runtime — no business logic of
its own.

Layering and side-effect surface
--------------------------------
* :func:`render_dashboard` — pure function: takes a sequence of
  :class:`DashboardRow` plus a few flags, returns a Rich
  :class:`~rich.table.Table` (a value, not an action). Snapshot tests
  pin its layout without booting Postgres or Rich's live runtime.
* :func:`fetch_dashboard_rows` — the only DB read. One SELECT per call,
  ordered by priority then id (same tiebreaker as ``claim_task`` in the
  repository, so the dashboard's row order matches the order workers
  will visit them).
* :func:`run_dashboard_command` — the CLI entry point that stitches
  argparse, the pool lifecycle, the Live renderer, and the hotkey
  listener together. The async work happens inside :func:`_async_run`
  via :func:`asyncio.run` so the synchronous CLI wrapper stays loop-free.

Why a read-only projection (``DashboardRow``) instead of :class:`Task`?
----------------------------------------------------------------------
The :class:`whilly.core.models.Task` value object is intentionally
narrow — it carries the fields a *worker* needs to act on a task
(dependencies, key_files, acceptance_criteria, etc.) plus the
optimistic-locking ``version`` counter. It deliberately omits operator-
facing metadata like ``claimed_by`` and ``claimed_at`` because adding
those would either:

1. force every domain code path to carry around ``Optional`` claim
   fields that 95% of consumers never look at, or
2. push the dashboard to JOIN against ``workers`` / ``events`` to
   reconstruct fields it could have asked for in one SELECT.

A small read-model dataclass at the adapter boundary is the textbook
CQRS pattern: writers go through the rich domain model with full
invariants; readers get the projection they need. The class is local to
this module (``__all__`` exports it for tests) so it never tempts the
``whilly.core`` layer.

``started_at`` semantics
-------------------------
The schema stores ``claimed_at`` (set on CLAIM, preserved through
START / DONE / FAILED, cleared on RELEASE → PENDING). The dashboard
shows it under the column header ``Started`` because operationally the
two coincide: a worker that owns a row has *started caring about* it,
even if it hasn't yet flipped ``CLAIMED`` → ``IN_PROGRESS``. Using
``claimed_at`` directly avoids a JOIN against ``events`` for what the
operator's eye reads as "how long has this task been owned?".

Concurrency contract with active workers
----------------------------------------
The polling loop runs SELECTs at READ COMMITTED isolation (asyncpg's
default). Each tick sees a consistent snapshot of the ``tasks`` table
at the moment of the query — concurrent UPDATE traffic from claimers /
completers / sweeps shows up at the next tick. Because the dashboard
never writes, there is no risk of conflicting with the optimistic-
locking lattice in :class:`~whilly.adapters.db.repository.TaskRepository`.

Hotkey implementation
---------------------
Single-character raw-mode reads from ``sys.stdin`` are POSIX-only
(termios + tty). On Windows the listener is silently disabled — the
dashboard still polls and renders, the operator just can't quit /
pause / refresh interactively (Ctrl-C still works because
:class:`~rich.live.Live` installs the signal handler). The injection
seam ``key_source`` lets tests substitute a deterministic key generator
(e.g. yields ``"q"`` after one tick) without poking at terminal state.

Exit codes
----------
Mirrors :mod:`whilly.cli.plan` and :mod:`whilly.cli.run` — one numbering
across the v4 CLI surface:

* ``0`` — dashboard exited cleanly (``q`` pressed, ``--max-iterations``
  reached, or the polling loop terminated for any other reason).
* ``2`` — environment failure: ``WHILLY_DATABASE_URL`` unset, or the
  requested ``plan_id`` is missing from ``plans``. The plan-missing
  surface mirrors ``whilly plan show`` so an operator typo gives the
  same diagnostic shape regardless of which read view they hit first.

There is intentionally no validation-error (``1``) path — the dashboard
takes no input that could be malformed beyond what argparse already
rejects with its own ``2``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import asyncpg
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from whilly.adapters.db import close_pool, create_pool
from whilly.core.models import PlanId, Priority, TaskId, TaskStatus

# POSIX-only single-char raw-mode reader. Importing inside a try-block keeps
# the module importable on Windows (where the dashboard still works in
# polling-only mode — operators just can't drive the hotkeys interactively).
try:
    import termios
    import tty

    _HAS_TERMIOS: bool = True
except ImportError:  # pragma: no cover — exercised only on Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    _HAS_TERMIOS = False


__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_POLL_INTERVAL",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "DashboardRow",
    "DashboardState",
    "build_dashboard_parser",
    "fetch_dashboard_rows",
    "render_dashboard",
    "run_dashboard_command",
]

logger = logging.getLogger(__name__)


# Same env var :mod:`whilly.cli.plan` and :mod:`whilly.cli.run` read. Single
# source of truth for the v4 CLI's Postgres pointer (PRD A-1) — operators
# only have to set one variable for the entire surface.
DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"

# Exit codes — kept aligned with :mod:`whilly.cli.plan` / :mod:`whilly.cli.run`
# so callers comparing across subcommands never see numbering drift.
EXIT_OK: Final[int] = 0
EXIT_ENVIRONMENT_ERROR: Final[int] = 2

# Default polling cadence. AC pins "Rich Live с обновлением каждую секунду";
# the flag exists so tests can drive the loop in milliseconds without waiting
# for the production cadence.
DEFAULT_POLL_INTERVAL: Final[float] = 1.0


# Status → Rich colour mapping. Intentionally duplicated from
# :data:`whilly.cli.plan._STATUS_COLOR` rather than imported, because:
#
# 1. The two files are independent compositional roots — a refactor that
#    changes the dashboard's palette (e.g. distinguishing IN_PROGRESS-with-
#    stale-heartbeat from IN_PROGRESS-fresh) shouldn't ripple into the
#    snapshot tests for ``plan show``.
# 2. Importing ``_STATUS_COLOR`` from another module would couple the two
#    rendering tests; if a future ``plan show`` change tweaks one colour,
#    every dashboard test would need updating too.
#
# Palette matches the AC: PENDING=grey, CLAIMED=cyan, IN_PROGRESS=yellow,
# DONE=green, FAILED=red. SKIPPED gets dim grey because it's a non-error
# terminal state that shouldn't draw the eye like FAILED.
_STATUS_COLOR: Final[dict[TaskStatus, str]] = {
    TaskStatus.PENDING: "grey50",
    TaskStatus.CLAIMED: "cyan",
    TaskStatus.IN_PROGRESS: "yellow",
    TaskStatus.DONE: "green",
    TaskStatus.FAILED: "red",
    TaskStatus.SKIPPED: "grey37",
}


# Priority → display weight. Lower number = more urgent. We render the raw
# string in the table cell (``critical``, ``high``, ``medium``, ``low``) but
# colour ``critical`` and ``high`` more loudly so the operator's eye picks
# them out of a long list. Leaving ``medium`` and ``low`` uncoloured keeps
# the table calm by default — the typical run is dominated by medium-priority
# work and a wall of warm colours would defeat the point.
_PRIORITY_COLOR: Final[dict[Priority, str]] = {
    Priority.CRITICAL: "bold red",
    Priority.HIGH: "bold yellow",
    Priority.MEDIUM: "white",
    Priority.LOW: "grey50",
}


# SELECT for one polling tick. Five operator-facing columns plus
# ``priority_rank`` for the deterministic ORDER BY (same tiebreaker the
# repository's ``_CLAIM_SQL`` uses, so the dashboard's row order matches
# the order workers will visit them). We project ``claimed_at`` directly
# rather than computing ``EXTRACT(EPOCH FROM NOW() - claimed_at)`` — the
# renderer formats the duration so we can keep the SQL portable across
# Postgres versions and we can show absolute timestamps when useful.
#
# JOINing ``workers`` to surface hostname is intentionally *not* done here:
# the AC says "claimed_by" (the worker_id), and adding the hostname would
# bloat the row width without operational gain — the worker_id already
# encodes hostname via :func:`whilly.cli.run._resolve_worker_id`'s
# ``<hostname>-<short-uuid>`` convention.
_SELECT_DASHBOARD_ROWS_SQL: Final[str] = """
SELECT
    id,
    status,
    priority,
    claimed_by,
    claimed_at,
    updated_at
FROM tasks
WHERE plan_id = $1
ORDER BY
    CASE priority
        WHEN 'critical' THEN 0
        WHEN 'high' THEN 1
        WHEN 'medium' THEN 2
        WHEN 'low' THEN 3
        ELSE 4
    END,
    id
"""


# Probe used to surface a clean "plan not found" diagnostic instead of a
# silent empty render. Cheaper than ``SELECT COUNT(*)`` against ``tasks``
# (single row by primary key, no tablescan).
_SELECT_PLAN_EXISTS_SQL: Final[str] = """
SELECT 1
FROM plans
WHERE id = $1
"""


@dataclass(frozen=True)
class DashboardRow:
    """Read-model projection of one ``tasks`` row for the dashboard view.

    Distinct from :class:`whilly.core.models.Task` on purpose — see the
    module docstring's "Why a read-only projection" section. The fields
    are exactly what the operator-visible table needs:

    * ``task_id`` / ``status`` / ``priority`` — same as ``Task``, but only
      these three columns (no dependencies / key_files / acceptance_criteria,
      etc., which would inflate the table without operational gain).
    * ``claimed_by`` — ``None`` for PENDING / SKIPPED rows, set to the
      worker_id of the current owner otherwise. Survives across
      ``CLAIMED`` → ``IN_PROGRESS`` → ``DONE`` / ``FAILED`` until a
      RELEASE flips the row back to PENDING.
    * ``started_at`` — alias for the schema's ``claimed_at``. ``None`` for
      unclaimed rows. See the module docstring's "started_at semantics"
      section for the rationale.
    * ``updated_at`` — last-mutation timestamp from the schema. Used to
      render an "age" hint in the rightmost column so the operator can
      tell a stuck task from a freshly-claimed one at a glance.

    Frozen so the renderer can safely cache or hash row instances; tuples
    of frozen dataclasses are hashable, which makes this projection a
    drop-in for any future "diff against last tick" optimisation.
    """

    task_id: TaskId
    status: TaskStatus
    priority: Priority
    claimed_by: str | None
    started_at: datetime | None
    updated_at: datetime


@dataclass
class DashboardState:
    """Mutable shared state between the polling loop and the hotkey listener.

    Plain dataclass (not frozen) by design — the listener mutates ``stop``
    and ``paused`` from another asyncio task, and the polling loop reads
    them on every tick. ``immediate_refresh`` is the ``r`` hotkey signal:
    set by the listener, cleared by the loop after a short-circuit fetch.

    Concurrency model
    -----------------
    asyncio is cooperative single-threaded — a coroutine reading a flag
    cannot observe a "torn" write because the writer cannot preempt the
    reader mid-statement. We therefore don't need a lock for these three
    bool fields; a regular attribute mutation is atomic at the cooperative-
    scheduling boundary.

    Why three flags rather than an Enum or a Queue?
        ``stop`` is sticky (once set, the loop unwinds), ``paused`` toggles
        repeatedly, and ``immediate_refresh`` is one-shot (set by the
        listener, cleared on the next tick). The three semantics don't
        compose well into a single state value, and a Queue would force
        the polling loop to context-switch on every tick to drain it.
    """

    stop: bool = False
    paused: bool = False
    immediate_refresh: bool = False


# Type alias for the optional ``key_source`` injection seam used by tests.
# Production callers leave it ``None`` so the production termios listener is
# used. Tests pass an async callable that yields predetermined keys and then
# returns ``None`` to signal "no more input".
KeySource = Callable[[], Awaitable[str | None]]


def build_dashboard_parser() -> argparse.ArgumentParser:
    """Build the ``whilly dashboard ...`` argparse tree.

    Pulled into its own factory for symmetry with :func:`build_run_parser`
    (cli.run) and :func:`build_plan_parser` (cli.plan) — tests can
    introspect the declared CLI surface without invoking the side-
    effecting handler (which opens a pool and grabs the terminal).
    """
    parser = argparse.ArgumentParser(
        prog="whilly dashboard",
        description=(
            "Live read-only TUI of a plan's tasks (status, claimed_by, started_at, priority). "
            "Hotkeys: q=quit, r=refresh, p=pause polling."
        ),
    )
    parser.add_argument(
        "--plan",
        dest="plan_id",
        required=True,
        help="Plan id to display (matches the 'plan_id' from `whilly plan import`).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=(
            f"Seconds between polling ticks (default: {DEFAULT_POLL_INTERVAL}). "
            "Lower for tight test loops; higher to reduce DB poll pressure on "
            "shared production deployments."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Cap the polling loop after N ticks (default: unbounded). "
            "Test hook for deterministic CI runs that want a predictable exit; "
            "production leaves it unset and exits via the 'q' hotkey or SIGINT."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Force plain ASCII output (default: auto-detect via isatty / NO_COLOR).",
    )
    return parser


def run_dashboard_command(
    argv: Sequence[str],
    *,
    key_source: KeySource | None = None,
) -> int:
    """Entry point for ``whilly dashboard ...``; returns the process exit code.

    ``key_source`` is the unit-test injection seam (same shape as the
    ``runner`` parameter on :func:`whilly.cli.run.run_run_command`). When
    ``None``, production termios reading is wired in. Tests pass an async
    callable that yields one key per call so the loop is driven
    deterministically without grabbing the terminal.

    Stays synchronous on the outside so callers (and tests) don't need an
    event loop — the async work is delegated to :func:`_async_run` via
    :func:`asyncio.run`.

    Why no ``install_signal_handlers`` flag (cf. :mod:`whilly.cli.run`)?
        :class:`~rich.live.Live` installs its own SIGINT handler to
        restore the terminal cleanly on Ctrl-C; layering our own
        ``loop.add_signal_handler`` on top would double-handle the signal
        and confuse the unwind sequence. The dashboard is operator-driven
        (``q`` hotkey is the canonical exit) and Ctrl-C is the fallback
        — both are handled by Rich's own context manager, so we don't
        need extra plumbing.
    """
    parser = build_dashboard_parser()
    args = parser.parse_args(list(argv))

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly dashboard: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    interval = args.interval if args.interval is not None else DEFAULT_POLL_INTERVAL
    use_color = _should_use_color(no_color=bool(args.no_color))
    effective_key_source = key_source if key_source is not None else _default_key_source()

    try:
        asyncio.run(
            _async_run(
                dsn=dsn,
                plan_id=args.plan_id,
                interval=interval,
                max_iterations=args.max_iterations,
                use_color=use_color,
                key_source=effective_key_source,
            )
        )
    except _PlanNotFoundError as exc:
        print(
            f"whilly dashboard: plan {exc.plan_id!r} not found — check the id matches the "
            "'plan_id' you used at import time, or run `whilly plan import` first.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    return EXIT_OK


class _PlanNotFoundError(Exception):
    """Internal signal that the requested plan_id is absent from Postgres.

    Same pattern as :class:`whilly.cli.run._PlanNotFoundError` — raised
    inside :func:`_async_run`, caught at the sync boundary in
    :func:`run_dashboard_command` so the caller can map it to
    ``EXIT_ENVIRONMENT_ERROR`` without an ``Optional`` return type.
    Module-private because the only producer and the only consumer live
    in this file.
    """

    def __init__(self, plan_id: str) -> None:
        super().__init__(plan_id)
        self.plan_id = plan_id


async def _async_run(
    *,
    dsn: str,
    plan_id: PlanId,
    interval: float,
    max_iterations: int | None,
    use_color: bool,
    key_source: KeySource,
) -> None:
    """Open the pool, verify the plan exists, run the polling+listener loop.

    Pool lifecycle is local to this call (same pattern as
    :func:`whilly.cli.plan._async_export` / :func:`whilly.cli.run._async_run`).
    The ``finally`` always calls :func:`close_pool` so a crash inside the
    Live loop or a SIGINT caught by Rich still drains connections.

    The plan-existence probe runs *before* the Live context manager grabs
    the terminal — otherwise a missing plan would wipe the operator's
    scrollback before they could see the diagnostic. Same UX rationale
    applies to the DSN check in :func:`run_dashboard_command` (which runs
    even before this function is called).
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(_SELECT_PLAN_EXISTS_SQL, plan_id)
            if exists is None:
                raise _PlanNotFoundError(plan_id)

        state = DashboardState()
        console = Console(
            file=sys.stdout,
            force_terminal=use_color,
            no_color=not use_color,
            highlight=False,
        )

        async with asyncio.TaskGroup() as tg:
            # Listener runs alongside the polling loop. Both watch ``state``
            # to decide when to stop. The ``Live`` context manager owns the
            # terminal grab/release; the listener and the poller never touch
            # ``sys.stdout`` directly except through ``live.update``.
            tg.create_task(_listen_for_keys(state, key_source))
            tg.create_task(
                _poll_loop(
                    pool,
                    plan_id,
                    state,
                    console=console,
                    interval=interval,
                    max_iterations=max_iterations,
                    use_color=use_color,
                )
            )
    finally:
        await close_pool(pool)


async def _poll_loop(
    pool: asyncpg.Pool,
    plan_id: PlanId,
    state: DashboardState,
    *,
    console: Console,
    interval: float,
    max_iterations: int | None,
    use_color: bool,
) -> None:
    """Polling + Live-render loop. One ``SELECT`` per tick; rebuilds the table in place.

    Re-fetches every tick (AC: "Reads from DB only — не зависит от
    in-memory state"). The ``paused`` flag short-circuits the SELECT so
    a paused dashboard truly stops hitting the DB; it still rewrites the
    table so the ``[PAUSED]`` marker renders.

    The ``immediate_refresh`` flag is the ``r`` hotkey signal: we exit
    the sleep early and re-fetch right away. Cleared on the next iteration
    so the flag is one-shot (set by the listener, consumed by the loop).

    Loop termination:

    * ``state.stop`` — the ``q`` hotkey or any external signal flipped it.
    * ``max_iterations`` — test hook; production leaves it ``None`` so the
      loop runs until the operator quits.

    Why ``Live`` and not raw print loops?
        Without Live, every tick would scroll a new table down the
        terminal — useless for an operator. ``Live`` rewrites a single
        region in place. ``refresh_per_second`` is set conservatively so
        we control the rendering rhythm; the polling rhythm is still our
        own ``interval``.
    """
    iteration = 0
    last_rows: tuple[DashboardRow, ...] = ()
    last_refresh: datetime | None = None
    last_error: str | None = None

    initial_table = render_dashboard(
        plan_id=plan_id,
        rows=last_rows,
        paused=state.paused,
        last_refresh=last_refresh,
        last_error=last_error,
        use_color=use_color,
    )

    with Live(initial_table, console=console, refresh_per_second=4, screen=False) as live:
        while not state.stop:
            iteration += 1
            if not state.paused:
                try:
                    last_rows = await fetch_dashboard_rows(pool, plan_id)
                    last_refresh = datetime.now().astimezone()
                    last_error = None
                except (asyncpg.PostgresError, OSError) as exc:
                    # Surface fetch failures inside the dashboard rather
                    # than tearing the whole loop down — operators want
                    # to *see* a transient connection blip, not have the
                    # TUI vanish. The next tick re-tries the SELECT.
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("dashboard fetch failed: %s", last_error)

            live.update(
                render_dashboard(
                    plan_id=plan_id,
                    rows=last_rows,
                    paused=state.paused,
                    last_refresh=last_refresh,
                    last_error=last_error,
                    use_color=use_color,
                )
            )

            if max_iterations is not None and iteration >= max_iterations:
                state.stop = True
                break

            # Sleep in small slices so ``r`` and ``q`` hotkeys feel
            # responsive even at long ``--interval`` settings. The
            # ``immediate_refresh`` flag exits the sleep early.
            slept = 0.0
            slice_size = min(0.05, interval)
            while slept < interval and not state.stop and not state.immediate_refresh:
                await asyncio.sleep(slice_size)
                slept += slice_size
            state.immediate_refresh = False


async def fetch_dashboard_rows(pool: asyncpg.Pool, plan_id: PlanId) -> tuple[DashboardRow, ...]:
    """Run one SELECT and map every row into a :class:`DashboardRow`.

    Returns a tuple (not a list) so callers that cache rows across ticks
    can compare with ``==`` semantically. Empty plan → empty tuple, not
    an error — the renderer shows "(no tasks)" in the table body.

    Public so tests can drive the read path without standing up the Live
    loop. Same shape as :func:`whilly.cli.plan._select_plan_with_tasks` —
    one connection acquired, one query, mapped to value objects, returned.
    """
    async with pool.acquire() as conn:
        records = await conn.fetch(_SELECT_DASHBOARD_ROWS_SQL, plan_id)
    return tuple(_row_to_dashboard_row(rec) for rec in records)


def _row_to_dashboard_row(record: asyncpg.Record) -> DashboardRow:
    """Map one ``tasks``-table row to a :class:`DashboardRow`.

    Local mirror of :func:`whilly.cli.plan._row_to_task` — intentionally
    duplicated rather than imported because the projection columns differ
    (no dependencies / key_files / etc.) and we want this module's read
    path to stay paired with its own SELECT statement.
    """
    return DashboardRow(
        task_id=record["id"],
        status=TaskStatus(record["status"]),
        priority=Priority(record["priority"]),
        claimed_by=record["claimed_by"],
        started_at=record["claimed_at"],
        updated_at=record["updated_at"],
    )


def render_dashboard(
    *,
    plan_id: PlanId,
    rows: Sequence[DashboardRow],
    paused: bool,
    last_refresh: datetime | None,
    last_error: str | None,
    use_color: bool,
) -> Table:
    """Return a Rich :class:`~rich.table.Table` for one dashboard tick.

    Pure function: takes value objects, returns a renderable. No DB, no
    process state, no Rich Console. The polling loop calls it once per
    tick and feeds the result into ``live.update``; tests call it with
    fabricated rows to pin layout / colour decisions without booting
    Postgres or grabbing the terminal.

    Layout
    ------
    Five columns matching the AC: ``Task``, ``Status``, ``Claimed by``,
    ``Started``, ``Priority``. Status and Priority cells use the colour
    palettes above; the other columns render plain so they don't compete
    for the operator's eye. Header row carries the plan id and a
    ``[PAUSED]`` marker (when ``paused=True``); the footer (``caption``)
    carries the last-refresh timestamp and any transient fetch error so
    the operator can tell a stuck dashboard from a stuck plan.

    use_color
    ---------
    When ``True``, status / priority cells are wrapped in
    :class:`~rich.text.Text` with style metadata so Rich emits ANSI
    sequences at print time. When ``False``, the same cells render as
    plain strings and the table prints clean ASCII (suitable for
    snapshots, log captures, or piping through tools that don't strip
    ANSI). The choice is driven by :func:`_should_use_color` at the CLI
    boundary; the renderer just honours the flag.

    Returns a fresh :class:`Table` per call (no mutation of a shared
    instance) because :class:`~rich.live.Live` re-renders by replacing
    the renderable each tick — a shared instance would invite race
    conditions if a future caller decided to mutate it concurrently.
    """
    title = _build_title(plan_id, paused=paused)
    caption = _build_caption(last_refresh, last_error)

    table = Table(
        title=title,
        caption=caption,
        title_justify="left",
        caption_justify="left",
        expand=True,
        show_lines=False,
        header_style="bold",
    )
    table.add_column("Task", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Claimed by", no_wrap=True)
    table.add_column("Started", no_wrap=True)
    table.add_column("Priority", no_wrap=True)

    if not rows:
        # ``add_row`` with five strings keeps the column count consistent
        # so Rich doesn't paint a malformed empty row. Single-column
        # ``add_row("(no tasks)")`` would split / pad oddly under
        # ``expand=True``.
        table.add_row(Text("(no tasks)", style="grey50"), "", "", "", "")
        return table

    for row in rows:
        table.add_row(
            row.task_id,
            _status_cell(row.status, use_color=use_color),
            row.claimed_by or _muted("—", use_color=use_color),
            _format_started_at(row.started_at, use_color=use_color),
            _priority_cell(row.priority, use_color=use_color),
        )
    return table


def _build_title(plan_id: PlanId, *, paused: bool) -> str:
    """Compose the table title: ``Plan: <id>`` plus ``[PAUSED]`` when relevant.

    Title text is the operator's primary signal that polling is active —
    the ``[PAUSED]`` marker is loud and unambiguous. We deliberately
    don't colour it inside the title because Rich's table title has its
    own style sequence and embedded markup interacts oddly with the
    ``title_justify`` setting.
    """
    if paused:
        return f"Plan: {plan_id}  [PAUSED]"
    return f"Plan: {plan_id}"


def _build_caption(last_refresh: datetime | None, last_error: str | None) -> str:
    """Compose the bottom-of-table line: hotkey hint + last-refresh + error.

    Hotkey hint is always present so a fresh operator doesn't have to
    consult ``--help`` to discover ``q``/``r``/``p``. Last-refresh
    timestamp lets them tell a frozen TUI from a healthy one. Last error
    surfaces transient fetch failures (network blips, Postgres restarts)
    without tearing down the loop.

    Why ``q=quit`` instead of ``[q]uit``?
        Rich's table caption is rendered through its markup parser, and
        bare ``[q]`` reads as an unknown style tag — Rich silently drops
        it and prints just ``uit``. Escaping (``\\[q\\]``) works but is
        noisy in the source. The ``q=quit`` form is unambiguous, escapes
        no characters, and reads the same way ``man`` pages document
        single-key bindings.
    """
    parts: list[str] = ["hotkeys: q=quit  r=refresh  p=pause"]
    if last_refresh is not None:
        parts.append(f"last refresh: {last_refresh.strftime('%H:%M:%S')}")
    if last_error is not None:
        parts.append(f"error: {last_error}")
    return "  ·  ".join(parts)


def _status_cell(status: TaskStatus, *, use_color: bool) -> Text | str:
    """Render the status cell with palette colour when ``use_color=True``.

    Returning :class:`Text` (not a string with ``[green]...[/green]``
    markup) sidesteps Rich's markup parser entirely — the cell is
    therefore safe for any future status name that happens to contain a
    bracket. The fallback string branch is what snapshot tests assert
    against because pure-string equality is robust across Rich versions.
    """
    if not use_color:
        return status.value
    return Text(status.value, style=_STATUS_COLOR[status])


def _priority_cell(priority: Priority, *, use_color: bool) -> Text | str:
    """Render the priority cell. Same pattern as :func:`_status_cell`."""
    if not use_color:
        return priority.value
    return Text(priority.value, style=_PRIORITY_COLOR[priority])


def _muted(text: str, *, use_color: bool) -> Text | str:
    """Render ``text`` in dim grey when colour is on, plain otherwise.

    Used for the placeholder ``—`` glyph in claim-related columns when a
    task isn't owned by anyone. Without the muted style the em-dash
    fights the real worker_ids for the eye; with it, the row reads as
    "no claim here" without screaming.
    """
    if not use_color:
        return text
    return Text(text, style="grey50")


def _format_started_at(started_at: datetime | None, *, use_color: bool) -> Text | str:
    """Render the ``Started`` column: HH:MM:SS for owned rows, ``—`` for unowned.

    We render time-of-day (not duration) because two adjacent rows with
    different absolute claim times read naturally as "this one's been
    running longer" without the renderer doing arithmetic against the
    current clock — and a static absolute time is robust to clock skew /
    laptop sleep that would corrupt a "claimed Ns ago" string. Operators
    who want the duration have their eyes plus the title's last-refresh
    timestamp.

    Timezone-aware ``datetime`` objects from asyncpg render in their
    stored timezone (UTC by default for our schema's TIMESTAMPTZ); naive
    objects (shouldn't happen via the production schema, but possible in
    handcrafted tests) render in local time.
    """
    if started_at is None:
        return _muted("—", use_color=use_color)
    return started_at.strftime("%H:%M:%S")


# ─── hotkey listener ─────────────────────────────────────────────────────


async def _listen_for_keys(state: DashboardState, key_source: KeySource) -> None:
    """Read keys forever (or until ``state.stop``) and mutate ``state``.

    The mapping is intentionally tiny:

    * ``q`` / ``Q`` / ``\\x03`` (Ctrl-C, defensive) → ``state.stop = True``.
    * ``r`` / ``R`` → ``state.immediate_refresh = True`` (loop short-circuits
      its sleep on the next iteration).
    * ``p`` / ``P`` → ``state.paused = not state.paused``.

    Anything else is ignored silently (no audible bell, no flash) — the
    AC enumerates only three hotkeys and a noisy "unknown key" indicator
    would compete with the table for the operator's eye.

    The ``key_source`` callable is the test seam. Production wires
    :func:`_default_key_source`, which reads one byte at a time from
    stdin via :func:`asyncio.to_thread` so the polling loop is never
    blocked by I/O. Tests pass an async closure that yields a script of
    keys and then ``None`` to terminate — the listener exits the loop on
    ``None`` so the TaskGroup unwinds cleanly when the script is
    exhausted.
    """
    while not state.stop:
        try:
            key = await key_source()
        except asyncio.CancelledError:
            raise
        except OSError:
            # Production termios reads can surface EOFError-like
            # conditions if stdin closes (e.g. the CLI was launched
            # without a TTY). Treat as "no more keys" and exit the
            # listener — the polling loop keeps running and the
            # operator can quit via SIGINT.
            return
        if key is None:
            return
        if key in {"q", "Q", "\x03"}:
            state.stop = True
            return
        if key in {"r", "R"}:
            state.immediate_refresh = True
            continue
        if key in {"p", "P"}:
            state.paused = not state.paused
            continue
        # Unknown key — ignored on purpose (see docstring).


def _default_key_source() -> KeySource:
    """Return a production key source that reads one byte at a time from stdin.

    POSIX-only (termios + tty). On Windows or under a non-TTY stdin
    (e.g. a CI runner with stdin redirected from /dev/null) this returns
    a coroutine that immediately yields ``None`` and exits — the
    dashboard then runs in polling-only mode without a hotkey listener,
    and the operator quits via SIGINT.

    Why ``asyncio.to_thread`` instead of ``loop.add_reader``?
        ``add_reader`` requires the file descriptor to be in non-blocking
        mode and assumes one read per ready notification. With a TTY in
        raw mode, that interacts poorly with terminal-control sequences
        (escape codes arrive in multi-byte bursts and we'd need to
        re-implement the buffering logic). ``to_thread`` keeps the
        single-byte read in a worker thread, which is fine for a
        once-a-second cadence — we're not optimising for thousands of
        keystrokes per second.
    """
    if not _HAS_TERMIOS:
        return _no_op_key_source

    stdin_fd = _resolve_stdin_fd()
    if stdin_fd is None:

        async def _no_keys_no_tty() -> str | None:
            return None

        return _no_keys_no_tty

    return _make_termios_key_source(stdin_fd)


async def _no_op_key_source() -> str | None:
    """Always-``None`` key source (Windows fallback / no-TTY fallback).

    Returning ``None`` once is enough — the listener exits its loop on
    the first ``None`` so the dashboard runs in polling-only mode for
    the rest of the session.
    """
    return None


def _resolve_stdin_fd() -> int | None:
    """Return the stdin file descriptor if it is a real TTY, else ``None``.

    Three failure modes we tolerate cleanly:

    * stdin was replaced by a non-fd object (e.g. ``StringIO`` under
      pytest's ``capsys``) — ``fileno()`` raises ``UnsupportedOperation``.
    * stdin is a pipe / file (CI runner with ``< input.txt``) — ``isatty``
      returns False and we treat it as "no interactive TTY".
    * stdin file descriptor is closed / detached — ``fileno`` raises
      ``ValueError``.

    Returning ``None`` triggers :func:`_default_key_source` to substitute
    the no-op key source so the dashboard still launches.
    """
    try:
        fd = sys.stdin.fileno()
    except (io.UnsupportedOperation, AttributeError, ValueError):
        return None
    try:
        if not os.isatty(fd):
            return None
    except OSError:
        return None
    return fd


def _make_termios_key_source(fd: int) -> KeySource:
    """Build a key-reading coroutine bound to ``fd`` with termios raw mode.

    The closure captures ``fd`` (avoiding a global) and the saved
    terminal attrs so a clean teardown restores the operator's shell on
    exit. We restore on every read failure so a transient EOF doesn't
    leave the terminal in raw mode for the rest of the operator's
    session.

    ``read(1)`` blocks the worker thread until a byte arrives, which is
    exactly what we want for the once-a-second cadence the dashboard
    runs at. The decode falls back to Latin-1 — a malformed UTF-8 byte
    can't crash the listener; it'll surface as an "unknown key" and be
    ignored by the hotkey switch.
    """
    if termios is None or tty is None:  # pragma: no cover — guarded by _HAS_TERMIOS
        return _no_op_key_source

    saved_attrs = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    async def _read_one_key() -> str | None:
        def _blocking_read() -> str | None:
            try:
                byte = os.read(fd, 1)
            except OSError:
                return None
            if not byte:
                return None
            try:
                return byte.decode("utf-8")
            except UnicodeDecodeError:
                return byte.decode("latin-1")

        try:
            return await asyncio.to_thread(_blocking_read)
        except asyncio.CancelledError:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(fd, termios.TCSADRAIN, saved_attrs)
            raise

    return _read_one_key


def _should_use_color(*, no_color: bool) -> bool:
    """Decide whether to emit ANSI colour sequences.

    Three signals, in priority order — same contract as
    :func:`whilly.cli.plan._should_use_color`:

    1. ``--no-color`` (``no_color=True``) → always plain.
    2. ``NO_COLOR`` env var set (any value) → plain. Honours the
       informal cross-tool convention at https://no-color.org so
       operators don't have to special-case Whilly in their dotfiles.
    3. Otherwise: ``sys.stdout.isatty()``. Pipe-to-file or pipe-to-less
       gets plain ASCII; an interactive terminal gets colour.

    ``getattr`` with a falsy default tolerates non-stream stdouts (e.g.
    pytest's ``capsys`` substitutes a buffer that doesn't always
    implement ``isatty``).
    """
    if no_color:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (io.UnsupportedOperation, ValueError):
        return False
