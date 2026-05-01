"""``whilly run`` subcommand — local worker entry point (TASK-019c, PRD FR-1.6).

This is the composition root that wires the v4 local-worker stack into a
single shell command:

* :func:`whilly.adapters.db.create_pool` opens an asyncpg pool against
  ``WHILLY_DATABASE_URL`` (same env var the import/export commands use, so
  operators only set one DSN).
* :class:`whilly.adapters.db.TaskRepository` adapts the pool for the worker.
* :func:`whilly.adapters.runner.run_task` is the production agent runner —
  the Claude CLI subprocess wrapper from TASK-017b. Tests substitute a stub
  via :func:`run_run_command`'s ``runner_factory`` parameter so unit tests
  exercise the plumbing without needing a ``claude`` binary.
* :func:`whilly.worker.run_worker` is the heartbeat + signal-handler shell
  around :func:`whilly.worker.local.run_local_worker`.

Why this lives in :mod:`whilly.cli` (an adapter), not :mod:`whilly.worker`
-------------------------------------------------------------------------
:mod:`whilly.worker` is itself a composer — it stitches the pure
:mod:`whilly.core` domain to the I/O adapters. Putting argparse and DSN
parsing in there would muddy the layering: ``whilly.worker.run_worker``
would no longer be reusable from a future remote control plane (the
control-plane app in TASK-021a is the same shape — pool + repository +
loop — but driven by FastAPI, not argparse). Keeping CLI parsing here
preserves :mod:`whilly.worker` as a parsing-free orchestration layer.

Plan loading reuses :mod:`whilly.cli.plan`
------------------------------------------
The SELECT used to materialise a :class:`Plan` from Postgres already lives
in :func:`whilly.cli.plan._select_plan_with_tasks` (TASK-010c). Calling it
here keeps one canonical reader for that table — if the plan schema grows
a column tomorrow, both ``whilly plan export`` and ``whilly run`` pick it
up from the same place. The function is private to ``cli.plan`` by
convention (no ``__all__`` mention) but it is intentionally symmetric with
``_insert_plan_and_tasks`` from import — exactly the kind of seam the run
command needs.

Worker registration
-------------------
The ``workers`` row needs to exist before ``claim_task`` can transition the
``tasks.claimed_by`` FK. Token-based registration (with bearer auth and
``token_hash``) is owned by TASK-021b on the HTTP control plane. The local
worker is colocated with Postgres — bearer auth would be theatre — so we
INSERT a placeholder row with ``ON CONFLICT DO NOTHING``. Idempotent re-
registration lets the operator restart the worker freely without manually
cleaning up the row, and the placeholder ``token_hash`` value (``"local"``)
makes it obvious to anyone reading the table that this worker did not go
through the bootstrap-token flow.

Exit codes
----------
Mirrors :mod:`whilly.cli.plan` so the v4 CLI surface is consistent:

* ``0`` — worker loop returned normally (``max_iterations`` reached, ``stop``
  set, or the plan is exhausted).
* ``2`` — environment failure: ``WHILLY_DATABASE_URL`` unset, plan_id not
  present in the database. The first matches the import/export contract;
  the second matches the AC verbatim ("При отсутствии плана — exit code 2").

There is intentionally no ``EXIT_VALIDATION_ERROR`` (1) path here — argparse
already returns 2 for malformed arguments, and once the plan is loaded the
worker either runs to its termination condition or surfaces a runtime
exception that we let propagate (the supervisor's job to restart). We don't
swallow exceptions to coerce them into exit codes; those are real bugs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import uuid
from collections.abc import Sequence
from typing import Final

from whilly.adapters.db import TaskRepository, close_pool, create_pool
from whilly.adapters.runner import run_task
from whilly.audit import JsonlEventSink
from whilly.cli.plan import _select_plan_with_tasks
from whilly.core.models import WorkerId
from whilly.worker import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_IDLE_WAIT,
    RunnerCallable,
    run_worker,
)
from whilly.worker.local import WorkerStats

__all__ = ["build_run_parser", "run_run_command"]

logger = logging.getLogger(__name__)


# Same env var :mod:`whilly.cli.plan` reads. Single source of truth for the
# v4 CLI's Postgres pointer (PRD A-1).
DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"

# Optional override for the auto-generated worker id. Useful for tests and
# for operators who want a stable identity across restarts.
WORKER_ID_ENV: Final[str] = "WHILLY_WORKER_ID"

# Exit codes — kept aligned with :mod:`whilly.cli.plan` so callers comparing
# against the v4 CLI never see numbering drift between subcommands.
EXIT_OK: Final[int] = 0
EXIT_ENVIRONMENT_ERROR: Final[int] = 2

# Placeholder token recorded in ``workers.token_hash`` for local registration.
# The HTTP control plane (TASK-021b) overwrites this with the bcrypt hash of
# a real bearer token; for the local worker the column exists only to satisfy
# the schema's NOT NULL constraint.
_LOCAL_TOKEN_PLACEHOLDER: Final[str] = "local"

_REGISTER_WORKER_SQL: Final[str] = """
INSERT INTO workers (worker_id, hostname, token_hash)
VALUES ($1, $2, $3)
ON CONFLICT (worker_id) DO UPDATE SET last_heartbeat = NOW()
"""


def build_run_parser() -> argparse.ArgumentParser:
    """Build the ``whilly run ...`` argparse tree.

    Pulled into its own factory for symmetry with :func:`build_plan_parser`
    in :mod:`whilly.cli.plan` — tests can introspect the declared CLI
    surface without invoking the side-effecting handler.
    """
    parser = argparse.ArgumentParser(
        prog="whilly run",
        description="Run a local worker that executes the given plan to completion.",
    )
    parser.add_argument(
        "--plan",
        dest="plan_id",
        required=True,
        help="Plan id to run (matches the 'plan_id' from `whilly plan import`).",
    )
    # The next three flags exist primarily so integration tests can drive
    # the loop deterministically without waiting for the production cadence
    # (30s heartbeat, 1s idle wait). Operators rarely override them.
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Cap the worker loop after N iterations (default: unbounded). "
            "Mostly useful for integration tests and one-shot CI runs that "
            "want a deterministic exit when the plan is exhausted."
        ),
    )
    parser.add_argument(
        "--idle-wait",
        type=float,
        default=None,
        help=(
            "Seconds to sleep when the queue is empty (default: 1.0). "
            "Lower for tight test loops; higher to reduce DB poll pressure."
        ),
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=None,
        help=("Seconds between worker heartbeat ticks (default: 30.0)."),
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help=(
            f"Override the auto-generated worker id (env: {WORKER_ID_ENV}). "
            "Defaults to '<hostname>-<short-uuid>' so concurrent local "
            "workers on the same host don't collide on the workers PK."
        ),
    )
    return parser


def run_run_command(
    argv: Sequence[str],
    *,
    runner: RunnerCallable | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Entry point for ``whilly run ...``; returns the process exit code.

    ``runner`` is an injection seam: production callers (the v4 CLI
    dispatcher) leave it ``None`` so the production
    :func:`whilly.adapters.runner.run_task` is used. Unit tests pass a stub
    coroutine so the CLI plumbing — argparse, pool lifecycle, registration,
    plan load — is exercised end-to-end without spawning ``claude``.

    ``install_signal_handlers`` mirrors :func:`whilly.worker.main.run_worker`'s
    parameter of the same name. Production CLI invocations always run in
    the main thread of the main interpreter, so the default ``True`` is
    correct. Integration tests that drive the CLI via :func:`asyncio.to_thread`
    (because the test itself runs in pytest-asyncio's loop) pass ``False`` —
    :meth:`asyncio.AbstractEventLoop.add_signal_handler` raises
    ``RuntimeError`` from a worker thread, and bypassing handler installation
    is the cleanest workaround that doesn't require restructuring tests.

    Stays synchronous on the outside so callers (and tests) don't need an
    event loop; the async work is delegated to :func:`_async_run` via
    :func:`asyncio.run`.
    """
    parser = build_run_parser()
    args = parser.parse_args(list(argv))

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly run: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    worker_id = _resolve_worker_id(args.worker_id)
    effective_runner = runner if runner is not None else run_task

    try:
        stats = asyncio.run(
            _async_run(
                dsn=dsn,
                plan_id=args.plan_id,
                worker_id=worker_id,
                runner=effective_runner,
                max_iterations=args.max_iterations,
                idle_wait=args.idle_wait,
                heartbeat_interval=args.heartbeat_interval,
                install_signal_handlers=install_signal_handlers,
            )
        )
    except _PlanNotFoundError as exc:
        print(
            f"whilly run: plan {exc.plan_id!r} not found — check the id matches the "
            "'plan_id' you used at import time, or run `whilly plan import` first.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    print(
        (
            f"whilly run: worker {worker_id!r} finished — "
            f"iterations={stats.iterations} completed={stats.completed} "
            f"failed={stats.failed} idle_polls={stats.idle_polls} "
            f"released_on_shutdown={stats.released_on_shutdown}"
        ),
        file=sys.stderr,
    )
    return EXIT_OK


def _resolve_worker_id(cli_override: str | None) -> WorkerId:
    """Pick the worker id; CLI flag > env > auto-generated.

    Auto-generated form is ``<hostname>-<8-char-uuid-prefix>``: hostname so
    operators can correlate workers with hosts without consulting the
    ``workers`` table, short uuid suffix so two workers on the same host
    don't collide on the PK. We deliberately don't expose the full UUID —
    eight hex chars give 4B distinct ids, which is plenty for the lifetime
    of a single deployment, and the shorter id reads cleanly in logs.
    """
    if cli_override:
        return cli_override
    env_override = os.environ.get(WORKER_ID_ENV)
    if env_override:
        return env_override
    suffix = uuid.uuid4().hex[:8]
    return f"{socket.gethostname()}-{suffix}"


class _PlanNotFoundError(Exception):
    """Internal signal that the requested plan_id is absent from Postgres.

    Raised inside :func:`_async_run` and caught at the sync boundary in
    :func:`run_run_command` so the caller can map it to ``EXIT_ENVIRONMENT_ERROR``
    without an ``Optional[stats]`` return type. We keep it module-private
    because the only path that produces it is also the only path that
    consumes it — exposing it in ``__all__`` would invite callers to start
    catching it from elsewhere.
    """

    def __init__(self, plan_id: str) -> None:
        super().__init__(plan_id)
        self.plan_id = plan_id


async def _async_run(
    *,
    dsn: str,
    plan_id: str,
    worker_id: WorkerId,
    runner: RunnerCallable,
    max_iterations: int | None,
    idle_wait: float | None,
    heartbeat_interval: float | None,
    install_signal_handlers: bool = True,
) -> WorkerStats:
    """Open the pool, register the worker, fetch the plan, run the loop.

    Pool lifecycle is local to this call — same pattern as
    :func:`whilly.cli.plan._async_import` / ``_async_export``. The
    ``finally`` always runs :func:`close_pool` so a crash inside
    ``run_worker`` (or a SIGTERM caught by its signal handlers) still
    drains connections to Postgres.

    The plan SELECT happens *inside* the pool block so we get a clean
    "plan missing" diagnostic without having to poke at SQL outside the
    repository abstractions. The worker registration INSERT is also kept
    here — it's not part of the worker loop's contract; the loop assumes
    the row already exists (FK on ``tasks.claimed_by``).
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            result = await _select_plan_with_tasks(conn, plan_id)
            if result is None:
                raise _PlanNotFoundError(plan_id)
            plan, _tasks = result
            await conn.execute(
                _REGISTER_WORKER_SQL,
                worker_id,
                socket.gethostname(),
                _LOCAL_TOKEN_PLACEHOLDER,
            )
        logger.info(
            "whilly run: registered worker=%s plan=%s tasks=%d",
            worker_id,
            plan.id,
            len(_tasks),
        )

        # Attach a JSONL audit sink so every CLAIM / START / COMPLETE /
        # FAIL / RELEASE / RESET / task.skipped row written by the
        # repository is also mirrored as one line into
        # ``whilly_logs/whilly_events.jsonl`` (VAL-CROSS-BACKCOMPAT-907).
        # The sink resolves its directory from ``WHILLY_LOG_DIR`` (env)
        # or the project default ``whilly_logs/``. Failures to write
        # are logged but never raised, so the orchestrator stays
        # functional on read-only filesystems / disk-full hosts.
        repo = TaskRepository(pool, jsonl_sink=JsonlEventSink())
        return await run_worker(
            repo,
            runner,
            plan,
            worker_id,
            idle_wait=idle_wait if idle_wait is not None else DEFAULT_IDLE_WAIT,
            heartbeat_interval=(heartbeat_interval if heartbeat_interval is not None else DEFAULT_HEARTBEAT_INTERVAL),
            max_iterations=max_iterations,
            install_signal_handlers=install_signal_handlers,
        )
    finally:
        await close_pool(pool)
