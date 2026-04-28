"""Integration tests for ``whilly plan show <plan_id>`` (TASK-015).

Acceptance criteria from TASK-015:

* ``whilly plan show <id>`` prints an ASCII dependency graph.
* Status colors via Rich (PENDING=grey, IN_PROGRESS=yellow, DONE=green,
  FAILED=red).
* A cycle in the dependency graph → exit code ``1`` plus a ``Cycle
  detected: A → B → C → A`` banner.
* Snapshot test pins the layout — see :mod:`tests.unit.test_plan_show`
  (the layout is a pure-function property, no DB needed).

Layering
--------
Layout snapshots live next door in :mod:`tests.unit.test_plan_show`.
This file covers the *plumbing* between the CLI entry point, the
SELECT path shared with ``plan export`` (TASK-010c), and the renderer.
We assert exit codes and the *presence* of diagnostic markers (cycle
banner, missing-plan message) without re-pinning every separator
character — that would couple a layout test to testcontainers and
force a Docker reboot every time we tweak a glyph.

The cycle scenario cannot use ``plan import`` (which rejects cycles up
front, by design — see TASK-010b). We bypass the parse path by
inserting plan + tasks directly via SQL on the test pool. That mirrors
the operational scenario the show-side cycle check guards against:
rows that arrived in Postgres through some path other than ``plan
import`` (manual SQL, future migration, third-party tooling) must
still surface cleanly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    EXIT_VALIDATION_ERROR,
    run_plan_command,
)

# Module-level skip — every test in this file boots a Postgres container via
# the session-scoped ``postgres_dsn`` fixture, so a Docker-less CI runner
# should skip collection rather than fail per-test.
pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    Same shape as the fixture in :mod:`tests.integration.test_plan_io`. We
    don't share it across modules so each test file stays self-contained;
    the handler reads the DSN from the environment (``run_plan_command``
    is a one-shot CLI; threading the DSN through every call site would
    explode the surface).
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
def acyclic_plan_payload() -> dict[str, Any]:
    """A v4 plan with three tasks at three statuses (PENDING / IN_PROGRESS / DONE).

    ``plan_id`` is unique to this file to keep cross-test diagnostics
    readable — the ``db_pool`` fixture already TRUNCATEs at setup, so
    isolation is guaranteed regardless, but a stable id makes test
    failures easy to attribute.
    """
    return {
        "plan_id": "plan-show-acyclic-001",
        "project": "Show Workshop",
        "tasks": [
            {
                "id": "T-001",
                "status": "DONE",
                "priority": "critical",
                "description": "Bootstrap.",
            },
            {
                "id": "T-002",
                "status": "IN_PROGRESS",
                "priority": "high",
                "description": "Build.",
                "dependencies": ["T-001"],
            },
            {
                "id": "T-003",
                "status": "PENDING",
                "priority": "low",
                "description": "Ship.",
                "dependencies": ["T-001", "T-002"],
            },
        ],
    }


@pytest.fixture
def acyclic_plan_file(tmp_path: Path, acyclic_plan_payload: dict[str, Any]) -> Path:
    """Write ``acyclic_plan_payload`` to disk and yield its path."""
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(acyclic_plan_payload), encoding="utf-8")
    return target


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from ``text`` for stable assertions.

    Rich's :class:`Console` emits ANSI when ``force_terminal=True``.
    The CLI handler decides via ``--no-color`` and ``stdout.isatty()``;
    under ``capsys`` the latter returns False so we shouldn't see ANSI
    in practice. This helper is a belt-and-braces filter so a future
    Rich change doesn't silently break the assertions.
    """
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ─── happy path ──────────────────────────────────────────────────────────


def test_show_acyclic_plan_returns_zero_and_renders_graph(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — implicit DB readiness via fixture chain
    database_url: str,  # noqa: ARG001
    acyclic_plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: import → show → assert exit ``0`` and graph in stdout.

    Asserts:
      * ``show`` exits :data:`EXIT_OK` for a cycle-free plan;
      * stdout contains the title line, every task id, and the summary;
      * stdout does *not* contain the cycle banner.

    Layout pinning lives in :mod:`tests.unit.test_plan_show`. This
    test verifies the *plumbing* — DB read path → renderer → stdout —
    without re-pinning every separator character.
    """
    assert run_plan_command(["import", str(acyclic_plan_file)]) == EXIT_OK
    capsys.readouterr()  # discard import banner

    rc = run_plan_command(["show", "plan-show-acyclic-001"])
    assert rc == EXIT_OK, f"show returned {rc} (expected {EXIT_OK})"

    captured = capsys.readouterr()
    out = _strip_ansi(captured.out)

    # Title line.
    assert "Plan: plan-show-acyclic-001 — Show Workshop" in out
    # Every task id is present.
    for tid in ("T-001", "T-002", "T-003"):
        assert tid in out, f"task id {tid} missing from output"
    # Status badges (substring check; exact alignment is layout-test
    # territory and lives in the unit snapshot test).
    assert "DONE" in out
    assert "IN_PROGRESS" in out
    assert "PENDING" in out
    # Dependency arrows in the expected order.
    assert "depends on: T-001" in out
    assert "depends on: T-001, T-002" in out
    # Summary line.
    assert "Summary: 3 tasks" in out
    # No cycle banner on a clean DAG.
    assert "Cycle detected" not in out


# ─── cycle path ──────────────────────────────────────────────────────────


def test_show_cycle_returns_exit_1_and_announces_cycle(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — request the fixture so the per-test TRUNCATE runs.
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insert a cyclic plan via raw SQL, then ``show`` must surface the cycle.

    AC verbatim: "Цикл → exit code 1 + сообщение 'Cycle detected: A →
    B → C → A'". The cycle banner is part of stdout (alongside the
    graph), and the exit code is the machine-readable channel.

    Why insert via raw SQL rather than through ``whilly plan import``?
    ----------------------------------------------------------------
    The import path validates with :func:`whilly.core.scheduler.detect_cycles`
    *before* INSERTing and refuses the plan with exit ``1``
    (TASK-010b AC). So we cannot reach a state where the DB holds a
    cyclic plan via the supported pipeline. The show-side cycle check
    exists to cover scenarios where rows arrived in Postgres through
    some other route (operations runbook running ad-hoc SQL, a partial
    migration, third-party tooling). To exercise that scenario we
    mimic it: skip the parse-path validation and insert directly.

    Why a fresh ``asyncpg.connect`` rather than the ``db_pool`` fixture?
    -------------------------------------------------------------------
    The pool fixture is bound to pytest-asyncio's per-test event loop.
    Calling ``run_plan_command`` (which uses :func:`asyncio.run`) and
    then trying to drive the same pool from a *different* loop here
    surfaces an ``InterfaceError: another operation in progress``. The
    cleanest workaround is to open a transient connection scoped to
    *our* :func:`asyncio.run` call — same pattern the production CLI
    uses (one-shot connect / disconnect per command).
    """

    async def _seed_cycle() -> None:
        """Insert plan + 3 tasks forming the cycle ``A → B → C → A``."""
        conn = await asyncpg.connect(database_url)
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO plans (id, name) VALUES ($1, $2)",
                    "plan-show-cycle-001",
                    "Cycle Workshop",
                )
                # JSONB columns: pass JSON text + ``::jsonb`` cast (same
                # idiom as ``_INSERT_TASK_SQL`` in whilly.cli.plan).
                tasks_with_deps = [
                    ("A", ["C"]),
                    ("B", ["A"]),
                    ("C", ["B"]),
                ]
                for tid, deps in tasks_with_deps:
                    await conn.execute(
                        """
                        INSERT INTO tasks (
                            id, plan_id, status, dependencies, key_files,
                            priority, description, acceptance_criteria,
                            test_steps, prd_requirement, version
                        )
                        VALUES (
                            $1, $2, 'PENDING', $3::jsonb, '[]'::jsonb,
                            'high', 'cycle node', '[]'::jsonb,
                            '[]'::jsonb, '', 0
                        )
                        """,
                        tid,
                        "plan-show-cycle-001",
                        json.dumps(deps),
                    )
        finally:
            await conn.close()

    asyncio.run(_seed_cycle())

    rc = run_plan_command(["show", "plan-show-cycle-001"])
    assert rc == EXIT_VALIDATION_ERROR, f"cycle must exit {EXIT_VALIDATION_ERROR}, got {rc}"

    captured = capsys.readouterr()
    out = _strip_ansi(captured.out)
    # Banner with the cycle members in deterministic (sorted) order.
    assert "Cycle detected: A → B → C → A" in out, f"cycle banner missing; full output:\n{out}"
    # Graph still rendered so the operator sees the cycle members in context.
    assert "Plan: plan-show-cycle-001 — Cycle Workshop" in out


# ─── error paths ─────────────────────────────────────────────────────────


def test_show_missing_plan_id_returns_exit_2_with_helpful_message(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — TRUNCATE makes the DB empty for this test.
    database_url: str,  # noqa: ARG001
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-existent ``plan_id`` exits ``2`` with a helpful stderr message.

    Same contract as ``plan export`` for consistency: the SELECT path
    is shared, the operator-visible error is shaped the same way.
    """
    rc = run_plan_command(["show", "no-such-plan-12345"])
    assert rc == EXIT_ENVIRONMENT_ERROR, f"expected exit {EXIT_ENVIRONMENT_ERROR}, got {rc}"
    captured = capsys.readouterr()
    assert captured.out == "", f"expected empty stdout on missing plan, got {captured.out!r}"
    assert "no-such-plan-12345" in captured.err
    assert "not found" in captured.err.lower()


def test_show_without_database_url_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing ``WHILLY_DATABASE_URL`` exits ``2`` (no DB connection attempted).

    The env-var check happens before pool open, so this test does not
    require Docker. Doubles as a guard that we don't accidentally
    dereference a None DSN.
    """
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    rc = run_plan_command(["show", "any-plan-id"])
    assert rc == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert DATABASE_URL_ENV in captured.err
    assert captured.out == ""
