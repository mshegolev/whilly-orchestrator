"""Integration: pr.iteration.completed event when *-rev-N reaches COMPLETE (VAL-PR-025).

Drives :func:`whilly.workflow.pr_iterate.emit_iteration_completed`
against a testcontainer-backed Postgres + JSONL sink, asserting:

* When ``task_id`` matches ``*-rev-N``, exactly one
  ``pr.iteration.completed`` event is inserted into ``events``
  whose ``payload`` contains ``orig_task_id``, ``new_task_id``, and
  ``iteration`` (1-indexed). Mirrored to JSONL with byte-identical
  payload (VAL-PR-004's round-trip contract).
* When ``task_id`` is a normal originating task (no ``-rev-N``
  suffix), the helper returns ``None`` and emits nothing — a normal
  COMPLETE on a non-rev task must not produce a stray
  ``pr.iteration.completed`` row.

Pairs with the unit-level cap test under
``tests/unit/test_pr_iterate_cap.py`` and the followup test under
``tests/integration/test_pr_iterate_followup.py`` to cover the full
``requested → completed`` lifecycle.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import (
    PR_ITERATION_COMPLETED_EVENT_TYPE,
    TaskRepository,
)
from whilly.audit import DEFAULT_JSONL_FILENAME, JsonlEventSink, LOG_DIR_ENV
from whilly.workflow.pr_iterate import emit_iteration_completed, parse_rev_task_id

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-PR-ITER-COMPLETE"
ORIG_TASK_ID = "task-99"
REV_TASK_ID = f"{ORIG_TASK_ID}-rev-1"


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    log_dir = tmp_path / "whilly_logs"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    yield log_dir


async def _seed_plan_orig_and_rev(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", PLAN_ID, "iter-complete")
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES
              ($1, $3, 'DONE', '[]'::jsonb, '[]'::jsonb,
               'medium', 'orig', '[]'::jsonb, '[]'::jsonb, '', 0),
              ($2, $3, 'DONE', $4::jsonb, '[]'::jsonb,
               'medium', 'rev', '[]'::jsonb, '[]'::jsonb,
               'https://github.com/x/y/pull/99', 0)
            """,
            ORIG_TASK_ID,
            REV_TASK_ID,
            PLAN_ID,
            json.dumps([ORIG_TASK_ID]),
        )


def _read_jsonl_lines(jsonl_path: Path) -> list[dict[str, object]]:
    if not jsonl_path.is_file():
        return []
    return [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# parse_rev_task_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("task-99-rev-1", ("task-99", 1)),
        ("GH-123-rev-2", ("GH-123", 2)),
        ("JIRA-PROJ-42-rev-13", ("JIRA-PROJ-42", 13)),
        ("task-99", None),
        ("just-text", None),
        ("rev-1", None),
    ],
)
def test_parse_rev_task_id_rules(task_id: str, expected: tuple[str, int] | None) -> None:
    assert parse_rev_task_id(task_id) == expected


# ---------------------------------------------------------------------------
# emit_iteration_completed: rev task → one event in PG + JSONL
# ---------------------------------------------------------------------------


async def test_emit_iteration_completed_inserts_one_event_for_rev_task(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> None:
    await _seed_plan_orig_and_rev(db_pool)
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    event_id = await emit_iteration_completed(
        repo=repo,
        plan_id=PLAN_ID,
        task_id=REV_TASK_ID,
    )
    assert isinstance(event_id, int)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, payload, task_id, plan_id FROM events WHERE plan_id = $1 ORDER BY id",
            PLAN_ID,
        )
    completed = [r for r in rows if r["event_type"] == PR_ITERATION_COMPLETED_EVENT_TYPE]
    assert len(completed) == 1, f"expected 1 pr.iteration.completed, got {[r['event_type'] for r in rows]!r}"
    pg_payload = completed[0]["payload"]
    if isinstance(pg_payload, str):
        pg_payload = json.loads(pg_payload)
    assert pg_payload == {
        "orig_task_id": ORIG_TASK_ID,
        "new_task_id": REV_TASK_ID,
        "iteration": 1,
    }
    assert completed[0]["task_id"] == REV_TASK_ID
    assert completed[0]["plan_id"] == PLAN_ID

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    jsonl_completed = [line for line in jsonl_lines if line["event_type"] == PR_ITERATION_COMPLETED_EVENT_TYPE]
    assert len(jsonl_completed) == 1
    assert jsonl_completed[0]["payload"] == pg_payload


# ---------------------------------------------------------------------------
# emit_iteration_completed: non-rev task → no event
# ---------------------------------------------------------------------------


async def test_emit_iteration_completed_noop_for_non_rev_task(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> None:
    await _seed_plan_orig_and_rev(db_pool)
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    result = await emit_iteration_completed(
        repo=repo,
        plan_id=PLAN_ID,
        task_id=ORIG_TASK_ID,
    )
    assert result is None

    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type FROM events WHERE plan_id = $1",
            PLAN_ID,
        )
    completed = [r for r in events if r["event_type"] == PR_ITERATION_COMPLETED_EVENT_TYPE]
    assert completed == []
    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    assert all(line["event_type"] != PR_ITERATION_COMPLETED_EVENT_TYPE for line in jsonl_lines)
