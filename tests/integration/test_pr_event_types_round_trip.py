"""Integration tests for the M2 PR event-type taxonomy round-trip.

Pins VAL-PR-004: each of ``pr.opened``,
``pr.review.changes_requested``, ``pr.review.approved``,
``pr.iteration.requested``, ``pr.iteration.completed``, ``pr.merged``
round-trips through the canonical events insert path with identical
payloads in Postgres ``events.detail`` (JSONB) and the JSONL audit
mirror at ``whilly_logs/whilly_events.jsonl``. A regression that
drops one mirror — or reshapes the payload between the two sinks —
fails this assertion.

The test drives the public
:meth:`whilly.adapters.db.repository.TaskRepository.emit_pr_event`
helper because that is the single entry point M2 producers (PR
opener, poller, re-iterate path) use; pinning the helper pins every
downstream call site.

The test also asserts:

* ``emit_pr_event`` rejects unknown event_types with
  :class:`ValueError` before any I/O touches the database
  (closed-set guard for the audit log surface).
* The Postgres ``events`` row's ``payload`` JSONB equals the
  Python dict the producer supplied, byte-identical to the
  ``payload`` key on the matching JSONL line.
* The Postgres ``events.event_type`` literal matches the
  helper's ``event_type`` argument verbatim (no rewrite, no
  normalization).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import (
    PR_EVENT_TYPES,
    PR_ITERATION_COMPLETED_EVENT_TYPE,
    PR_ITERATION_REQUESTED_EVENT_TYPE,
    PR_MERGED_EVENT_TYPE,
    PR_OPENED_EVENT_TYPE,
    PR_REVIEW_APPROVED_EVENT_TYPE,
    PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
    TaskRepository,
)
from whilly.audit import DEFAULT_JSONL_FILENAME, JsonlEventSink, LOG_DIR_ENV

pytestmark = DOCKER_REQUIRED


PLAN_ID: str = "PLAN-PR-EVENT-RT"
TASK_ID: str = "T-PR-EVENT-RT-1"


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate the JSONL sink to a per-test directory."""
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
async def seeded_repo(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> Iterator[TaskRepository]:
    """Seed a plan + a task and return a repository with the JSONL sink attached."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, 'pr-event-rt')",
            PLAN_ID,
        )
        await conn.execute(
            """
            INSERT INTO tasks (id, plan_id, status, description)
            VALUES ($1, $2, 'PENDING', 'pr event rt task')
            """,
            TASK_ID,
            PLAN_ID,
        )
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))
    yield repo


def _read_jsonl_lines(jsonl_path: Path) -> list[dict[str, object]]:
    if not jsonl_path.is_file():
        return []
    raw = jsonl_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.split("\n") if line.strip()]


# ---------------------------------------------------------------------------
# All six PR event types covered by VAL-PR-004
# ---------------------------------------------------------------------------


_PR_EVENT_PAYLOADS: list[tuple[str, dict[str, object]]] = [
    (
        PR_OPENED_EVENT_TYPE,
        {
            "pr_url": "https://github.com/x/y/pull/42",
            "pr_number": 42,
            "branch": "feat-task-001",
            "head_sha": "deadbeef",
            "task_id": TASK_ID,
        },
    ),
    (
        PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
        {
            "pr_url": "https://github.com/x/y/pull/42",
            "pr_number": 42,
            "head_sha": "deadbeef",
            "comments": [
                {"body": "please rename foo", "path": "a.py", "line": 10, "author": "alice"},
                {"body": "extract helper", "path": "b.py", "line": 5, "author": "bob"},
            ],
        },
    ),
    (
        PR_REVIEW_APPROVED_EVENT_TYPE,
        {
            "pr_url": "https://github.com/x/y/pull/42",
            "pr_number": 42,
            "head_sha": "deadbeef",
            "reviewer": "alice",
        },
    ),
    (
        PR_ITERATION_REQUESTED_EVENT_TYPE,
        {
            "orig_task_id": TASK_ID,
            "new_task_id": f"{TASK_ID}-rev-1",
            "pr_url": "https://github.com/x/y/pull/42",
            "iteration": 1,
        },
    ),
    (
        PR_ITERATION_COMPLETED_EVENT_TYPE,
        {
            "orig_task_id": TASK_ID,
            "new_task_id": f"{TASK_ID}-rev-1",
            "iteration": 1,
        },
    ),
    (
        PR_MERGED_EVENT_TYPE,
        {
            "pr_url": "https://github.com/x/y/pull/42",
            "pr_number": 42,
            "head_sha": "deadbeef",
            "merged_at": "2026-05-05T12:34:56+00:00",
        },
    ),
]


def test_pr_event_types_taxonomy_covers_all_six() -> None:
    """``PR_EVENT_TYPES`` enumerates exactly the six contract literals."""
    expected = {
        "pr.opened",
        "pr.review.changes_requested",
        "pr.review.approved",
        "pr.iteration.requested",
        "pr.iteration.completed",
        "pr.merged",
    }
    assert set(PR_EVENT_TYPES) == expected, f"PR_EVENT_TYPES taxonomy drift: {set(PR_EVENT_TYPES) ^ expected}"


@pytest.mark.parametrize(("event_type", "payload"), _PR_EVENT_PAYLOADS)
async def test_pr_event_round_trips_through_postgres_and_jsonl(
    seeded_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
    event_type: str,
    payload: dict[str, object],
) -> None:
    """One emit_pr_event call writes one Postgres row + one JSONL line with identical payloads."""
    event_id = await seeded_repo.emit_pr_event(
        event_type,
        plan_id=PLAN_ID,
        task_id=TASK_ID,
        payload=payload,
    )
    assert isinstance(event_id, int)
    assert event_id > 0

    # ── Postgres side ────────────────────────────────────────────
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, event_type, task_id, plan_id, payload FROM events WHERE id = $1",
            event_id,
        )
    assert row is not None, f"no events row for id={event_id}"
    assert row["event_type"] == event_type
    assert row["task_id"] == TASK_ID
    assert row["plan_id"] == PLAN_ID
    pg_payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert pg_payload == payload, f"Postgres payload != supplied dict: {pg_payload!r} vs {payload!r}"

    # ── JSONL mirror ─────────────────────────────────────────────
    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    matching = [line for line in jsonl_lines if line["event_type"] == event_type]
    assert len(matching) == 1, f"expected exactly one JSONL line for {event_type}, got {len(matching)}"
    jsonl_line = matching[0]
    assert jsonl_line["task_id"] == TASK_ID
    assert jsonl_line["plan_id"] == PLAN_ID
    assert jsonl_line["payload"] == payload, f"JSONL payload != supplied dict: {jsonl_line['payload']!r} vs {payload!r}"

    # ── Cross-sink parity (VAL-PR-004 invariant) ─────────────────
    assert jsonl_line["payload"] == pg_payload, (
        "Postgres events.payload and JSONL line payload diverge — round-trip broken"
    )
    # ``event`` legacy alias must match too so v4.3.1 readers see the same string.
    assert jsonl_line["event"] == event_type
    assert jsonl_line["event"] == jsonl_line["event_type"]


# ---------------------------------------------------------------------------
# Closed-set guard (defensive)
# ---------------------------------------------------------------------------


async def test_emit_pr_event_rejects_unknown_event_type(seeded_repo: TaskRepository) -> None:
    """``emit_pr_event`` raises ValueError before any I/O when the event_type is not in the taxonomy."""
    with pytest.raises(ValueError, match="event_type"):
        await seeded_repo.emit_pr_event(
            "pr.something_invented",
            plan_id=PLAN_ID,
            task_id=TASK_ID,
            payload={"k": "v"},
        )


async def test_emit_pr_event_rejects_known_lifecycle_transitions(
    seeded_repo: TaskRepository,
) -> None:
    """The closed set excludes lifecycle transition names (``CLAIM``/``START`` etc.)."""
    for non_pr in ("CLAIM", "START", "COMPLETE", "task.skipped"):
        with pytest.raises(ValueError, match="event_type"):
            await seeded_repo.emit_pr_event(
                non_pr,
                plan_id=PLAN_ID,
                task_id=TASK_ID,
                payload={},
            )


# ---------------------------------------------------------------------------
# Sequential six-event flow (VAL-PR-021's documented order)
# ---------------------------------------------------------------------------


async def test_pr_event_flow_writes_six_events_in_order(
    seeded_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> None:
    """The six-event sequence lands in Postgres in increasing-id order; JSONL mirrors verbatim."""
    inserted_ids: list[int] = []
    for event_type, payload in _PR_EVENT_PAYLOADS:
        event_id = await seeded_repo.emit_pr_event(
            event_type,
            plan_id=PLAN_ID,
            task_id=TASK_ID,
            payload=payload,
        )
        inserted_ids.append(event_id)

    # Strictly increasing.
    assert inserted_ids == sorted(inserted_ids), inserted_ids
    assert len(set(inserted_ids)) == len(inserted_ids), "duplicate events.id values"

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, event_type FROM events WHERE id = ANY($1::bigint[]) ORDER BY id",
            inserted_ids,
        )
    assert [row["event_type"] for row in rows] == [pair[0] for pair in _PR_EVENT_PAYLOADS]

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    pr_lines = [line for line in jsonl_lines if line["event_type"] in dict(_PR_EVENT_PAYLOADS)]
    # Same six event_types appear in JSONL in the same producer order.
    assert [line["event_type"] for line in pr_lines] == [pair[0] for pair in _PR_EVENT_PAYLOADS]
