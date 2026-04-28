"""``whilly plan`` subcommand surface (PRD FR-2.5, FR-3.1, TASK-010b / TASK-010c).

Exposes the two halves of the plan-IO pair:

* ``whilly plan import <plan_file>`` (TASK-010b) — read a v4 plan JSON,
  reject cycles, and persist the plan + tasks to Postgres in a single
  transaction.
* ``whilly plan export <plan_id>`` (TASK-010c, this commit) — fetch plan
  + tasks from Postgres and emit canonical v4 JSON to stdout. Designed
  to round-trip with ``import``: ``import → export → import`` is a no-op
  because the export uses :func:`~whilly.adapters.filesystem.plan_io.serialize_plan`,
  which emits exactly the canonical fields :func:`parse_plan` consumes.

``whilly plan show`` lands in TASK-015 and registers another subparser on
the same :class:`argparse.ArgumentParser` built here.

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
The CLI follows the conventional 0/1/2 split, mirroring the legacy
:mod:`whilly.cli_legacy` and the rest of the v4 CLI tasks:

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
import json
import logging
import os
import sys
from collections.abc import Iterable, Sequence

import asyncpg

from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.filesystem.plan_io import PlanParseError, parse_plan, serialize_plan
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus
from whilly.core.scheduler import detect_cycles

__all__ = ["build_plan_parser", "run_plan_command"]

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
    return parser


def run_plan_command(argv: Sequence[str]) -> int:
    """Entry point for ``whilly plan ...``; returns the process exit code.

    Stays synchronous on the outside so callers (and tests) don't need an
    event loop — the async parts are delegated to :func:`_async_import` via
    :func:`asyncio.run`.
    """
    parser = build_plan_parser()
    args = parser.parse_args(list(argv))
    if args.action == "import":
        return _run_import(args.plan_file)
    if args.action == "export":
        return _run_export(args.plan_id)
    # argparse's ``required=True`` already surfaces a 2-exit on missing
    # action; this branch is defensive for future subcommands added without
    # an explicit handler here.
    parser.error(f"unknown action {args.action!r}")  # noqa: RET503  (no-return)
    return EXIT_ENVIRONMENT_ERROR  # pragma: no cover — argparse SystemExits first


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
        await conn.execute(
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
