"""Phase 3 Integration Test — DAG vertical slice (TASK-016, PRD SC-4 / FR-3.x).

Phase 3 ships the DAG layer end-to-end:

* :func:`whilly.adapters.filesystem.plan_io.parse_plan` reads v4 JSON.
* :func:`whilly.core.scheduler.detect_cycles` validates the in-memory plan.
* :mod:`whilly.cli.plan` writes the plan to Postgres via ``plan import``,
  reads it back via ``plan show`` (shared SELECT path with ``plan export``),
  and renders an ASCII graph.
* :func:`whilly.core.scheduler.next_ready` answers "what may I dispatch
  next?" against the same models the database round-trips.

The unit suites in :mod:`tests.unit.test_scheduler`,
:mod:`tests.unit.test_plan_io`, and :mod:`tests.unit.test_plan_show` already
prove each component in isolation. This file is the *integration* gate: it
proves that running these components together — through real Postgres —
satisfies the three acceptance criteria from TASK-016:

1. ``Тест на отказ при цикле`` — ``plan import`` of a cyclic plan exits
   with :data:`EXIT_VALIDATION_ERROR` and writes nothing to the DB.
2. ``Тест на корректную визуализацию`` — ``plan import`` of a valid plan
   followed by ``plan show`` renders a graph that mentions every task id,
   the project title, and a clean (non-cycle) summary.
3. ``Тест на priority в next_ready`` — :func:`next_ready`, applied to the
   models loaded back out of the DB, returns ready tasks in
   ``critical → high → medium → low`` order with file-conflict guarding.

Why integration, not unit?
--------------------------
:func:`detect_cycles` and :func:`next_ready` are pure — they have full unit
coverage in :mod:`tests.unit.test_scheduler`. What this file checks is the
*plumbing*: that a cyclic plan never produces partial INSERTs, that the
SELECT path that round-trips through ``plan show`` reconstitutes
:class:`Plan` / :class:`Task` value objects faithful enough for
:func:`next_ready` to make the same decisions it would on the original
parse, and that ``plan show`` emits the layout the operator expects on a
non-trivial DAG. Mocking asyncpg here would only assert "we call the right
methods"; the failure modes Phase 3 actually has to defend against
(incomplete imports, JSONB encoding drift, status round-trip) only show up
against a real Postgres.

Fixture strategy
----------------
We re-use :func:`tests.conftest.db_pool` (per-test asyncpg pool against the
session-scoped testcontainers Postgres with migrations applied). That
fixture also TRUNCATEs every table at setup, so each test starts from an
empty DB. The cycle test relies on an *empty* ``tasks`` table after the
failed import — that's how we prove "no partial inserts".

The CLI handler reads its DSN from ``WHILLY_DATABASE_URL`` (one-shot
process, no DSN threading). We point the env var at the testcontainers
DSN per-test and restore the prior value on teardown so a sibling test
that sets a custom DSN does not leak into a later one.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.filesystem.plan_io import parse_plan
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_OK,
    EXIT_VALIDATION_ERROR,
    run_plan_command,
)
from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus
from whilly.core.scheduler import next_ready

# Module-level skip — every test boots a Postgres container via the
# session-scoped ``postgres_dsn`` fixture, so a Docker-less CI runner
# should skip collection rather than fail per-test.
pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    Mirrors the fixture used by :mod:`tests.integration.test_plan_io` and
    :mod:`tests.integration.test_plan_show`. The CLI handler is one-shot;
    threading the DSN through every call site would explode the surface,
    so the env var is the canonical channel.
    """
    prior = os.environ.get(DATABASE_URL_ENV)
    os.environ[DATABASE_URL_ENV] = postgres_dsn
    try:
        yield postgres_dsn
    finally:
        if prior is None:
            os.environ.pop(DATABASE_URL_ENV, None)
        else:
            os.environ[DATABASE_URL_ENV] = prior


@pytest.fixture
def cyclic_plan_payload() -> dict[str, Any]:
    """A v4 plan that violates SC-4 — three tasks forming ``A → B → C → A``.

    Each node has the bare minimum fields :func:`parse_plan` requires; the
    cycle is the *only* validation failure so the test isolates the
    cycle-rejection contract from any incidental shape errors.
    """
    return {
        "plan_id": "plan-phase3-cycle-001",
        "project": "Phase 3 Cycle Workshop",
        "tasks": [
            {
                "id": "A",
                "status": "PENDING",
                "priority": "high",
                "description": "Cycle node A.",
                "dependencies": ["C"],
            },
            {
                "id": "B",
                "status": "PENDING",
                "priority": "high",
                "description": "Cycle node B.",
                "dependencies": ["A"],
            },
            {
                "id": "C",
                "status": "PENDING",
                "priority": "high",
                "description": "Cycle node C.",
                "dependencies": ["B"],
            },
        ],
    }


@pytest.fixture
def acyclic_priority_payload() -> dict[str, Any]:
    """A v4 plan exercising priority ordering and key_files conflicts.

    Layout:

    * ``T-ROOT`` — already DONE, depends on nothing. Models a completed
      bootstrap step so the three children below are all *ready* at once.
    * ``T-CRIT`` (critical), ``T-HIGH`` (high), ``T-LOW`` (low) — each
      depends on ``T-ROOT`` so they all sit at the same DAG layer; the
      only thing distinguishing them is :class:`Priority`.
    * ``T-CRIT`` and ``T-HIGH`` both touch ``shared.py``. After the
      file-conflict guard kicks in, only the *higher-priority* one
      (``T-CRIT``) survives the round of admissions.
    * ``T-LOW`` touches ``low.py`` — no overlap, so it is ready in
      isolation but is dropped only because of priority's ordering
      requirement is already satisfied (it stays *in* the ready list, just
      after the critical task).

    The expected ``next_ready`` output is therefore::

        ['T-CRIT', 'T-LOW']

    — ``T-HIGH`` is filtered by the ``shared.py`` collision with the
    higher-priority ``T-CRIT``. This is the canonical PRD FR-3.4 scenario:
    priority ordering *and* file-conflict resolution must both fire.
    """
    return {
        "plan_id": "plan-phase3-priority-001",
        "project": "Phase 3 Priority Workshop",
        "tasks": [
            {
                "id": "T-ROOT",
                "status": "DONE",
                "priority": "critical",
                "description": "Bootstrap (already done, unblocks the rest).",
            },
            {
                "id": "T-CRIT",
                "status": "PENDING",
                "priority": "critical",
                "description": "Critical work; shares shared.py with T-HIGH.",
                "dependencies": ["T-ROOT"],
                "key_files": ["shared.py"],
            },
            {
                "id": "T-HIGH",
                "status": "PENDING",
                "priority": "high",
                "description": "High work; loses shared.py to T-CRIT.",
                "dependencies": ["T-ROOT"],
                "key_files": ["shared.py"],
            },
            {
                "id": "T-LOW",
                "status": "PENDING",
                "priority": "low",
                "description": "Low work, isolated to low.py.",
                "dependencies": ["T-ROOT"],
                "key_files": ["low.py"],
            },
        ],
    }


def _materialise(payload: dict[str, Any], target: Path) -> Path:
    """Write ``payload`` to ``target`` and return it.

    Materialising on disk (rather than calling :func:`parse_plan` on a
    dict) keeps this an integration test of the full CLI flow — the file
    read inside :func:`parse_plan` is part of the surface we exercise.
    """
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


async def _count_rows(dsn: str, table: str) -> int:
    """Open a transient connection and ``SELECT COUNT(*)`` from ``table``.

    We open a fresh :func:`asyncpg.connect` rather than reusing the
    ``db_pool`` fixture because that pool is bound to pytest-asyncio's
    per-test event loop, while :func:`run_plan_command` uses
    :func:`asyncio.run`. Driving the same pool from a *different* loop
    surfaces an ``InterfaceError: another operation in progress``. The
    pattern matches :mod:`tests.integration.test_plan_show`.
    """
    conn = await asyncpg.connect(dsn)
    try:
        result = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
        return int(result or 0)
    finally:
        await conn.close()


# ─── AC #1: cycle rejection ──────────────────────────────────────────────


def test_import_of_cyclic_plan_returns_validation_error_and_inserts_nothing(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — implicit DB readiness via fixture chain.
    database_url: str,
    cyclic_plan_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``plan import`` rejects a cyclic plan with exit ``1`` and zero rows.

    AC verbatim: "Тест на отказ при цикле". Two halves:

    * Exit code is :data:`EXIT_VALIDATION_ERROR` (PRD SC-4: "Цикл в
      зависимостях → exit code 1 с указанием цепочки").
    * The ``plans`` and ``tasks`` tables remain empty — :func:`detect_cycles`
      runs *before* the asyncpg pool is even opened, so there is no DB
      round-trip on the failure path.

    The cycle banner is asserted via stderr substring rather than by
    pinning the exact rendering — that layout is owned by
    :func:`whilly.cli.plan._format_cycle` and pinned in the unit tests.
    """
    plan_file = _materialise(cyclic_plan_payload, tmp_path / "cyclic.json")
    rc = run_plan_command(["import", str(plan_file)])
    assert rc == EXIT_VALIDATION_ERROR, f"cycle must exit {EXIT_VALIDATION_ERROR}, got {rc}"

    captured = capsys.readouterr()
    assert "Cycle detected" in captured.err, f"stderr must announce the cycle; got: {captured.err!r}"
    # Banner format from :func:`_format_cycle` — at least the membership
    # appears in the rendered chain. We do not pin separator characters
    # here (those are layout-test territory in :mod:`tests.unit.test_plan_show`).
    for tid in ("A", "B", "C"):
        assert tid in captured.err, f"cycle members must appear in stderr; missing {tid!r}: {captured.err!r}"
    # Stdout stays empty on the failure path so a CI capture
    # ``import 2>/dev/null`` shows no spurious progress lines.
    assert captured.out == "", f"failure path must not print to stdout; got: {captured.out!r}"

    # No partial insert: cycle detection runs in-memory before the pool
    # is opened, so neither table received a row. Use a transient
    # connection — see _count_rows docstring on the event-loop split.
    assert asyncio.run(_count_rows(database_url, "plans")) == 0, "plans table must be empty after cycle rejection"
    assert asyncio.run(_count_rows(database_url, "tasks")) == 0, "tasks table must be empty after cycle rejection"


# ─── AC #2: correct visualisation ────────────────────────────────────────


def test_import_then_show_renders_graph_for_valid_plan(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,  # noqa: ARG001  — sets WHILLY_DATABASE_URL for both subcommands.
    acyclic_priority_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: ``plan import`` → ``plan show`` renders the DAG cleanly.

    AC verbatim: "Тест на корректную визуализацию". We assert the
    *plumbing* — DB read path → renderer → stdout — succeeds for a
    multi-priority DAG, and that the rendered output contains the
    landmarks an operator relies on:

    * Title line with plan id + project name.
    * Every task id present.
    * Every status badge that appears in the plan (here: ``DONE`` on
      ``T-ROOT`` and ``PENDING`` on the three children).
    * The ``Summary:`` line for at-a-glance task counts.
    * **No** cycle banner — this plan is acyclic, so the show-side cycle
      check must stay quiet (PRD SC-4 only fires the banner on a real
      cycle, not on an empty result).

    Layout pinning lives in :mod:`tests.unit.test_plan_show`; this test
    verifies the round-trip works on a fresh import without re-pinning
    every separator character. The ``--no-color`` flag forces plain
    ASCII so substring assertions are stable across terminals.
    """
    plan_file = _materialise(acyclic_priority_payload, tmp_path / "priority.json")
    assert run_plan_command(["import", str(plan_file)]) == EXIT_OK, "valid plan must import"
    capsys.readouterr()  # discard import banner so show's output is clean.

    rc = run_plan_command(["show", "plan-phase3-priority-001", "--no-color"])
    assert rc == EXIT_OK, f"show must succeed on acyclic plan, got rc={rc}"

    captured = capsys.readouterr()
    out = captured.out
    # Title line — render_plan_graph emits ``Plan: <id> — <name>``.
    assert "Plan: plan-phase3-priority-001 — Phase 3 Priority Workshop" in out
    # Every task id present.
    for tid in ("T-ROOT", "T-CRIT", "T-HIGH", "T-LOW"):
        assert tid in out, f"task id {tid} missing from rendered graph"
    # Every status badge that appears in this plan. ``IN_PROGRESS`` is
    # *not* in this plan, so we must not see a stray badge for it (the
    # summary line filters out zero-count statuses).
    assert "DONE" in out, "T-ROOT's DONE badge must appear"
    assert "PENDING" in out, "PENDING badges for the three children must appear"
    # Summary line tallies all four tasks.
    assert "Summary: 4 tasks" in out
    # Acyclic plan must NOT produce a cycle banner.
    assert "Cycle detected" not in out


# ─── AC #3: priority in next_ready ───────────────────────────────────────


def test_next_ready_after_round_trip_honours_priority_and_key_files(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,  # noqa: ARG001
    acyclic_priority_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``next_ready`` over DB-loaded models obeys priority + key_files.

    AC verbatim: "Тест на priority в next_ready". The integration aspect
    is *which* :class:`Plan` / :class:`Task` instances we feed into the
    pure :func:`next_ready` function:

    * The original payload is imported via :func:`run_plan_command`.
    * Then exported via :func:`run_plan_command` (``plan export``) which
      re-uses the SELECT path ``plan show`` calls.
    * The exported JSON is re-parsed via :func:`parse_plan` to get a
      *fresh* (Plan, list[Task]) pair — no shared object identity with the
      payload we wrote in.

    If JSONB encoding, status round-trip, or priority deserialisation
    drifted between insert and select, :func:`next_ready` would either
    raise (unknown priority) or return the wrong order. Asserting on the
    expected order pins all three properties in one go.

    Expected output: ``['T-CRIT', 'T-LOW']``
        * ``T-ROOT`` is DONE, so it never enters the ready list.
        * ``T-CRIT`` (critical) wins the priority race and reserves
          ``shared.py``.
        * ``T-HIGH`` (high) wanted ``shared.py`` too — file-conflict
          guard drops it from this round (PRD FR-3.4).
        * ``T-LOW`` (low) touches a different file and follows ``T-CRIT``
          in the priority-then-id sort.

    The order itself is the load-bearing assertion: a shuffled list would
    mean either priority sort broke or the file-conflict admission
    walks the candidate list in the wrong direction.
    """
    plan_file = _materialise(acyclic_priority_payload, tmp_path / "priority.json")
    assert run_plan_command(["import", str(plan_file)]) == EXIT_OK
    capsys.readouterr()

    # Round-trip via export → parse so we exercise the same SELECT path
    # plan show uses, and rebuild core models from the canonical JSON.
    assert run_plan_command(["export", "plan-phase3-priority-001"]) == EXIT_OK
    exported_json = capsys.readouterr().out
    exported_file = tmp_path / "exported.json"
    exported_file.write_text(exported_json, encoding="utf-8")
    plan, tasks = parse_plan(exported_file)

    # Sanity check: the round-trip preserved status, priority, and key_files.
    by_id: dict[TaskId, Task] = {t.id: t for t in tasks}
    assert by_id["T-ROOT"].status == TaskStatus.DONE
    assert by_id["T-CRIT"].status == TaskStatus.PENDING
    assert by_id["T-CRIT"].priority == Priority.CRITICAL
    assert by_id["T-HIGH"].priority == Priority.HIGH
    assert by_id["T-LOW"].priority == Priority.LOW
    assert by_id["T-CRIT"].key_files == ("shared.py",)
    assert by_id["T-HIGH"].key_files == ("shared.py",)
    assert by_id["T-LOW"].key_files == ("low.py",)

    # The actual contract under test. ``in_progress=set()`` means no task
    # is currently being worked on by a worker — the entire ready set is
    # up for grabs subject to deps + priority + file conflicts.
    ready = next_ready(plan, in_progress=set())
    assert ready == ["T-CRIT", "T-LOW"], (
        f"next_ready must order critical-then-low and drop the high-priority "
        f"task that loses shared.py to the critical one; got {ready!r}"
    )


def test_next_ready_with_critical_in_progress_releases_high_to_run(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,  # noqa: ARG001
    acyclic_priority_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Once the critical task is in-flight, the high-priority sibling unblocks.

    Companion to the previous test, illustrating the *temporal* half of
    PRD FR-3.4: the file-conflict guard is per-call, not per-plan. As
    soon as ``T-CRIT`` is no longer in the ``next_ready`` candidate list
    (because it's already CLAIMED / IN_PROGRESS), its ``shared.py``
    reservation rolls forward via the ``in_progress`` parameter and
    ``T-HIGH`` is the one that gets dropped instead of admitted —
    *unless* ``T-CRIT`` is what's reserving the file, in which case
    ``T-HIGH`` is still locked out.

    Why this test exists in addition to the previous one
    ----------------------------------------------------
    The simpler "T-CRIT wins" assertion would still pass under a buggy
    implementation that hard-coded "always pick the highest-priority
    candidate" without honoring ``in_progress``. This test catches the
    bug where ``next_ready`` ignores its second argument: with
    ``T-CRIT`` already in flight, the function must *not* return it
    again, and ``T-HIGH`` must remain blocked because the critical's
    ``shared.py`` is still reserved.

    Expected: ``['T-LOW']``. ``T-CRIT`` is in_progress (skipped from
    candidates), and ``T-HIGH`` is dropped because ``shared.py`` is
    reserved by ``T-CRIT``. Only ``T-LOW`` (different file) is admitted.
    """
    plan_file = _materialise(acyclic_priority_payload, tmp_path / "priority.json")
    assert run_plan_command(["import", str(plan_file)]) == EXIT_OK
    capsys.readouterr()
    assert run_plan_command(["export", "plan-phase3-priority-001"]) == EXIT_OK
    exported_file = tmp_path / "exported.json"
    exported_file.write_text(capsys.readouterr().out, encoding="utf-8")
    plan, _tasks = parse_plan(exported_file)

    # Pretend T-CRIT was just claimed by another worker. Its key_files
    # ride along into the file-conflict guard via ``in_progress``.
    ready = next_ready(plan, in_progress={"T-CRIT"})
    assert ready == ["T-LOW"], (
        f"with T-CRIT in flight, T-HIGH must stay blocked on shared.py and only T-LOW is admitted; got {ready!r}"
    )


# ─── plumbing sanity: cycle rejection on disk-empty plan ─────────────────


def test_import_of_self_loop_returns_validation_error(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single task depending on itself (``A → A``) is rejected too.

    Self-loops are a degenerate but important cycle case (PRD scheduler
    docstring: "an SCC contains a single task id that lists itself as a
    dependency"). The unit suite already covers the algorithm; this test
    proves the CLI surface honors it on the import path. Without a
    self-loop case it would be possible to ship a "single-task plan
    always succeeds" shortcut without anyone noticing.
    """
    payload = {
        "plan_id": "plan-phase3-selfloop-001",
        "project": "Phase 3 Self-loop Workshop",
        "tasks": [
            {
                "id": "A",
                "status": "PENDING",
                "priority": "high",
                "description": "Self-loop node.",
                "dependencies": ["A"],
            },
        ],
    }
    plan_file = _materialise(payload, tmp_path / "selfloop.json")
    rc = run_plan_command(["import", str(plan_file)])
    assert rc == EXIT_VALIDATION_ERROR, f"self-loop must exit {EXIT_VALIDATION_ERROR}, got {rc}"

    captured = capsys.readouterr()
    assert "Cycle detected" in captured.err
    assert "A" in captured.err
    # No row reaches Postgres.
    assert asyncio.run(_count_rows(database_url, "plans")) == 0
    assert asyncio.run(_count_rows(database_url, "tasks")) == 0


# ─── docstring receipt: Plan type binding (light usage check) ────────────
# A defensive import-and-bind: if Plan is ever renamed away from
# whilly.core.models, the import at the top of this module fails
# during collection. We touch the symbol here so ruff's "imported but
# unused" rule doesn't flag the import — Plan is part of the public
# Phase 3 contract this test pins down, even if no test currently
# instantiates it directly.
_PLAN_TYPE: type[Plan] = Plan
