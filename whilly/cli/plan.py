"""``whilly plan`` subcommand surface (PRD FR-2.5, FR-3.1, TASK-010b / TASK-010c / TASK-015).

Exposes the three halves of the plan-IO + visualisation surface:

* ``whilly plan import <plan_file>`` (TASK-010b) — read a v4 plan JSON,
  reject cycles, and persist the plan + tasks to Postgres in a single
  transaction.
* ``whilly plan export <plan_id>`` (TASK-010c) — fetch plan + tasks
  from Postgres and emit canonical v4 JSON to stdout. Designed to
  round-trip with ``import``: ``import → export → import`` is a no-op
  because the export uses :func:`~whilly.adapters.filesystem.plan_io.serialize_plan`,
  which emits exactly the canonical fields :func:`parse_plan` consumes.
* ``whilly plan show <plan_id>`` (TASK-015, this commit) — fetch plan +
  tasks from Postgres and render an ASCII dependency graph with colored
  status badges to stdout. Cycles surface as ``Cycle detected: A → B →
  C → A`` and exit ``1``. The renderer is a pure function
  (:func:`render_plan_graph`) so a snapshot unit test can pin the layout
  without booting Postgres; the CLI handler is the thin shell that
  composes the SELECT path (shared with ``export``) with the renderer.

Layering and side-effect surface
--------------------------------
This module is the canonical *adapter* composition site for the plan
import path — it stitches together three lower-level components without
adding business logic of its own:

* :func:`whilly.adapters.filesystem.plan_io.parse_plan` — pure parser; the
  only filesystem read in this flow lives there.
* :func:`whilly.core.scheduler.detect_cycles` — pure validation; runs on
  the in-memory :class:`~whilly.core.models.Plan` *before* any database
  side effect, so a malformed plan can never produce partial inserts.
* :class:`whilly.adapters.db.repository.TaskRepository`'s pool — owns the
  asyncpg connection. We acquire one connection, open one transaction, and
  do the entire INSERT batch inside it (see "Atomicity" below).

Idempotence and atomicity
-------------------------
The AC for TASK-010b reads:

* "Импорт идемпотентен по plan_id (повторный запуск не создаёт дублей)"
* "Импорт всех задач — в одной транзакции (rollback при ошибке)"

We satisfy both with one SQL idiom: ``INSERT ... ON CONFLICT (id) DO
NOTHING`` for the ``plans`` row and every ``tasks`` row, all wrapped in a
single :meth:`asyncpg.Connection.transaction`. Why each detail matters:

* **DO NOTHING (vs. upsert).** A re-import must not clobber state the
  workers have accumulated in the meantime — claimed_by, version, status
  promoted from PENDING to DONE. ``DO NOTHING`` preserves that state while
  still letting the operator add brand-new tasks on a subsequent run by
  replaying the same import command. Upsert would silently roll a DONE
  task back to PENDING, which is exactly the kind of foot-gun the
  optimistic-locking design (PRD FR-2.4) exists to prevent.
* **Plan-row first, tasks second, all in one transaction.** A failed
  task INSERT (e.g. ``CHECK`` constraint violation on ``priority``)
  rolls back the plan INSERT too, so a partially-populated plan never
  appears in the database. The CTE-style alternative (one big INSERT
  with ``unnest($1, $2, ...)``) would also work but is harder to
  diagnose when one task fails — per-row inserts surface the offending
  ``task.id`` in the asyncpg exception path.
* **Cycle detection runs before the pool is even opened.** Connecting to
  Postgres just to fail validation is wasteful; running
  :func:`detect_cycles` on the parsed plan keeps the bad-input path
  fast (no DB round-trip) and keeps the SQL path uncoupled from the
  cycle algorithm.

Exit codes
----------
The CLI follows the conventional 0/1/2 split, mirroring the rest of the
v4 CLI tasks:

* ``0`` — success (plan + N tasks committed; or canonical JSON printed).
* ``1`` — validation failure: malformed JSON, missing required field,
  cycle in dependency graph. ``argparse`` errors also surface as ``2``
  (its own convention) but the *plan-level* validation we do explicitly
  uses ``1`` so cycle detection has a stable, testable exit code (PRD
  SC-4: "Цикл в зависимостях → exit code 1 с указанием цепочки").
* ``2`` — environment failure: ``WHILLY_DATABASE_URL`` unset, file not
  found, **or plan_id missing in the database** (TASK-010c AC:
  "Несуществующий plan_id → exit 2"). Treating "missing plan" as an
  environment failure (rather than validation) matches the AC verbatim
  and keeps the export path symmetric with the import path's
  "DSN missing → exit 2": both are "the world isn't set up the way the
  CLI expected", as opposed to "the input shape was wrong".
"""

from __future__ import annotations

import argparse
import asyncio
import decimal
import io
import json
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from decimal import Decimal

import asyncpg
from rich.console import Console

from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.db.repository import TaskRepository, VersionConflictError
from whilly.adapters.filesystem.plan_io import PlanParseError, parse_plan, serialize_plan
from whilly.core.gates import GateVerdictKind, evaluate_decision_gate
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus
from whilly.core.scheduler import detect_cycles

__all__ = ["build_plan_parser", "render_plan_graph", "run_plan_command"]

logger = logging.getLogger(__name__)


# Exit codes (kept here as constants so test imports can compare against the
# same symbols rather than literal numbers — protects against silent drift if
# we ever renumber them).
EXIT_OK: int = 0
EXIT_VALIDATION_ERROR: int = 1
EXIT_ENVIRONMENT_ERROR: int = 2

# Env var that drives the asyncpg pool — same name Alembic env.py uses
# (TASK-007), so operators only have to set one variable.
DATABASE_URL_ENV: str = "WHILLY_DATABASE_URL"


# ON CONFLICT (id) DO NOTHING handles the "повторный запуск не создаёт
# дублей" half of the AC; the surrounding ``async with conn.transaction()``
# in :func:`_async_import` handles the "в одной транзакции" half.
_INSERT_PLAN_SQL: str = """
INSERT INTO plans (id, name)
VALUES ($1, $2)
ON CONFLICT (id) DO NOTHING
"""


# ``status`` and ``priority`` are TEXT columns with CHECK constraints in the
# 001 migration, so passing the enum's ``.value`` (a string) is correct and
# does not need a Postgres ENUM type.
#
# ``::jsonb`` casts on the array columns let us pass plain ``json.dumps`` text
# without registering a custom JSONB codec on the pool — same pattern as
# repository.py's _INSERT_EVENT_SQL.
_INSERT_TASK_SQL: str = """
INSERT INTO tasks (
    id,
    plan_id,
    status,
    dependencies,
    key_files,
    priority,
    description,
    acceptance_criteria,
    test_steps,
    prd_requirement,
    version
)
VALUES (
    $1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8::jsonb, $9::jsonb, $10, $11
)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""


# Audit-event INSERT for the per-task ``task.created`` row emitted by
# ``whilly plan apply`` (M3 fix-feature). Mirrors the shape of the
# repository-side ``_INSERT_TASK_EVENT_WITH_PLAN_SQL``: BOTH ``task_id``
# and ``plan_id`` populated so the cross-flow evidence query
# (``WHERE plan_id=$1 GROUP BY event_type``) sees the row.
#
# Emission is gated on the task INSERT actually returning a row id
# (i.e. ON CONFLICT DO NOTHING did NOT fire) so a re-run of ``apply``
# against the same plan emits zero new ``task.created`` rows. This is
# the gate VAL-CROSS-005 idempotency relies on for this event type.
_INSERT_TASK_CREATED_EVENT_SQL: str = """
INSERT INTO events (task_id, plan_id, event_type, payload)
VALUES ($1, $2, 'task.created', $3::jsonb)
"""


# Audit-event INSERT for the per-apply ``plan.applied`` sentinel row
# (M3 fix-feature). One row per ``whilly plan apply`` /
# ``apply --strict`` invocation, written *after* the strict gate
# iteration completes so its ``created_at`` is strictly later than
# any ``task.skipped`` row from the same call. Reuses
# :data:`whilly.adapters.db.repository._INSERT_PLAN_EVENT_SQL`'s shape
# locally to keep the SQL string in this module's adapter site (the
# repository-level constant is imported only via the repository's
# public surface; copying the literal here keeps the module
# self-contained).
_INSERT_PLAN_APPLIED_EVENT_SQL: str = """
INSERT INTO events (task_id, plan_id, event_type, payload)
VALUES (NULL, $1, 'plan.applied', $2::jsonb)
"""


# Symmetric counterparts of the INSERT statements above (TASK-010c). Pulled
# out of :func:`_async_export` for the same reason the INSERTs are constants:
# every operator-visible SQL string lives in module-scope so a schema review
# can ``grep`` for table names without descending into function bodies.
_SELECT_PLAN_SQL: str = """
SELECT id, name
FROM plans
WHERE id = $1
"""


# Stable order: ``id`` ASC. The export must be deterministic so the
# round-trip ``import → export → import`` test in test_plan_io.py compares
# ``Plan == Plan`` without sort-order noise. The schema does not record
# original insertion order (no ``position`` column) and a v4 plan's tasks
# are a *DAG*, not a sequence — sorting by id is the canonical
# tiebreaker the rest of the v4 codebase already uses (see
# ``_CLAIM_SQL``'s ``ORDER BY ..., id`` in repository.py).
_SELECT_TASKS_SQL: str = """
SELECT
    id,
    status,
    dependencies,
    key_files,
    priority,
    description,
    acceptance_criteria,
    test_steps,
    prd_requirement,
    version
FROM tasks
WHERE plan_id = $1
ORDER BY id
"""


def build_plan_parser() -> argparse.ArgumentParser:
    """Build (but do not parse) the ``whilly plan ...`` argparse tree.

    Pulled into its own factory so future subcommands (``export``, ``show``)
    plug into the same parser without :func:`run_plan_command` growing a
    forest of conditionals. Tests can also call this to introspect the
    declared CLI surface without invoking the side-effecting handler.
    """
    parser = argparse.ArgumentParser(
        prog="whilly plan",
        description="Manage v4 plans (import / export / show).",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")

    # ── plan create (TASK-102) ──────────────────────────────────────────
    # Empty-plan creation primitive used by the budget-guard surface and
    # by tests / operators who want a plan row in Postgres without
    # importing a tasks JSON file. Distinct from ``plan import`` because
    # the budget can only be set at create time today (a future
    # ``plan set --budget`` could land separately).
    p_create = sub.add_parser(
        "create",
        help="Create a new plan row (optionally with a budget cap).",
    )
    p_create.add_argument(
        "--id",
        dest="plan_id",
        required=True,
        help="Plan id (matches the 'plan_id' field used by import/export).",
    )
    p_create.add_argument(
        "--name",
        dest="name",
        required=True,
        help="Human-readable plan name (shown in `plan show`).",
    )
    p_create.add_argument(
        "--budget",
        dest="budget",
        default=None,
        help=(
            "Spend cap in USD (Decimal). When set, the orchestrator refuses "
            "to claim further tasks once plans.spent_usd >= this value, and "
            "emits a 'plan.budget_exceeded' sentinel event on crossing. "
            "Omit for unlimited (NULL stored)."
        ),
    )

    # ── plan import ──────────────────────────────────────────────────────
    p_import = sub.add_parser(
        "import",
        help="Import a v4 plan JSON file into Postgres (idempotent on plan_id).",
    )
    p_import.add_argument(
        "plan_file",
        help="Path to a v4 plan JSON file (see examples/sample_plan.json).",
    )

    # ── plan export ──────────────────────────────────────────────────────
    # Inverse of ``plan import``. The output is canonical v4 JSON
    # (whatever :func:`serialize_plan` emits), so the round-trip
    # ``import → export → import`` is a no-op.
    p_export = sub.add_parser(
        "export",
        help="Export a plan + tasks from Postgres as canonical v4 JSON to stdout.",
    )
    p_export.add_argument(
        "plan_id",
        help="Plan id to export (matches the 'plan_id' field in the original JSON).",
    )

    # ── plan show ────────────────────────────────────────────────────────
    # Visualisation counterpart to ``export``: same SELECT path, different
    # output. ``--no-color`` is exposed so CI / pipe-to-file callers can
    # request plain ASCII; the default auto-detects via
    # :attr:`sys.stdout.isatty`. See :func:`render_plan_graph` for the
    # layout contract that the snapshot test pins down.
    p_show = sub.add_parser(
        "show",
        help="Print an ASCII dependency graph of a plan from Postgres (TASK-015).",
    )
    p_show.add_argument(
        "plan_id",
        help="Plan id to show (matches the 'plan_id' field in the original JSON).",
    )
    p_show.add_argument(
        "--no-color",
        action="store_true",
        help="Force plain ASCII output (default: auto-detect via isatty).",
    )

    # ── plan reset ───────────────────────────────────────────────────────
    # Operator-facing recovery primitive (TASK-103). Two modes:
    #   --keep-tasks → soft: tasks → PENDING, events purged, RESET row
    #                  per task with reason=manual_reset.
    #   --hard       → DELETE plan row; cascades to tasks + events.
    # Without --yes, the handler prompts y/N (interactive only —
    # non-TTY callers pipeline `--yes` to skip the prompt).
    p_reset = sub.add_parser(
        "reset",
        help="Reset a plan to PENDING (--keep-tasks) or DELETE it (--hard).",
    )
    p_reset.add_argument(
        "plan_id",
        help="Plan id to reset (matches the 'plan_id' field in the original JSON).",
    )
    mode_group = p_reset.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--keep-tasks",
        action="store_true",
        help="Soft reset: tasks → PENDING, events purged, RESET audit row per task.",
    )
    mode_group.add_argument(
        "--hard",
        action="store_true",
        help="Hard reset: DELETE plan row (cascades to tasks + events).",
    )
    p_reset.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive y/N confirmation prompt.",
    )

    # ── plan apply ───────────────────────────────────────────────────────
    # ``plan apply <plan_file>`` is the import + decision-gate composite
    # (TASK-104c). Without ``--strict`` it behaves like ``plan import``
    # plus a structured warning per task that fails the
    # :func:`whilly.core.gates.evaluate_decision_gate` rules — every
    # task is still imported into Postgres. With ``--strict`` it
    # additionally calls :meth:`TaskRepository.skip_task` on each
    # REJECT verdict so the offending row lands in the DB as
    # ``SKIPPED`` (with a ``SKIP`` event row carrying the missing
    # field labels) before any worker gets a chance to claim it.
    #
    # The ``--strict`` slot is between :func:`detect_cycles` and the
    # eventual agent-pool open: a cyclic plan still fails at exit 1
    # before the gate even runs (see VAL-GATES-019 for the contract).
    p_apply = sub.add_parser(
        "apply",
        help=(
            "Import a v4 plan JSON and run the Decision Gate on each task; "
            "with --strict, gate-rejecting tasks are marked SKIPPED."
        ),
    )
    p_apply.add_argument(
        "plan_file",
        help="Path to a v4 plan JSON file (see examples/sample_plan.json).",
    )
    p_apply.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Enable strict Decision Gate: tasks whose description, "
            "acceptance_criteria, or test_steps fail the pure gate check are "
            "transitioned to SKIPPED via repo.skip_task with reason "
            "'decision_gate_failed'. Without --strict the gate only emits a "
            "structured warning to stderr and leaves the task PENDING."
        ),
    )
    return parser


def run_plan_command(argv: Sequence[str]) -> int:
    """Entry point for ``whilly plan ...``; returns the process exit code.

    Stays synchronous on the outside so callers (and tests) don't need an
    event loop — the async parts are delegated to :func:`_async_import` via
    :func:`asyncio.run`.
    """
    parser = build_plan_parser()
    args = parser.parse_args(list(argv))
    if args.action == "create":
        return _run_create(args.plan_id, name=args.name, budget=args.budget)
    if args.action == "import":
        return _run_import(args.plan_file)
    if args.action == "export":
        return _run_export(args.plan_id)
    if args.action == "show":
        return _run_show(args.plan_id, no_color=bool(args.no_color))
    if args.action == "reset":
        return _run_reset(
            args.plan_id,
            keep_tasks=bool(args.keep_tasks),
            hard=bool(args.hard),
            yes=bool(args.yes),
        )
    if args.action == "apply":
        return _run_apply(args.plan_file, strict=bool(args.strict))
    # argparse's ``required=True`` already surfaces a 2-exit on missing
    # action; this branch is defensive for future subcommands added without
    # an explicit handler here.
    parser.error(f"unknown action {args.action!r}")  # noqa: RET503  (no-return)
    return EXIT_ENVIRONMENT_ERROR  # pragma: no cover — argparse SystemExits first


# ── plan create (TASK-102) ────────────────────────────────────────────────


# Insert template for ``whilly plan create``. Uses ``ON CONFLICT (id) DO
# NOTHING`` so re-running the command against an existing plan is
# idempotent at the SQL layer (consistent with ``plan import``); the
# Python wrapper still surfaces a "already exists" warning so the
# operator knows their ``--budget`` argument was not applied to the
# pre-existing row.
_INSERT_PLAN_WITH_BUDGET_SQL: str = """
INSERT INTO plans (id, name, budget_usd)
VALUES ($1, $2, $3)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""


def _parse_budget_arg(raw: str | None) -> Decimal | None:
    """Validate the ``--budget`` flag and convert to :class:`Decimal`.

    Rejects negative / non-numeric values with a single ``ValueError``
    carrying a message safe to pipe to stderr (VAL-BUDGET-012). ``None``
    is returned for ``raw is None`` (operator omitted the flag → NULL =
    unlimited per VAL-BUDGET-011).
    """
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (decimal.InvalidOperation, ValueError) as exc:
        raise ValueError(f"--budget must be a numeric value (e.g. 5.00); got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"--budget must be non-negative; got {value}")
    # Quantise to numeric(10,4) precision so the persisted value matches
    # the column shape (and the post-create echo to stdout shows the
    # canonical form, not the operator's raw input).
    return value.quantize(Decimal("0.0001"))


def _run_create(plan_id: str, *, name: str, budget: str | None) -> int:
    """Implement ``whilly plan create --id <id> --name <name> [--budget <usd>]``.

    1. Validate ``--budget`` (if provided). Bad shape →
       ``EXIT_VALIDATION_ERROR`` with stderr diagnostic naming the
       offending value (VAL-BUDGET-012 evidence).
    2. Read ``WHILLY_DATABASE_URL`` from the environment — missing →
       ``EXIT_ENVIRONMENT_ERROR``.
    3. INSERT the plan row via :data:`_INSERT_PLAN_WITH_BUDGET_SQL`.
       ON CONFLICT (id) DO NOTHING — re-running on an existing id is a
       safe no-op, but we surface a warning to stderr so the operator
       knows their ``--budget`` value was *not* applied to the
       pre-existing row.
    """
    try:
        budget_decimal = _parse_budget_arg(budget)
    except ValueError as exc:
        print(f"whilly plan create: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly plan create: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    inserted = asyncio.run(_async_create(dsn, plan_id, name, budget_decimal))
    if not inserted:
        print(
            f"whilly plan create: warning: plan {plan_id!r} already exists; "
            f"--budget was not applied (use `plan reset --hard` then re-create to change it).",
            file=sys.stderr,
        )
        return EXIT_OK
    budget_label = "unlimited" if budget_decimal is None else f"budget_usd={budget_decimal}"
    print(f"whilly plan create: created plan {plan_id!r} ({name!r}, {budget_label}).")
    return EXIT_OK


async def _async_create(
    dsn: str,
    plan_id: str,
    name: str,
    budget: Decimal | None,
) -> bool:
    """INSERT a plan row; return True iff a fresh row was created.

    Idiomatic short-lived pool lifecycle (matches ``_async_import`` /
    ``_async_export``). Returns False on the ON CONFLICT no-op path so
    the CLI can surface a warning rather than misreporting a no-op as
    a successful create.
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_INSERT_PLAN_WITH_BUDGET_SQL, plan_id, name, budget)
        return row is not None
    finally:
        await close_pool(pool)


def _run_import(plan_file: str) -> int:
    """Implement ``whilly plan import <plan_file>``.

    1. Parse + validate JSON shape via :func:`parse_plan` — bad shape /
       missing required field → ``EXIT_VALIDATION_ERROR``.
    2. Run :func:`detect_cycles` on the in-memory plan — any cycle →
       ``EXIT_VALIDATION_ERROR`` with a human-readable chain printout
       (PRD SC-4).
    3. Read ``WHILLY_DATABASE_URL`` from the environment — missing →
       ``EXIT_ENVIRONMENT_ERROR``.
    4. Hand the parsed (Plan, tasks) pair to :func:`_async_import`, which
       owns the asyncpg pool lifecycle and the single-transaction insert.
    5. Print a one-line success message to stdout (so shell pipelines and
       CI logs surface the import without needing ``--verbose``).

    All error paths print to ``stderr`` so success-path callers can ``> /dev/null``
    without losing diagnostics. Exit codes map to the contract in the
    module docstring.
    """
    try:
        plan, tasks = parse_plan(plan_file)
    except PlanParseError as exc:
        # PlanParseError already includes the source path / task id in the
        # message; we just prefix with the subcommand for context.
        print(f"whilly plan import: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except FileNotFoundError as exc:
        # parse_plan wraps OSError in PlanParseError, but a path that fails
        # other early validation (e.g. empty string) can still raise the
        # bare exception. Treat it as an environment failure: the *file*
        # is the problem, not the JSON shape.
        print(f"whilly plan import: file not found: {exc.filename or plan_file}", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    cycles = detect_cycles(plan)
    if cycles:
        for cycle in cycles:
            print(f"whilly plan import: Cycle detected: {_format_cycle(cycle)}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly plan import: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    asyncio.run(_async_import(dsn, plan, tasks))
    print(f"whilly plan import: imported plan {plan.id!r} ({len(tasks)} task(s)).")
    return EXIT_OK


def _format_cycle(cycle: Sequence[TaskId]) -> str:
    """Render an SCC as ``A → B → C → A`` (or ``A → A`` for a self-loop).

    :func:`whilly.core.scheduler.detect_cycles` returns each SCC sorted by
    ``task.id`` (deterministic) but does *not* preserve the directed edge
    sequence — the membership is what matters for validation. The arrow
    rendering is purely cosmetic, designed to read like a familiar
    cycle notation in CLI output. Self-loops still close the loop visually
    so the user sees that the cycle exists rather than being puzzled by
    a single id with no arrow.
    """
    if len(cycle) == 1:
        node = cycle[0]
        return f"{node} → {node}"
    return " → ".join(cycle) + f" → {cycle[0]}"


async def _async_import(dsn: str, plan: Plan, tasks: Iterable[Task]) -> None:
    """Open a pool, INSERT plan + tasks in one transaction, close the pool.

    Pool lifecycle is local to this call: we don't own a long-lived pool
    in the CLI process because ``whilly plan import`` is a one-shot
    command. :func:`whilly.adapters.db.pool.create_pool` does its own
    ``SELECT 1`` health check on construction so a bad DSN crashes here
    rather than at the first INSERT.

    The ``finally`` always runs :func:`close_pool` even on exception so
    the asyncio shutdown does not leave dangling sockets to Postgres.
    Exceptions from the INSERT path propagate to the caller; the
    transaction context manager rolls the plan + tasks INSERTs back as a
    unit, satisfying the "rollback при ошибке" half of the AC.
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _insert_plan_and_tasks(conn, plan, tasks)
    finally:
        await close_pool(pool)


async def _insert_plan_and_tasks(
    conn: asyncpg.Connection,
    plan: Plan,
    tasks: Iterable[Task],
) -> None:
    """Run the plan INSERT then every task INSERT on ``conn``.

    Pulled out of :func:`_async_import` so tests holding a pre-existing
    pool / connection (e.g. the testcontainers ``db_pool`` fixture) can
    drive the same code path without re-opening the pool just to import a
    plan. The caller owns the transaction; this helper assumes it runs
    inside ``async with conn.transaction()`` and does not commit on its
    own.

    JSONB serialisation goes via :func:`json.dumps` rather than asyncpg's
    automatic codec because we don't register one on the pool (TASK-009a
    deliberately keeps codec policy out of pool.py, see its docstring). The
    ``::jsonb`` casts in :data:`_INSERT_TASK_SQL` accept the resulting
    text without complaint.
    """
    await conn.execute(_INSERT_PLAN_SQL, plan.id, plan.name)
    inserted = 0
    for task in tasks:
        # Use ``fetchval`` on the RETURNING-augmented task INSERT so a
        # row that was actually written returns its id, while a row
        # that hit ``ON CONFLICT (id) DO NOTHING`` returns ``None`` —
        # the latter case is the canonical idempotency path: a re-run
        # of ``whilly plan apply`` against the same plan must NOT emit
        # a duplicate ``task.created`` event for already-imported
        # rows (VAL-CROSS-005).
        new_id = await conn.fetchval(
            _INSERT_TASK_SQL,
            task.id,
            plan.id,
            task.status.value,
            json.dumps(list(task.dependencies)),
            json.dumps(list(task.key_files)),
            task.priority.value,
            task.description,
            json.dumps(list(task.acceptance_criteria)),
            json.dumps(list(task.test_steps)),
            task.prd_requirement,
            task.version,
        )
        if new_id is None:
            # ON CONFLICT (id) DO NOTHING — pre-existing row owned by
            # this plan (idempotent re-run) or by a different plan
            # (cross-plan id collision; the strict-apply path's
            # cross-plan safety guard handles the SKIPPED transition,
            # but here we simply skip the audit emission). No event
            # row written ⇒ ``task.created`` count stays stable across
            # reruns.
            continue
        # M3 fix-feature: emit one ``task.created`` event per
        # newly-inserted task row. The payload carries diagnostic
        # fields (priority, dependencies) that mirror the
        # ``plan.created`` event's diagnostic shape so a downstream
        # observer can render a human-readable line without
        # re-querying ``tasks``. Both ``task_id`` and ``plan_id`` are
        # populated so VAL-CROSS-004's per-plan distribution query
        # (``GROUP BY event_type WHERE plan_id=$1``) sees the row.
        await conn.execute(
            _INSERT_TASK_CREATED_EVENT_SQL,
            new_id,
            plan.id,
            json.dumps(
                {
                    "priority": task.priority.value,
                    "dependencies": list(task.dependencies),
                }
            ),
        )
        inserted += 1
    logger.info("plan import: inserted plan %s with %d task(s)", plan.id, inserted)


def _run_export(plan_id: str) -> int:
    """Implement ``whilly plan export <plan_id>``.

    Symmetric with :func:`_run_import`:

    1. Read ``WHILLY_DATABASE_URL`` from the environment — missing →
       ``EXIT_ENVIRONMENT_ERROR``.
    2. Hand the ``plan_id`` to :func:`_async_export`, which owns the pool
       lifecycle and the SELECT round-trip.
    3. ``None`` result → plan absent → ``EXIT_ENVIRONMENT_ERROR`` with a
       message that names the missing id (PRD AC for TASK-010c:
       "Несуществующий plan_id → exit 2 с понятным сообщением").
    4. Otherwise serialise via :func:`serialize_plan` (same writer the
       round-trip test compares against) and print to stdout.

    The success path writes to ``stdout`` so callers can pipe the output
    (``> /tmp/exported.json``); the message line stays on ``stderr`` —
    redirecting stdout to a file then importing the result must not pick
    up any "imported N tasks" preamble.
    """
    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly plan export: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    result = asyncio.run(_async_export(dsn, plan_id))
    if result is None:
        print(
            f"whilly plan export: plan {plan_id!r} not found — check the id matches the "
            "'plan_id' you used at import time.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    plan, tasks = result
    payload = serialize_plan(plan, tasks)
    # ``sort_keys=True`` + ``indent=2`` keeps the output deterministic
    # *and* human-readable. Determinism matters for the round-trip test
    # (two consecutive exports must be byte-identical) and for ``diff``-ing
    # exports across DB snapshots in operations.
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    print(
        f"whilly plan export: exported plan {plan.id!r} ({len(tasks)} task(s)).",
        file=sys.stderr,
    )
    return EXIT_OK


async def _async_export(dsn: str, plan_id: str) -> tuple[Plan, list[Task]] | None:
    """Open a pool, SELECT plan + tasks, return them as core models or ``None``.

    Mirrors :func:`_async_import` lifecycle: short-lived pool, ``SELECT 1``
    health check on construction, ``finally`` always closes. We deliberately
    do **not** wrap the SELECTs in an explicit transaction:

    * Postgres puts every individual statement in an implicit transaction
      anyway, so the plan SELECT and tasks SELECT each see a consistent
      snapshot internally.
    * A multi-statement transaction would be marginally stronger (the plan
      and tasks SELECTs would share one MVCC snapshot, ruling out a race
      where a parallel import inserts a task between the two SELECTs and
      we miss it). But the export command is a one-shot CLI invocation —
      the operational scenarios that care about consistency (CI snapshot
      capture, debugging) all pause writes anyway. The simpler code path
      pays for itself in less SQL noise.

    Returns ``None`` when the plan row is absent so the caller can map that
    to a clean ``EXIT_ENVIRONMENT_ERROR`` without raising. We intentionally
    do *not* check task count: a plan with zero tasks is legitimate (an
    empty plan still has an entry in the ``plans`` table after import) and
    must round-trip cleanly.
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            return await _select_plan_with_tasks(conn, plan_id)
    finally:
        await close_pool(pool)


async def _select_plan_with_tasks(
    conn: asyncpg.Connection,
    plan_id: str,
) -> tuple[Plan, list[Task]] | None:
    """SELECT one plan + its tasks; return ``(Plan, list[Task])`` or ``None``.

    Pulled out of :func:`_async_export` for the same reason
    :func:`_insert_plan_and_tasks` is pulled out of :func:`_async_import`:
    integration tests holding a pre-existing pool / connection (e.g. the
    testcontainers ``db_pool`` fixture in ``tests/conftest.py``) can drive
    the same code path without re-opening the pool just to export.

    The mapping from ``tasks`` rows to :class:`Task` value objects is
    inlined here rather than imported from
    :mod:`whilly.adapters.db.repository`. The repository's ``_row_to_task``
    is a private helper tied to the claim/complete/fail SQL there; copying
    the eight-line conversion keeps the export path self-contained next to
    its symmetric INSERT counterpart in this file. JSONB columns come back
    as ``str`` (raw JSON text) by default — ``_decode_jsonb`` parses with
    stdlib :mod:`json` so this code works whether or not a future codec is
    registered on the pool.
    """
    plan_row = await conn.fetchrow(_SELECT_PLAN_SQL, plan_id)
    if plan_row is None:
        return None

    task_rows = await conn.fetch(_SELECT_TASKS_SQL, plan_id)
    tasks = [_row_to_task(row) for row in task_rows]
    plan = Plan(id=plan_row["id"], name=plan_row["name"], tasks=tuple(tasks))
    logger.info("plan export: fetched plan %s with %d task(s)", plan.id, len(tasks))
    return plan, tasks


def _decode_jsonb(raw: object) -> list[str]:
    """Return ``raw`` (a JSONB column value from asyncpg) as a list of strings.

    asyncpg returns JSONB as ``str`` (raw JSON text) unless a codec has been
    registered on the connection — :mod:`whilly.adapters.db.pool` deliberately
    does not register one (TASK-009a). We parse with stdlib :mod:`json` here
    so the export path stays decoupled from that decision.

    All four JSONB columns we read (``dependencies``, ``key_files``,
    ``acceptance_criteria``, ``test_steps``) are arrays of strings by
    convention (the schema's ``DEFAULT '[]'::jsonb`` is the canonical empty
    value). A non-list value would mean someone bypassed the import path —
    we surface a :class:`TypeError` rather than silently returning ``[]`` so
    that data corruption is loud.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        decoded: list[object] = raw
    elif isinstance(raw, str):
        decoded_any = json.loads(raw)
        if not isinstance(decoded_any, list):
            raise TypeError(f"expected JSONB array, got {type(decoded_any).__name__}: {decoded_any!r}")
        decoded = decoded_any
    else:
        raise TypeError(f"unexpected JSONB column type {type(raw).__name__}: {raw!r}")
    out: list[str] = []
    for item in decoded:
        if not isinstance(item, str):
            raise TypeError(f"JSONB array contains non-string element {item!r}")
        out.append(item)
    return out


def _row_to_task(row: asyncpg.Record) -> Task:
    """Map one ``tasks`` row to the immutable :class:`Task` value object.

    Local mirror of :func:`whilly.adapters.db.repository._row_to_task` —
    intentionally duplicated rather than imported from a private symbol so
    the export pipeline in this file stays paired with its symmetric INSERT
    counterpart. Empty / missing JSONB arrays normalise to ``()`` so
    :func:`serialize_plan` emits ``[]`` deterministically.
    """
    return Task(
        id=row["id"],
        status=TaskStatus(row["status"]),
        dependencies=tuple(_decode_jsonb(row["dependencies"])),
        key_files=tuple(_decode_jsonb(row["key_files"])),
        priority=Priority(row["priority"]),
        description=row["description"],
        acceptance_criteria=tuple(_decode_jsonb(row["acceptance_criteria"])),
        test_steps=tuple(_decode_jsonb(row["test_steps"])),
        prd_requirement=row["prd_requirement"],
        version=row["version"],
    )


# ── plan show ────────────────────────────────────────────────────────────
#
# Status → Rich color mapping. Lifted out of the renderer so a future
# dashboard (TASK-027) can reuse the same palette without re-deriving it
# from the AC text. The PRD-mandated colors are PENDING=grey,
# IN_PROGRESS=yellow, DONE=green, FAILED=red. CLAIMED gets cyan to
# distinguish "owned but not yet started" from PENDING (helpful when a
# claim is wedged); SKIPPED is dim because it's a non-error terminal
# state that shouldn't draw the eye like FAILED.
_STATUS_COLOR: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "grey50",
    TaskStatus.CLAIMED: "cyan",
    TaskStatus.IN_PROGRESS: "yellow",
    TaskStatus.DONE: "green",
    TaskStatus.FAILED: "red",
    TaskStatus.SKIPPED: "grey37",
}


def render_plan_graph(
    plan: Plan,
    tasks: Sequence[Task],
    cycles: Sequence[Sequence[TaskId]],
    *,
    use_color: bool,
) -> str:
    """Return the ASCII dependency graph for ``plan`` as a single string.

    Pure function: takes core models, returns text. No I/O, no DB, no
    process state. The CLI handler in :func:`_run_show` composes this with
    the SELECT path; the snapshot test in
    :mod:`tests.integration.test_plan_show` pins the layout by passing
    fabricated :class:`Plan` / :class:`Task` instances directly.

    Layout
    ------
    The graph is rendered top-down, ordered by topological depth so a
    reader's eye flows from "no deps" at the top to "depends on
    everything" at the bottom. Within each depth bucket, tasks are sorted
    by ``id`` (lexicographic) — same tie-break Kahn's frontier and
    Tarjan's DFS use elsewhere in :mod:`whilly.core.scheduler`, so the
    output is byte-identical across reruns.

    Format::

        Plan: <plan_id> — <project_name>
        ─────────────────────────────────
        [STATUS    ] task_id  (priority)
            └─ depends on: dep1, dep2
        ...

        Summary: N tasks · PENDING=a · CLAIMED=b · IN_PROGRESS=c · DONE=d · FAILED=e · SKIPPED=f

    The status badge is fixed-width (10 chars) so the dependency arrows
    line up across rows of mixed status. Tasks with no dependencies omit
    the ``└─`` arrow line entirely (less visual noise for leaf-level
    tasks). Cycles are reported *above* the graph so they're the first
    thing the operator sees, but the graph still renders so the operator
    can see *where* the cycle is in context.

    Determinism
    -----------
    Two invocations on equal inputs return ``==``-equal strings. The
    topological depth bucketing falls back to ``len(plan.tasks) + 1`` for
    nodes that participate in cycles (so they don't get omitted from the
    output) — this is a stable sentinel because we always sort by
    ``(depth, task.id)`` and depth alone never decides ordering inside a
    cycle (the id tiebreaker takes over).

    use_color
    ---------
    When ``True``, status badges are wrapped in Rich color tags
    (``[green]DONE[/green]``) so the surrounding :class:`Console` paints
    them in the operator's terminal. When ``False``, the badges are
    plain text — that's the mode the snapshot test uses so the assertion
    is a literal byte equality without ANSI escape gymnastics.
    """
    by_id: dict[TaskId, Task] = {task.id: task for task in tasks}
    depth = _compute_topological_depth(tasks, by_id)
    sentinel = max(depth.values(), default=0) + 1
    ordered = sorted(tasks, key=lambda t: (depth.get(t.id, sentinel), t.id))

    cycle_lines: list[str] = []
    for cycle in cycles:
        cycle_lines.append(f"Cycle detected: {_format_cycle(tuple(cycle))}")

    title = f"Plan: {plan.id} — {plan.name}"
    rule = "─" * max(len(title), 40)

    body_lines: list[str] = [title, rule]

    # Pre-compute the longest status label so the badge column width
    # is uniform regardless of which statuses appear in the plan. We
    # bound on the enum (not the actual rows) so the layout stays
    # consistent across exports of small plans.
    badge_width = max(len(s.value) for s in TaskStatus)

    for task in ordered:
        body_lines.append(_render_task_line(task, badge_width=badge_width, use_color=use_color))
        if task.dependencies:
            # Filter to in-plan deps only — cross-plan deps are silently
            # ignored throughout the scheduler (see scheduler.py docstring
            # on cross-plan refs); rendering them here would lie about the
            # actual edges Postgres will respect.
            in_plan_deps = [dep for dep in task.dependencies if dep in by_id]
            if in_plan_deps:
                body_lines.append(f"    └─ depends on: {', '.join(in_plan_deps)}")

    summary_counts = _count_statuses(tasks)
    summary = "Summary: " + " · ".join(
        [f"{len(tasks)} tasks", *(f"{s.value}={summary_counts[s]}" for s in TaskStatus if summary_counts[s] > 0)]
    )

    parts: list[str] = []
    if cycle_lines:
        parts.extend(cycle_lines)
        parts.append("")
    parts.extend(body_lines)
    parts.append("")
    parts.append(summary)

    raw = "\n".join(parts) + "\n"
    if use_color:
        return raw

    # Strip Rich color tags when caller asked for plain text. The
    # _render_task_line helper only embeds tags when ``use_color=True``,
    # so this is a no-op on the plain branch — kept here as a guard for
    # any future caller that flips the flag mid-render.
    return raw


def _render_task_line(task: Task, *, badge_width: int, use_color: bool) -> str:
    """Format one ``[STATUS] task_id (priority)`` row.

    The badge is left-padded to ``badge_width`` so the trailing ids and
    priorities line up across rows. With ``use_color=True``, the badge
    text is wrapped in Rich-tag markup (``[green]...[/green]``) — Rich
    parses the tag at print time and applies the ANSI sequence. With
    ``use_color=False`` the tag wrapping is omitted so the output is
    pure ASCII (suitable for snapshots, log files, ``less -R``-less
    pipes, etc.).
    """
    badge_text = task.status.value.ljust(badge_width)
    if use_color:
        color = _STATUS_COLOR[task.status]
        badge = f"[{color}]{badge_text}[/{color}]"
    else:
        badge = badge_text
    return f"[{badge}] {task.id}  ({task.priority.value})"


def _compute_topological_depth(
    tasks: Iterable[Task],
    by_id: dict[TaskId, Task],
) -> dict[TaskId, int]:
    """Return ``{task_id: depth}`` where depth is longest-path distance from a root.

    Tasks with no in-plan dependencies have depth ``0``. A task's depth is
    ``1 + max(depth of in-plan deps)``. Tasks that participate in a cycle
    cannot be assigned a finite depth — they are omitted from the result
    so the caller can fall back to a sentinel (the renderer uses
    ``max(depth) + 1`` so cycle members sort below all DAG nodes; the
    cycle banner above the graph already names them, so listing them last
    is the least confusing layout).

    Implementation: memoised iterative DFS with a "visiting" marker to
    detect back-edges. We don't reuse :func:`whilly.core.scheduler.topological_sort`
    here because it returns a flat order, not depth — and because we
    must remain robust against cycles (``topological_sort`` raises;
    callers of the renderer have already detected and reported cycles
    via :func:`detect_cycles`, so we skip those nodes silently rather
    than re-raising).
    """
    depth: dict[TaskId, int] = {}
    visiting: set[TaskId] = set()

    def resolve(tid: TaskId) -> int | None:
        """Compute depth of ``tid`` or return ``None`` if it's in a cycle."""
        if tid in depth:
            return depth[tid]
        if tid in visiting:
            return None
        task = by_id.get(tid)
        if task is None:
            return 0
        visiting.add(tid)
        max_dep_depth = -1
        cycle_seen = False
        for dep in task.dependencies:
            if dep not in by_id:
                continue
            sub = resolve(dep)
            if sub is None:
                cycle_seen = True
                continue
            if sub > max_dep_depth:
                max_dep_depth = sub
        visiting.discard(tid)
        if cycle_seen:
            # Don't memoise: the partial depth is meaningless without
            # the dep we couldn't resolve. Caller's sentinel handles it.
            return None
        depth[tid] = max_dep_depth + 1
        return depth[tid]

    for task in tasks:
        resolve(task.id)
    return depth


def _count_statuses(tasks: Iterable[Task]) -> dict[TaskStatus, int]:
    """Tally ``tasks`` by status.

    Returns a fully populated dict (every :class:`TaskStatus` member has
    a key, default ``0``) so the renderer's summary line can iterate the
    enum in declaration order without ``.get(..., 0)`` ceremony.
    """
    tally = dict.fromkeys(TaskStatus, 0)
    for task in tasks:
        tally[task.status] += 1
    return tally


def _run_show(plan_id: str, *, no_color: bool) -> int:
    """Implement ``whilly plan show <plan_id>``.

    Symmetric with :func:`_run_export`:

    1. Read ``WHILLY_DATABASE_URL`` from the environment — missing →
       ``EXIT_ENVIRONMENT_ERROR``.
    2. Fetch ``(Plan, list[Task])`` via :func:`_async_export` — same
       SELECT path so a future schema change (e.g. adding ``created_at``
       to the projection) only needs to be made once.
    3. Detect cycles on the in-memory plan via :func:`detect_cycles`.
    4. Render via :func:`render_plan_graph` and print to stdout. The
       Rich :class:`Console` decides whether to paint colors based on
       ``--no-color`` and ``stdout.isatty()`` — so the same command
       behaves correctly when piped to a file (no ANSI noise) or run
       in an interactive terminal (full color).
    5. If any cycle was detected, exit ``EXIT_VALIDATION_ERROR``
       (PRD SC-4: cycles must surface as a non-zero exit even on the
       read-only ``show`` path).

    The graph is printed via Rich so the color tags in the rendered
    string are interpreted at print time. Stdout becomes the canonical
    transport — operator can ``> graph.txt``, ``| less``, etc.

    Returns
    -------
    EXIT_OK
        Plan exists, no cycles.
    EXIT_VALIDATION_ERROR
        Plan exists but contains a cycle.
    EXIT_ENVIRONMENT_ERROR
        DSN unset, or plan_id not found.
    """
    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly plan show: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    result = asyncio.run(_async_export(dsn, plan_id))
    if result is None:
        print(
            f"whilly plan show: plan {plan_id!r} not found — check the id matches the "
            "'plan_id' you used at import time.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    plan, tasks = result
    cycles = detect_cycles(plan)
    use_color = _should_use_color(no_color=no_color)
    rendered = render_plan_graph(plan, tasks, cycles, use_color=use_color)

    # Use a Rich Console so [green]...[/green] tags resolve to ANSI when
    # color is enabled. ``soft_wrap=True`` keeps long task ids on a
    # single line; ``highlight=False`` stops Rich from auto-colourising
    # numbers / quoted strings (we want explicit colors only on the
    # status badge).
    console = Console(
        file=sys.stdout,
        force_terminal=use_color,
        no_color=not use_color,
        soft_wrap=True,
        highlight=False,
    )
    console.print(rendered, end="")

    if cycles:
        # Cycles already surfaced inside the graph header; the exit code
        # is the machine-readable channel. PRD SC-4 mandates a non-zero
        # exit, AC says "Цикл → exit code 1".
        return EXIT_VALIDATION_ERROR
    return EXIT_OK


def _should_use_color(*, no_color: bool) -> bool:
    """Decide whether to emit ANSI color sequences.

    Three signals, in priority order:

    1. ``--no-color`` (``no_color=True``) → always plain.
    2. ``NO_COLOR`` env var set (any value) → plain. Honors the
       informal cross-tool convention at https://no-color.org so
       operators don't have to special-case Whilly in their dotfiles.
    3. Otherwise: ``sys.stdout.isatty()``. Pipe-to-file or pipe-to-less
       gets plain ASCII; an interactive terminal gets color.

    The :class:`io.TextIOBase` ``isatty`` check tolerates non-stream
    stdouts (e.g. ``pytest``'s ``capsys`` substitutes a buffer that
    doesn't always implement isatty); ``getattr`` with a falsy default
    keeps us robust there.
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
        # Closed buffer / detached file descriptor.
        return False


# ── plan reset (TASK-103) ────────────────────────────────────────────────


def _run_reset(plan_id: str, *, keep_tasks: bool, hard: bool, yes: bool) -> int:
    """Implement ``whilly plan reset <plan_id> --keep-tasks|--hard [--yes]``.

    Two-step compose: a SELECT-side preflight to check the plan exists
    and count its tasks (so the y/N prompt can show the operator what
    they're about to wipe), then a single :meth:`TaskRepository.reset_plan`
    call to do the real work. The preflight reuses the :func:`_async_export`
    SELECT path so a future schema change only needs to be made in one
    place.

    Mode invariant: argparse's ``mutually_exclusive_group(required=True)``
    guarantees exactly one of ``--keep-tasks`` / ``--hard`` is set, so we
    don't validate that pair again here — the assertion is loud-fail
    only as a guard against future refactors that disable the argparse
    group.

    Confirmation flow:
        Without ``--yes``, the handler reads a y/N answer from stdin.
        Anything other than ``y`` / ``Y`` / ``yes`` aborts with
        :data:`EXIT_OK` (a deliberate decision by the operator is not an
        error). Non-TTY stdin (CI / piped invocation) without ``--yes``
        is treated as "no answer" → abort. Operators automating the
        reset should pass ``--yes`` explicitly so the intent is in the
        command line.

    Returns
    -------
    EXIT_OK
        Reset succeeded, or operator aborted at the prompt.
    EXIT_ENVIRONMENT_ERROR
        DSN unset, or plan_id not found.
    """
    assert keep_tasks ^ hard, "argparse mutex group should make this unreachable"

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly plan reset: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    # Preflight: confirm the plan exists and grab the task count for the
    # confirmation prompt. Reuses _async_export's SELECT path.
    preflight = asyncio.run(_async_export(dsn, plan_id))
    if preflight is None:
        print(
            f"whilly plan reset: plan {plan_id!r} not found — check the id matches the "
            "'plan_id' you used at import time.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR
    plan, tasks = preflight
    mode_label = "keep-tasks" if keep_tasks else "hard"
    summary = f"plan {plan.id!r} ({plan.name}) — {len(tasks)} task(s), mode={mode_label}"

    if not yes and not _confirm_reset(summary, hard=hard):
        print("whilly plan reset: aborted by operator (no --yes).", file=sys.stderr)
        return EXIT_OK

    affected = asyncio.run(_async_reset(dsn, plan_id, keep_tasks=keep_tasks))
    verb = "reset" if keep_tasks else "deleted"
    print(f"whilly plan reset: {verb} {summary} ({affected} task(s) affected).")
    return EXIT_OK


def _confirm_reset(summary: str, *, hard: bool) -> bool:
    """Prompt the operator with a y/N question; return True iff they said yes.

    Hard-mode prompts use a stronger wording (``DELETE``) to reflect
    the irrecoverable nature of the operation: with --keep-tasks an
    operator who confirms by mistake still has the plan rows on disk;
    with --hard the plan is gone, FK-cascaded events and all.

    Non-TTY stdin (CI / piped invocation) is treated as "no answer" →
    return False. Operators automating the reset should pass ``--yes``
    so the intent is explicit at the command line; falling through
    silently here would be the worst of both worlds.
    """
    is_tty = getattr(sys.stdin, "isatty", None)
    if not callable(is_tty) or not is_tty():
        return False
    verb = "DELETE" if hard else "RESET"
    prompt = f"About to {verb} {summary}. Continue? [y/N]: "
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


async def _async_reset(dsn: str, plan_id: str, *, keep_tasks: bool) -> int:
    """Open a pool, call :meth:`TaskRepository.reset_plan`, close the pool.

    Mirrors :func:`_async_import` lifecycle: short-lived pool, ``SELECT 1``
    health check on construction, ``finally`` always closes. Returns the
    number of tasks affected (reset to PENDING in keep-tasks mode, or
    scheduled for deletion in hard mode).
    """
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        return await repo.reset_plan(plan_id, keep_tasks=keep_tasks)
    finally:
        await close_pool(pool)


# ── plan apply (TASK-104c) ───────────────────────────────────────────────


def _run_apply(plan_file: str, *, strict: bool) -> int:
    """Implement ``whilly plan apply <plan_file> [--strict]`` (TASK-104c).

    Composite of :func:`_run_import` plus the
    :func:`whilly.core.gates.evaluate_decision_gate` step:

    1. :func:`parse_plan` — bad shape → ``EXIT_VALIDATION_ERROR``.
    2. :func:`detect_cycles` — any cycle → ``EXIT_VALIDATION_ERROR``
       with the chain printout (PRD VAL-GATES-019: cycle errors win
       over gate errors at the same exit code).
    3. ``WHILLY_DATABASE_URL`` env check — missing →
       ``EXIT_ENVIRONMENT_ERROR``.
    4. :func:`_async_apply` — open the pool, INSERT plan + tasks in
       one transaction, then run the Decision Gate over each task and
       (in ``--strict`` mode only) call
       :meth:`TaskRepository.skip_task` on each REJECT.

    Default mode (``strict=False``) emits one structured warning to
    stderr per failing task and leaves it ``PENDING`` — no
    ``task.skipped`` / ``SKIP`` events are written from the gate path
    (PRD VAL-GATES-022). Strict mode writes one ``SKIP`` event per
    REJECT (PRD VAL-CROSS-003).

    Exit codes follow the module-level convention (``0`` = ok,
    ``1`` = validation error, ``2`` = environment error). Both modes
    return ``EXIT_OK`` on the happy path — the gate diagnostic is a
    soft warning, not a validation failure (the cycle check above is
    where hard failures live).
    """
    try:
        plan, tasks = parse_plan(plan_file)
    except PlanParseError as exc:
        print(f"whilly plan apply: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except FileNotFoundError as exc:
        print(
            f"whilly plan apply: file not found: {exc.filename or plan_file}",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    cycles = detect_cycles(plan)
    if cycles:
        for cycle in cycles:
            print(f"whilly plan apply: Cycle detected: {_format_cycle(cycle)}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(
            f"whilly plan apply: {DATABASE_URL_ENV} is not set — point it at a Postgres "
            "instance with the v4 schema applied (see scripts/db-up.sh).",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT_ERROR

    skipped_ids, warned_ids = asyncio.run(_async_apply(dsn, plan, list(tasks), strict=strict))

    if strict and skipped_ids:
        print(
            f"whilly plan apply: applied plan {plan.id!r} ({len(tasks)} task(s)); "
            f"--strict skipped {len(skipped_ids)} task(s): {', '.join(skipped_ids)}.",
        )
    elif warned_ids:
        # Default-mode warnings already went to stderr inside _async_apply;
        # the success line on stdout still reports the import count so
        # shell pipelines / CI logs stay parseable.
        print(
            f"whilly plan apply: applied plan {plan.id!r} ({len(tasks)} task(s)); "
            f"{len(warned_ids)} task(s) failed Decision Gate (see warnings above).",
        )
    else:
        print(f"whilly plan apply: applied plan {plan.id!r} ({len(tasks)} task(s)).")
    return EXIT_OK


async def _async_apply(
    dsn: str,
    plan: Plan,
    tasks: list[Task],
    *,
    strict: bool,
) -> tuple[list[TaskId], list[TaskId]]:
    """Open a pool, INSERT plan + tasks, then run the Decision Gate.

    Returns ``(skipped_ids, warned_ids)``: the task ids that were
    transitioned to ``SKIPPED`` via :meth:`TaskRepository.skip_task`
    (only populated when ``strict=True``) and the task ids that
    surfaced a structured warning (only populated when ``strict=False``).
    These two sets are mutually exclusive — strict mode never warns,
    default mode never skips.

    Lifecycle mirrors :func:`_async_import`: short-lived pool with the
    ``SELECT 1`` health check, INSERT plan + tasks in a single
    transaction (so a crash mid-INSERT can never leave a partially-
    populated plan visible), then the gate iterations run in their
    own per-row transactions inside :meth:`skip_task`. The gate
    iteration intentionally does *not* share the import transaction:
    we want every task imported even if a later ``skip_task`` raises.

    Cross-plan safety (round-2 scrutiny fix)
    ----------------------------------------
    ``tasks.id`` is the schema's primary key — globally unique across
    all plans. Combined with the import path's ``ON CONFLICT (id) DO
    NOTHING`` semantics, a task id present in this plan's JSON file
    might already exist in the database under a *different* ``plan_id``
    (e.g. an operator copy-pasted ``T-OK-1`` between two plan files).
    In that case the row in ``tasks`` belongs to the other plan, and
    ``--strict`` calling :meth:`skip_task` against ``task.id`` would
    flip *that* row to SKIPPED — a cross-plan mutation bug.

    Mitigation: after the INSERT transaction commits, fetch the set of
    task ids that actually live under *our* ``plan_id`` and gate-iterate
    only those. Any parsed-from-file task whose id does not appear in
    that set is reported as a collision warning (stderr) instead of
    being skipped. This covers three observable scenarios cleanly:

    * tasks freshly inserted by this apply → present in ``ours_ids``;
    * tasks pre-existing for *this* plan (idempotent re-run) → present;
    * tasks colliding with another plan's id → absent → never skipped.

    The follow-up SELECT runs once per apply (cheap; one round trip
    after the import transaction). It uses the same ``plan_id``
    ``WHERE`` clause that ``_select_plan_with_tasks`` uses, so a future
    refactor would only need to touch one SQL string.
    """
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _insert_plan_and_tasks(conn, plan, tasks)

            # Snapshot the set of task ids that actually live under
            # our plan_id *after* the import committed. Used by the
            # strict iteration below to refuse to skip tasks whose
            # primary-key row belongs to a different plan (round-2
            # scrutiny finding B1 — see the docstring section
            # "Cross-plan safety" above).
            owned_id_rows = await conn.fetch(
                "SELECT id FROM tasks WHERE plan_id = $1",
                plan.id,
            )
            owned_ids: set[TaskId] = {row["id"] for row in owned_id_rows}

        repo = TaskRepository(pool)
        skipped: list[TaskId] = []
        warned: list[TaskId] = []
        for task in tasks:
            verdict = evaluate_decision_gate(task)
            if verdict.kind == GateVerdictKind.ALLOW:
                continue
            if strict:
                # Cross-plan safety guard: if the task id from the
                # parsed file is not present under *our* plan_id in
                # the database (because ``ON CONFLICT (id) DO NOTHING``
                # left a pre-existing row owned by a different plan
                # untouched), refuse to skip it — the SKIPPED transition
                # would mutate the other plan's row. Surface a
                # structured warning so the operator can investigate
                # the id collision in their plan files.
                if task.id not in owned_ids:
                    logger.warning(
                        "plan apply --strict: task %s not owned by plan %s "
                        "(primary-key collision with another plan); refusing to skip",
                        task.id,
                        plan.id,
                    )
                    print(
                        "whilly plan apply: warning: refusing to skip "
                        f"{task.id!r} — task id collides with another plan's "
                        f"row (this plan {plan.id!r} did not insert it).",
                        file=sys.stderr,
                    )
                    continue
                # ``task.version`` is the version we just inserted (0
                # by default); the row was created in this same call
                # so no other writer can have advanced it. The
                # idempotency probe inside ``skip_task`` covers the
                # operator-iteration case (re-running ``apply`` after
                # a partial run) without us having to re-fetch here.
                try:
                    await repo.skip_task(
                        task.id,
                        task.version,
                        reason="decision_gate_failed",
                        detail={"missing": list(verdict.missing)},
                    )
                except VersionConflictError as exc:
                    # Surface conflicts as warnings rather than aborting:
                    # the gate isn't authoritative enough to prevent a
                    # legitimate worker that beat us to the row. The
                    # task ends up not skipped — operators can re-run
                    # ``apply`` once the worker finishes.
                    logger.warning(
                        "plan apply --strict: skip_task conflict on task %s: %s",
                        task.id,
                        exc,
                    )
                    print(
                        f"whilly plan apply: warning: could not skip {task.id!r} ({exc}); leaving as-is.",
                        file=sys.stderr,
                    )
                    continue
                logger.info(
                    "plan apply --strict: skipped task %s (missing=%s)",
                    task.id,
                    verdict.missing,
                )
                skipped.append(task.id)
            else:
                logger.warning(
                    "plan apply: decision_gate_failed task=%s missing=%s reason=%s",
                    task.id,
                    verdict.missing,
                    verdict.reason,
                )
                # Stable structured stderr line; the ``decision_gate``
                # substring is part of the public contract (PRD
                # VAL-GATES-018) so operators can grep for it.
                print(
                    "whilly plan apply: warning: decision_gate task="
                    f"{task.id!r} missing={list(verdict.missing)} "
                    f"reason={verdict.reason!r}",
                    file=sys.stderr,
                )
                warned.append(task.id)

        # M3 fix-feature: emit exactly one ``plan.applied`` audit
        # event per ``apply`` invocation, after the gate iteration
        # finishes. Carries a small diagnostic payload identifying
        # how many tasks the call processed, how many were skipped,
        # how many surfaced a warning, and whether ``--strict`` was
        # set. Emitted on every apply invocation — VAL-CROSS-005's
        # idempotency invariant only constrains ``plan.created`` /
        # ``task.created`` / ``task.skipped``; ``plan.applied``
        # naturally accumulates one row per apply call so operators
        # can audit how many times an apply was run.
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_PLAN_APPLIED_EVENT_SQL,
                plan.id,
                json.dumps(
                    {
                        "tasks_count": len(tasks),
                        "skipped_count": len(skipped),
                        "warned_count": len(warned),
                        "strict": strict,
                    }
                ),
            )
        return skipped, warned
    finally:
        await close_pool(pool)
