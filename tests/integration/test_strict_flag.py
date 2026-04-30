"""Integration tests for ``whilly plan apply --strict`` (TASK-104c).

Mirrors VAL-GATES-016 through VAL-GATES-022 in
``validation-contract.md`` plus the SKIPPED-survives-restart
invariant (VAL-GATES-021). All tests drive ``run_plan_command``
synchronously because the CLI owns its own ``asyncio.run`` calls;
seeding / verification helpers wrap their own ``asyncio.run`` against
freshly-opened pools tied to the session-scoped Postgres container.

Why integration vs. unit?
-------------------------
The ``--strict`` slot composes plan parsing, cycle detection, plan
INSERT, the pure Decision Gate, and ``skip_task`` SQL — only the
DB-side behaviour proves the operator-facing contract. Mocking
asyncpg here would only assert we *call* the right method, not that
the SKIP event row carries the documented payload shape (the failure
mode the test is designed to catch).
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
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_OK,
    EXIT_VALIDATION_ERROR,
    run_plan_command,
)

pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` and TRUNCATE tables for one test.

    Mirrors the fixture in ``test_plan_reset.py``: ``run_plan_command``
    reads the DSN from the environment, and we want a clean slate per
    test without depending on the async ``db_pool`` fixture's loop.
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
def healthy_plan_payload() -> dict[str, Any]:
    """A plan whose every task passes the Decision Gate."""
    return {
        "plan_id": "plan-strict-healthy",
        "project": "Strict Healthy",
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
                "id": "T-OK-2",
                "status": "PENDING",
                "priority": "medium",
                "description": "Wire telemetry into the new feature flag flow.",
                "dependencies": ["T-OK-1"],
                "key_files": [],
                "acceptance_criteria": ["telemetry emits one event per click"],
                "test_steps": ["pytest -k telemetry"],
                "prd_requirement": "",
            },
        ],
    }


@pytest.fixture
def mixed_plan_payload() -> dict[str, Any]:
    """A plan with two healthy tasks and two gate-rejecting ones."""
    return {
        "plan_id": "plan-strict-mixed",
        "project": "Strict Mixed",
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
                "acceptance_criteria": [],  # ← gate REJECT
                "test_steps": ["pytest -k logging"],
                "prd_requirement": "",
            },
            {
                "id": "T-BAD-EMPTY-STEPS",
                "status": "PENDING",
                "priority": "low",
                "description": "Document onboarding instructions for new contributors.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["README has section"],
                "test_steps": [],  # ← gate REJECT
                "prd_requirement": "",
            },
            {
                "id": "T-OK-2",
                "status": "PENDING",
                "priority": "medium",
                "description": "Wire telemetry into the new feature flag flow.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["telemetry emits one event per click"],
                "test_steps": ["pytest -k telemetry"],
                "prd_requirement": "",
            },
        ],
    }


@pytest.fixture
def cyclic_plan_payload() -> dict[str, Any]:
    """A plan with a A→B→A cycle to exercise VAL-GATES-019."""
    return {
        "plan_id": "plan-strict-cycle",
        "project": "Strict Cycle",
        "tasks": [
            {
                "id": "T-A",
                "status": "PENDING",
                "priority": "medium",
                "description": "First task in the cycle (long enough description).",
                "dependencies": ["T-B"],
                "key_files": [],
                "acceptance_criteria": ["criterion-a"],
                "test_steps": ["step-a"],
                "prd_requirement": "",
            },
            {
                "id": "T-B",
                "status": "PENDING",
                "priority": "medium",
                "description": "Second task in the cycle (long enough description).",
                "dependencies": ["T-A"],
                "key_files": [],
                "acceptance_criteria": ["criterion-b"],
                "test_steps": ["step-b"],
                "prd_requirement": "",
            },
        ],
    }


def _write_plan(tmp_path: Path, payload: dict[str, Any]) -> Path:
    target = tmp_path / "plan.json"
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


# ─── VAL-GATES-017: --strict on healthy plan → exit 0, all imported ─────


def test_strict_accepts_healthy_plan(
    database_url: str,
    healthy_plan_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every task passes the gate → exit 0, plan + tasks in DB, no SKIP events."""
    plan_file = _write_plan(tmp_path, healthy_plan_payload)
    rc = run_plan_command(["apply", "--strict", str(plan_file)])
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    assert "applied plan" in captured.out

    plans = _query_db(database_url, "SELECT id FROM plans WHERE id = $1", "plan-strict-healthy")
    assert len(plans) == 1

    tasks = _query_db(
        database_url,
        "SELECT id, status FROM tasks WHERE plan_id = $1 ORDER BY id",
        "plan-strict-healthy",
    )
    assert {row["id"] for row in tasks} == {"T-OK-1", "T-OK-2"}
    assert all(row["status"] == "PENDING" for row in tasks)

    # M3 fix-feature anti-regression: the canonical literal is
    # 'task.skipped' (lowercase dotted); zero rows of that literal AND
    # zero rows of the legacy 'SKIP' literal must exist after a
    # healthy plan apply.
    skip_events = _query_db(
        database_url,
        "SELECT id FROM events WHERE event_type = 'task.skipped'",
    )
    assert skip_events == []
    legacy_skip_events = _query_db(
        database_url,
        "SELECT id FROM events WHERE event_type = 'SKIP'",
    )
    assert legacy_skip_events == [], "regression: legacy uppercase 'SKIP' event_type leaked into events table"


# ─── VAL-CROSS-003 + feature description: --strict skips REJECT tasks ───


def test_strict_skips_reject_verdict_tasks(
    database_url: str,
    mixed_plan_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mixed plan + --strict: gate-failing tasks land as SKIPPED with payload."""
    plan_file = _write_plan(tmp_path, mixed_plan_payload)
    rc = run_plan_command(["apply", "--strict", str(plan_file)])
    assert rc == EXIT_OK

    rows = _query_db(
        database_url,
        "SELECT id, status FROM tasks WHERE plan_id = $1 ORDER BY id",
        "plan-strict-mixed",
    )
    by_id = {row["id"]: row["status"] for row in rows}
    assert by_id == {
        "T-OK-1": "PENDING",
        "T-OK-2": "PENDING",
        "T-BAD-EMPTY-AC": "SKIPPED",
        "T-BAD-EMPTY-STEPS": "SKIPPED",
    }

    # M3 fix-feature: events.event_type is the contract literal
    # 'task.skipped' (lowercase dotted); events.plan_id is populated
    # from tasks.plan_id (RETURNING tasks.plan_id was added to
    # _SKIP_SQL specifically for this assertion).
    events = _query_db(
        database_url,
        "SELECT task_id, plan_id, event_type, payload FROM events WHERE event_type = 'task.skipped' ORDER BY task_id",
    )
    assert {e["task_id"] for e in events} == {"T-BAD-EMPTY-AC", "T-BAD-EMPTY-STEPS"}
    # Every skip event row carries the parent plan_id (cross-flow
    # contract VAL-CROSS-003 / VAL-CROSS-004).
    for row in events:
        assert row["plan_id"] == "plan-strict-mixed", (
            f"task.skipped event for task={row['task_id']} missing plan_id; got {row['plan_id']!r}"
        )

    # Anti-regression: zero rows of the legacy uppercase 'SKIP'
    # literal must exist; the contract literal is the lowercase
    # dotted form (M3 fix-feature, drop the 'OR SKIP' permissiveness).
    legacy_rows = _query_db(
        database_url,
        "SELECT id FROM events WHERE event_type = 'SKIP'",
    )
    assert legacy_rows == [], (
        "regression: legacy uppercase 'SKIP' event_type was written "
        "by skip_task; the canonical literal is 'task.skipped'"
    )

    payload_by_task: dict[str, dict[str, Any]] = {}
    for row in events:
        payload_raw = row["payload"]
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        payload_by_task[row["task_id"]] = payload

    assert payload_by_task["T-BAD-EMPTY-AC"]["reason"] == "decision_gate_failed"
    assert "acceptance_criteria" in payload_by_task["T-BAD-EMPTY-AC"]["missing"]
    assert payload_by_task["T-BAD-EMPTY-STEPS"]["reason"] == "decision_gate_failed"
    assert "test_steps" in payload_by_task["T-BAD-EMPTY-STEPS"]["missing"]

    captured = capsys.readouterr()
    assert "T-BAD-EMPTY-AC" in captured.out
    assert "T-BAD-EMPTY-STEPS" in captured.out


# ─── VAL-GATES-018: default mode imports failing tasks with stderr warning ──


def test_default_mode_imports_failing_tasks_with_warning(
    database_url: str,
    mixed_plan_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --strict: every task imported PENDING; stderr names each failing task."""
    plan_file = _write_plan(tmp_path, mixed_plan_payload)
    rc = run_plan_command(["apply", str(plan_file)])
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    # Stderr contains a warning naming each failing task id and the
    # `decision_gate` substring (PRD VAL-GATES-018).
    assert "decision_gate" in captured.err
    assert "T-BAD-EMPTY-AC" in captured.err
    assert "T-BAD-EMPTY-STEPS" in captured.err

    rows = _query_db(
        database_url,
        "SELECT id, status FROM tasks WHERE plan_id = $1 ORDER BY id",
        "plan-strict-mixed",
    )
    assert len(rows) == 4
    # Default mode: no task is auto-skipped — every task stays PENDING.
    assert all(row["status"] == "PENDING" for row in rows)


# ─── VAL-GATES-022: default mode writes zero SKIP events ────────────────


def test_default_mode_writes_zero_skip_events(
    database_url: str,
    mixed_plan_payload: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Default mode: events table has zero ``task.skipped`` rows after apply.

    M3 fix-feature: the contract literal is the lowercase dotted
    form. The ``OR 'SKIP'`` permissiveness was a transitional cover
    that masked the M1 implementation drift; with the implementation
    now writing ``'task.skipped'`` exclusively, the strict query
    catches both the canonical zero-count contract AND the
    no-legacy-leak regression invariant in one read.
    """
    plan_file = _write_plan(tmp_path, mixed_plan_payload)
    assert run_plan_command(["apply", str(plan_file)]) == EXIT_OK

    rows = _query_db(
        database_url,
        "SELECT id FROM events WHERE event_type = 'task.skipped'",
    )
    assert rows == [], f"default mode wrote {len(rows)} task.skipped events"

    # Defence in depth: also assert zero rows of the legacy literal
    # — this is the explicit anti-regression guard requested by
    # VAL-GATES-022's M3 fix-feature update (drop the transitional
    # ``IN ('task.skipped', 'SKIP')`` permissiveness).
    legacy_rows = _query_db(
        database_url,
        "SELECT id FROM events WHERE event_type = 'SKIP'",
    )
    assert legacy_rows == [], f"regression: default mode leaked {len(legacy_rows)} legacy 'SKIP' events"


# ─── VAL-GATES-019: --strict cycle errors at exit 1 ─────────────────────


def test_strict_reports_cycle_at_exit_1(
    database_url: str,
    cyclic_plan_payload: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cycle wins over gate: exit 1, stderr names the cycle, no DB rows."""
    plan_file = _write_plan(tmp_path, cyclic_plan_payload)
    rc = run_plan_command(["apply", "--strict", str(plan_file)])
    assert rc == EXIT_VALIDATION_ERROR

    captured = capsys.readouterr()
    assert "Cycle detected" in captured.err

    plans = _query_db(database_url, "SELECT id FROM plans WHERE id = $1", "plan-strict-cycle")
    assert plans == []

    tasks = _query_db(database_url, "SELECT id FROM tasks WHERE plan_id = $1", "plan-strict-cycle")
    assert tasks == []


# ─── VAL-GATES-020: --help shows --strict + Decision Gate ───────────────


def test_apply_help_shows_strict_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``whilly plan apply --help`` mentions ``--strict`` and Decision Gate."""
    with pytest.raises(SystemExit) as exc_info:
        run_plan_command(["apply", "--help"])
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "--strict" in captured.out
    # Help text references Decision Gate so operators understand the
    # behaviour without consulting the docstring (PRD VAL-GATES-020).
    assert "Decision Gate" in captured.out


# ─── VAL-GATES-021: SKIPPED survives a re-run ────────────────────────────


def test_skipped_tasks_remain_skipped_across_reruns(
    database_url: str,
    mixed_plan_payload: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A SKIPPED task stays SKIPPED on a subsequent --strict run.

    Mirrors VAL-GATES-021 in spirit: rather than spinning up a second
    LifespanManager (which the FastAPI assertion targets), we
    re-invoke the CLI handler against the same DB. Both paths exercise
    the same invariant: the orchestrator must not silently roll a
    SKIPPED row back to PENDING on startup, and re-running the gate
    must not create duplicate ``SKIP`` events for already-skipped
    rows.
    """
    plan_file = _write_plan(tmp_path, mixed_plan_payload)

    # First strict run — populates SKIPPED rows.
    assert run_plan_command(["apply", "--strict", str(plan_file)]) == EXIT_OK

    rows_before = _query_db(
        database_url,
        "SELECT id, status, version FROM tasks WHERE plan_id = $1 ORDER BY id",
        "plan-strict-mixed",
    )
    skip_count_before = _query_db(
        database_url,
        "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.skipped'",
    )[0]["c"]

    # Second strict run — must be idempotent.
    assert run_plan_command(["apply", "--strict", str(plan_file)]) == EXIT_OK

    rows_after = _query_db(
        database_url,
        "SELECT id, status, version FROM tasks WHERE plan_id = $1 ORDER BY id",
        "plan-strict-mixed",
    )
    skip_count_after = _query_db(
        database_url,
        "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.skipped'",
    )[0]["c"]

    # Status preserved on every SKIPPED task.
    by_id_before = {r["id"]: r["status"] for r in rows_before}
    by_id_after = {r["id"]: r["status"] for r in rows_after}
    assert by_id_before["T-BAD-EMPTY-AC"] == "SKIPPED"
    assert by_id_after["T-BAD-EMPTY-AC"] == "SKIPPED"
    assert by_id_before["T-BAD-EMPTY-STEPS"] == "SKIPPED"
    assert by_id_after["T-BAD-EMPTY-STEPS"] == "SKIPPED"

    # Version unchanged across the second run (idempotent path skips
    # the UPDATE entirely).
    versions_before = {r["id"]: r["version"] for r in rows_before}
    versions_after = {r["id"]: r["version"] for r in rows_after}
    assert versions_before == versions_after

    # Exactly one task.skipped event per failing task — no duplicates
    # after the re-run.
    assert skip_count_before == 2
    assert skip_count_after == 2


# ─── M3 fix-feature: plan.applied event emitted on each apply ────────────


def test_strict_apply_emits_plan_applied_event(
    database_url: str,
    mixed_plan_payload: dict[str, Any],
    tmp_path: Path,
) -> None:
    """``apply --strict`` writes exactly one ``plan.applied`` event."""
    plan_file = _write_plan(tmp_path, mixed_plan_payload)
    assert run_plan_command(["apply", "--strict", str(plan_file)]) == EXIT_OK

    rows = _query_db(
        database_url,
        "SELECT plan_id, payload FROM events WHERE plan_id=$1 AND event_type='plan.applied'",
        "plan-strict-mixed",
    )
    assert len(rows) == 1
    payload_raw = rows[0]["payload"]
    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
    assert payload["tasks_count"] == 4  # 4 tasks in the mixed plan
    assert payload["skipped_count"] == 2
    assert payload["warned_count"] == 0
    assert payload["strict"] is True


def test_default_apply_emits_plan_applied_event(
    database_url: str,
    mixed_plan_payload: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Default ``apply`` (no --strict) writes exactly one ``plan.applied`` event."""
    plan_file = _write_plan(tmp_path, mixed_plan_payload)
    assert run_plan_command(["apply", str(plan_file)]) == EXIT_OK

    rows = _query_db(
        database_url,
        "SELECT plan_id, payload FROM events WHERE plan_id=$1 AND event_type='plan.applied'",
        "plan-strict-mixed",
    )
    assert len(rows) == 1
    payload_raw = rows[0]["payload"]
    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
    assert payload["strict"] is False
    # Default mode: zero skipped, two warned (for the two reject
    # tasks).
    assert payload["skipped_count"] == 0
    assert payload["warned_count"] == 2


# ─── M3 fix-feature: task.created event emitted per inserted task ────────


def test_apply_emits_one_task_created_per_inserted_task(
    database_url: str,
    healthy_plan_payload: dict[str, Any],
    tmp_path: Path,
) -> None:
    """``apply`` writes exactly one ``task.created`` event per newly-inserted task row."""
    plan_file = _write_plan(tmp_path, healthy_plan_payload)
    assert run_plan_command(["apply", str(plan_file)]) == EXIT_OK

    rows = _query_db(
        database_url,
        "SELECT task_id, plan_id FROM events WHERE plan_id=$1 AND event_type='task.created' ORDER BY task_id",
        "plan-strict-healthy",
    )
    assert {r["task_id"] for r in rows} == {"T-OK-1", "T-OK-2"}
    for row in rows:
        assert row["plan_id"] == "plan-strict-healthy"


def test_apply_does_not_duplicate_task_created_on_rerun(
    database_url: str,
    healthy_plan_payload: dict[str, Any],
    tmp_path: Path,
) -> None:
    """``apply`` rerun on the same plan does not duplicate ``task.created`` events.

    Pins VAL-CROSS-005 idempotency for the task.created event: ON
    CONFLICT (id) DO NOTHING returns NULL on the task INSERT, so
    the event emission is skipped on subsequent runs.
    """
    plan_file = _write_plan(tmp_path, healthy_plan_payload)
    assert run_plan_command(["apply", str(plan_file)]) == EXIT_OK
    assert run_plan_command(["apply", str(plan_file)]) == EXIT_OK

    count = _query_db(
        database_url,
        "SELECT COUNT(*) AS c FROM events WHERE plan_id=$1 AND event_type='task.created'",
        "plan-strict-healthy",
    )[0]["c"]
    assert count == 2  # not 4 — second run was idempotent
