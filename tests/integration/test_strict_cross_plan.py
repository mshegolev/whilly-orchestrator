"""Integration tests for cross-plan safety of ``whilly plan apply --strict``.

Round-2 scrutiny finding (B1): the strict gate must not mutate task
rows owned by a different plan, even when the parsed plan file carries
a task id that collides with a row already in the ``tasks`` table.

Background
----------
``tasks.id`` is the schema's global primary key. The ``plan apply``
import path uses ``INSERT ... ON CONFLICT (id) DO NOTHING`` so a
re-run is idempotent on plan_id. This means a plan whose task ids
collide with rows already owned by *another* plan_id will silently
have its tasks skipped at INSERT time — but ``--strict`` previously
called ``repo.skip_task(task_id, version)`` against those ids
regardless, mutating the *other* plan's rows.

The fix snapshots the set of task ids that actually live under the
applying plan's ``plan_id`` after the import transaction commits and
refuses to call ``skip_task`` on any task id outside that set.

These tests exercise that contract by seeding plan B first (the
"victim") and then applying plan A (the "attacker") with ``--strict``;
plan B's row must remain unchanged.
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
from whilly.adapters.db import close_pool, create_pool
from whilly.cli.plan import DATABASE_URL_ENV, EXIT_OK, run_plan_command

pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` and TRUNCATE tables for one test."""
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


def _write_plan(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _query_db(postgres_dsn: str, sql: str, *args: object) -> list[asyncpg.Record]:
    """Synchronous read helper — opens a fresh pool, runs one SELECT, closes."""

    async def _go() -> list[asyncpg.Record]:
        pool = await create_pool(postgres_dsn)
        try:
            async with pool.acquire() as conn:
                return await conn.fetch(sql, *args)
        finally:
            await close_pool(pool)

    return asyncio.run(_go())


def _victim_plan_payload() -> dict[str, Any]:
    """Plan B — healthy, owns the colliding task id ``T-SHARED``."""
    return {
        "plan_id": "plan-victim",
        "project": "Victim Plan",
        "tasks": [
            {
                "id": "T-SHARED",
                "status": "PENDING",
                "priority": "high",
                "description": "Owned by plan B; must remain PENDING after the cross-plan apply.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["plan B's AC stays in place"],
                "test_steps": ["pytest -k victim"],
                "prd_requirement": "",
            },
        ],
    }


def _attacker_plan_payload() -> dict[str, Any]:
    """Plan A — gate-failing, shares the task id ``T-SHARED`` with plan B."""
    return {
        "plan_id": "plan-attacker",
        "project": "Attacker Plan",
        "tasks": [
            {
                "id": "T-SHARED",
                "status": "PENDING",
                "priority": "medium",
                "description": "Refactor logging utilities to use structlog throughout.",
                "dependencies": [],
                "key_files": [],
                # Empty AC → gate REJECT; --strict would historically call
                # skip_task("T-SHARED", ...) and flip the victim row.
                "acceptance_criteria": [],
                "test_steps": ["pytest -k logging"],
                "prd_requirement": "",
            },
        ],
    }


# ─── B1 round-2 cross-plan safety (primary contract) ─────────────────────


def test_strict_apply_does_not_mutate_other_plans_task(
    database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Strict apply on plan A leaves plan B's colliding task untouched."""
    victim_file = _write_plan(tmp_path, "victim.json", _victim_plan_payload())
    attacker_file = _write_plan(tmp_path, "attacker.json", _attacker_plan_payload())

    # Seed plan B with the shared task id first — it owns the row.
    assert run_plan_command(["import", str(victim_file)]) == EXIT_OK
    capsys.readouterr()  # drain output from the seed import

    pre_rows = _query_db(
        database_url,
        "SELECT id, plan_id, status, version FROM tasks WHERE id = $1",
        "T-SHARED",
    )
    assert len(pre_rows) == 1, "victim plan should own exactly one T-SHARED row"
    pre_row = pre_rows[0]
    assert pre_row["plan_id"] == "plan-victim"
    assert pre_row["status"] == "PENDING"
    pre_version = pre_row["version"]

    # Apply the attacker plan with --strict. The shared id must not
    # be flipped to SKIPPED on plan B's row.
    rc = run_plan_command(["apply", "--strict", str(attacker_file)])
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    # Stderr names the collision so the operator can investigate.
    assert "T-SHARED" in captured.err
    assert "collide" in captured.err.lower() or "collision" in captured.err.lower()

    # Plan B's task is untouched: same plan_id, still PENDING, version
    # unchanged (no UPDATE applied).
    post_rows = _query_db(
        database_url,
        "SELECT id, plan_id, status, version FROM tasks WHERE id = $1",
        "T-SHARED",
    )
    assert len(post_rows) == 1
    post_row = post_rows[0]
    assert post_row["plan_id"] == "plan-victim", "cross-plan mutation: T-SHARED must remain owned by plan B"
    assert post_row["status"] == "PENDING", "cross-plan mutation: T-SHARED must remain PENDING"
    assert post_row["version"] == pre_version, "cross-plan mutation: T-SHARED version must not advance"

    # No SKIP event row references T-SHARED — the strict path refused
    # to call skip_task on the colliding id.
    skip_events = _query_db(
        database_url,
        "SELECT task_id FROM events WHERE task_id = $1 AND event_type = 'SKIP'",
        "T-SHARED",
    )
    assert skip_events == [], "cross-plan mutation: SKIP event row was written for T-SHARED"

    # Plan A is recorded but contributed no tasks (the only task id
    # collided and was skipped at INSERT time).
    plan_a_rows = _query_db(
        database_url,
        "SELECT id FROM plans WHERE id = $1",
        "plan-attacker",
    )
    assert len(plan_a_rows) == 1
    plan_a_tasks = _query_db(
        database_url,
        "SELECT id FROM tasks WHERE plan_id = $1",
        "plan-attacker",
    )
    assert plan_a_tasks == []


def test_strict_apply_still_skips_own_failing_tasks(
    database_url: str,
    tmp_path: Path,
) -> None:
    """The cross-plan guard does not regress single-plan strict-skip behavior."""
    payload = {
        "plan_id": "plan-solo",
        "project": "Solo Plan",
        "tasks": [
            {
                "id": "T-OK-1",
                "status": "PENDING",
                "priority": "high",
                "description": "Implement the feature flag rollout for the dashboard.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["dashboard shows the flag"],
                "test_steps": ["pytest -k dashboard"],
                "prd_requirement": "",
            },
            {
                "id": "T-BAD-EMPTY-AC",
                "status": "PENDING",
                "priority": "medium",
                "description": "Refactor logging utilities to use structlog throughout.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": ["pytest -k logging"],
                "prd_requirement": "",
            },
        ],
    }
    plan_file = _write_plan(tmp_path, "solo.json", payload)
    assert run_plan_command(["apply", "--strict", str(plan_file)]) == EXIT_OK

    rows = _query_db(
        database_url,
        "SELECT id, status FROM tasks WHERE plan_id = $1 ORDER BY id",
        "plan-solo",
    )
    by_id = {row["id"]: row["status"] for row in rows}
    assert by_id == {"T-OK-1": "PENDING", "T-BAD-EMPTY-AC": "SKIPPED"}

    skip_events = _query_db(
        database_url,
        "SELECT task_id FROM events WHERE event_type = 'SKIP'",
    )
    assert {row["task_id"] for row in skip_events} == {"T-BAD-EMPTY-AC"}
