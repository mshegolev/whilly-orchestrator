"""Integration tests for ``whilly plan reset`` (TASK-103, PRD FR-2.5).

Acceptance criteria from TASK-103:

* ``whilly plan reset <plan_id>`` works in both modes (``--keep-tasks`` and ``--hard``).
* Without ``--yes`` — interactive y/N confirmation.
* With ``--keep-tasks``: ``tasks.status='PENDING'``, ``claimed_by=NULL``,
  ``version+=1``; events for this ``plan_id`` are deleted.
* With ``--hard``: ``DELETE FROM tasks/events/plans WHERE plan_id=X`` (CASCADE).
* Audit row ``RESET`` added to events with ``reason='manual_reset'``.

Why integration over unit?
--------------------------
``reset_plan`` is a multi-statement transaction touching three tables;
the SQL invariants (events DELETE before tasks UPDATE, RESET event per
task, FK cascade behaviour for hard mode) are exactly what a unit test
would have to mock away.

Why sync test functions (not ``async def``)?
--------------------------------------------
:func:`whilly.cli.plan.run_plan_command` is the synchronous CLI entry
point — it owns its own ``asyncio.run()`` calls internally. Wrapping
the test in an outer ``async def`` would put pytest-asyncio's loop
between the test and ``run_plan_command``, and the inner ``asyncio.run``
would raise ``RuntimeError: cannot be called from a running event
loop``. Mirrors the pattern in :mod:`tests.integration.test_plan_io`,
which keeps its CLI-driving tests synchronous for the same reason.

For the seed / verify steps that need direct asyncpg access, we
wrap them in :func:`asyncio.run` against a freshly-opened pool tied
to the session-scoped Postgres container. We deliberately do NOT
use the session conftest's async ``db_pool`` fixture here: that
fixture's pool lives on pytest-asyncio's loop, and using it from
inside our own ``asyncio.run`` would cross loops.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.db.repository import TaskRepository
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    run_plan_command,
)

pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    Mirrors the fixture in ``test_plan_io.py``: :func:`run_plan_command`
    reads the DSN from the environment, so we set it for the test and
    restore the prior value on teardown.

    Also TRUNCATEs every relevant table at setup so each test starts
    from a clean DB. This replaces the role the async ``db_pool``
    fixture's TRUNCATE plays for async-style tests — we can't use that
    fixture here without crossing event loops (see module docstring).
    """
    prior = os.environ.get(DATABASE_URL_ENV)
    os.environ[DATABASE_URL_ENV] = postgres_dsn

    async def _truncate() -> None:
        pool = await create_pool(postgres_dsn)
        try:
            async with pool.acquire() as conn:
                await conn.execute("TRUNCATE events, tasks, plans, workers RESTART IDENTITY CASCADE")
        finally:
            await close_pool(pool)

    asyncio.run(_truncate())
    try:
        yield postgres_dsn
    finally:
        if prior is None:
            os.environ.pop(DATABASE_URL_ENV, None)
        else:
            os.environ[DATABASE_URL_ENV] = prior


@pytest.fixture
def sample_plan_payload() -> dict[str, Any]:
    """Return a v4 plan dict with three tasks at mixed priorities.

    T-001 critical, T-002 high, T-003 low — picked so the
    deterministic claim order (priority, then id) puts T-001 first
    and T-002 second; the seed helper relies on that ordering to
    leave the DB in a known mid-lifecycle state.
    """
    return {
        "plan_id": "plan-reset-001",
        "project": "Reset Workshop",
        "tasks": [
            {
                "id": "T-001",
                "status": "PENDING",
                "priority": "critical",
                "description": "First task",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
                "prd_requirement": "",
            },
            {
                "id": "T-002",
                "status": "PENDING",
                "priority": "high",
                "description": "Second task",
                "dependencies": ["T-001"],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
                "prd_requirement": "",
            },
            {
                "id": "T-003",
                "status": "PENDING",
                "priority": "low",
                "description": "Third task",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
                "prd_requirement": "",
            },
        ],
    }


@pytest.fixture
def sample_plan_file(tmp_path: Path, sample_plan_payload: dict[str, Any]) -> Path:
    """Materialise the sample plan as JSON on disk so ``plan import`` reads it."""
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(sample_plan_payload), encoding="utf-8")
    return target


# ─── helpers ─────────────────────────────────────────────────────────────


def _run_with_pool(dsn: str, fn: Callable[[asyncpg.Pool], Awaitable[Any]]) -> Any:
    """Open a pool against ``dsn``, run ``fn(pool)``, close the pool.

    Single-shot helper that lets sync tests interact with asyncpg
    without keeping a long-lived pool around. We deliberately don't
    reuse the session's ``db_pool`` fixture because it lives on a
    different event loop (see module docstring) and crossing loops
    risks "got Future ... attached to a different loop" errors.
    """

    async def _wrapper() -> Any:
        pool = await create_pool(dsn)
        try:
            return await fn(pool)
        finally:
            await close_pool(pool)

    return asyncio.run(_wrapper())


async def _seed_progress(pool: asyncpg.Pool, plan_id: str, worker_id: str) -> None:
    """Drive a few tasks through the lifecycle so the reset has state to wipe.

    Inserts a worker, claims and starts T-001 and T-002. After this
    helper runs, the DB carries:

    * 1 worker row,
    * 3 task rows: T-001=IN_PROGRESS, T-002=IN_PROGRESS, T-003=PENDING,
    * a few CLAIM / START events.

    The reset under test must wipe / reset all of that according to the
    selected mode. Side-stepping the HTTP transport keeps the test
    focused on the repository contract.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            "host-reset-test",
            "hash-reset-test",
        )
    repo = TaskRepository(pool)
    claimed = await repo.claim_task(worker_id, plan_id)
    assert claimed is not None and claimed.id == "T-001"
    started = await repo.start_task(claimed.id, claimed.version)
    assert started.status.value == "IN_PROGRESS"
    second = await repo.claim_task(worker_id, plan_id)
    # Claim order is priority bucket then id: T-001 critical first,
    # T-002 high second, T-003 low last.
    assert second is not None and second.id == "T-002"
    started2 = await repo.start_task(second.id, second.version)
    assert started2.status.value == "IN_PROGRESS"


# ─── tests ───────────────────────────────────────────────────────────────


def test_reset_keep_tasks_resets_status_and_writes_audit_rows(
    database_url: str,
    sample_plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--keep-tasks --yes`` resets every task to PENDING and writes RESET events."""
    assert run_plan_command(["import", str(sample_plan_file)]) == EXIT_OK
    capsys.readouterr()

    _run_with_pool(database_url, lambda pool: _seed_progress(pool, "plan-reset-001", "w-reset-keep"))

    rc = run_plan_command(["reset", "plan-reset-001", "--keep-tasks", "--yes"])
    assert rc == EXIT_OK, f"reset returned {rc}"
    out = capsys.readouterr().out
    assert "3 task(s) affected" in out
    assert "reset" in out.lower()

    async def _verify(pool: asyncpg.Pool) -> dict[str, int]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, status, claimed_by, claimed_at, version FROM tasks WHERE plan_id = $1 ORDER BY id",
                "plan-reset-001",
            )
            assert [r["id"] for r in rows] == ["T-001", "T-002", "T-003"]
            for r in rows:
                assert r["status"] == "PENDING", f"{r['id']} not reset to PENDING"
                assert r["claimed_by"] is None
                assert r["claimed_at"] is None
            version_by_id = {r["id"]: r["version"] for r in rows}
            # T-001 / T-002 went through CLAIM (v0→1) → START (v1→2) →
            # RESET (v2→3). T-003 was never claimed; only RESET (v0→1).
            assert version_by_id == {"T-001": 3, "T-002": 3, "T-003": 1}

            events = await conn.fetch(
                "SELECT task_id, event_type, payload "
                "FROM events WHERE task_id IN (SELECT id FROM tasks WHERE plan_id = $1) "
                "ORDER BY task_id, id",
                "plan-reset-001",
            )
            assert {row["event_type"] for row in events} == {"RESET"}
            assert len(events) == 3
            for row in events:
                payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                assert payload["reason"] == "manual_reset"
                assert payload["mode"] == "keep_tasks"
                assert payload["version"] == version_by_id[row["task_id"]]
            return version_by_id

    _run_with_pool(database_url, _verify)


def test_reset_hard_deletes_plan_tasks_and_events(
    database_url: str,
    sample_plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--hard --yes`` removes plan, tasks, and events via FK cascade."""
    assert run_plan_command(["import", str(sample_plan_file)]) == EXIT_OK
    capsys.readouterr()

    _run_with_pool(database_url, lambda pool: _seed_progress(pool, "plan-reset-001", "w-reset-hard"))

    rc = run_plan_command(["reset", "plan-reset-001", "--hard", "--yes"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "3 task(s) affected" in out
    assert "deleted" in out.lower()

    async def _verify(pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            plan_count = await conn.fetchval("SELECT COUNT(*) FROM plans WHERE id = $1", "plan-reset-001")
            task_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE plan_id = $1", "plan-reset-001")
            event_count = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE task_id IN ($1, $2, $3)",
                "T-001",
                "T-002",
                "T-003",
            )
            assert plan_count == 0
            assert task_count == 0
            assert event_count == 0

    _run_with_pool(database_url, _verify)


def test_reset_unknown_plan_id_exits_environment_error(
    database_url: str,  # noqa: ARG001 — sets WHILLY_DATABASE_URL for run_plan_command
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resetting a non-existent ``plan_id`` exits 2 with a helpful stderr."""
    rc = run_plan_command(["reset", "no-such-plan", "--keep-tasks", "--yes"])
    assert rc == EXIT_ENVIRONMENT_ERROR
    err = capsys.readouterr().err
    assert "no-such-plan" in err
    assert "not found" in err


def test_reset_without_mode_flag_fails_argparse(
    database_url: str,  # noqa: ARG001
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``whilly plan reset <id>`` without ``--keep-tasks`` / ``--hard`` exits 2.

    argparse's ``mutually_exclusive_group(required=True)`` is the rule;
    the test ensures we don't accidentally disable that constraint
    in a future refactor.
    """
    with pytest.raises(SystemExit) as excinfo:
        run_plan_command(["reset", "plan-reset-001"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--keep-tasks" in err or "--hard" in err


def test_reset_keep_tasks_is_idempotent(
    database_url: str,
    sample_plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running ``--keep-tasks --yes`` twice in a row leaves the plan in PENDING.

    Second invocation finds tasks already PENDING; the UPDATE matches
    them anyway (no status filter on reset_plan's keep-tasks UPDATE),
    bumps versions, wipes the previous RESET events, and writes fresh
    RESET events. Net effect: same task count, same status, but
    ``version`` is one higher per reset. This is the documented
    contract — the operator-facing surface only says "tasks are
    PENDING after reset", not "version is unchanged on a no-op reset".
    """
    assert run_plan_command(["import", str(sample_plan_file)]) == EXIT_OK
    capsys.readouterr()

    assert run_plan_command(["reset", "plan-reset-001", "--keep-tasks", "--yes"]) == EXIT_OK
    capsys.readouterr()
    assert run_plan_command(["reset", "plan-reset-001", "--keep-tasks", "--yes"]) == EXIT_OK
    capsys.readouterr()

    async def _verify(pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, status, version FROM tasks WHERE plan_id = $1 ORDER BY id",
                "plan-reset-001",
            )
            assert [r["status"] for r in rows] == ["PENDING", "PENDING", "PENDING"]
            assert {r["version"] for r in rows} == {2}
            events = await conn.fetch(
                "SELECT event_type FROM events WHERE task_id IN (SELECT id FROM tasks WHERE plan_id = $1)",
                "plan-reset-001",
            )
            assert {row["event_type"] for row in events} == {"RESET"}
            assert len(events) == 3

    _run_with_pool(database_url, _verify)
