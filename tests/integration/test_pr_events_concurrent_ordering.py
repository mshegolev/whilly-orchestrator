"""Concurrent-emission ordering test for the M2 PR event taxonomy.

Pins VAL-CROSS-007 (cross-area assertion 7): two concurrent producers
each emitting 50 distinct PR events to the same plan produce
``events`` rows with monotonically increasing ``id`` values, no
duplicates, and no per-payload cross-talk between drivers. The same
plan-id and task-id are reused across both drivers so the test
exercises the contention path the production poller / iterate
loops will hit when an operator's pgbouncer is sized for parallel
work.

Strategy
--------
Use :func:`asyncio.gather` to run two producer coroutines that each
issue 50 :meth:`whilly.adapters.db.repository.TaskRepository.emit_pr_event`
calls. Each call carries a unique sentinel
(``payload['producer']`` + ``payload['index']``) so the test can
assert post-flight that:

* every ``(producer, index)`` pair appears exactly once;
* no row's payload was overwritten by the other driver
  (per-payload integrity);
* the 100 inserted ``events.id`` values are strictly increasing;
* the JSONL mirror also contains exactly 100 PR lines, with the
  same ``(producer, index)`` set and no duplicates.

The test does NOT use ``threading`` — Postgres + asyncpg are async-
native and the contract is "concurrent emission via asyncio.gather
or threaded". Adding a threaded variant would require the helper to
spin a per-thread loop, which is out of scope for this feature.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import (
    PR_OPENED_EVENT_TYPE,
    PR_REVIEW_APPROVED_EVENT_TYPE,
    PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
    TaskRepository,
)
from whilly.audit import DEFAULT_JSONL_FILENAME, JsonlEventSink, LOG_DIR_ENV

pytestmark = DOCKER_REQUIRED


PLAN_ID: str = "PLAN-PR-CONCURRENT"
TASK_ID: str = "T-PR-CONCURRENT-1"
EVENTS_PER_PRODUCER: int = 50


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
async def seeded_repo(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> Iterator[TaskRepository]:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, 'pr-concurrent')",
            PLAN_ID,
        )
        await conn.execute(
            """
            INSERT INTO tasks (id, plan_id, status, description)
            VALUES ($1, $2, 'PENDING', 'pr concurrent task')
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


# Cycle through three of the PR event types so the test exercises
# the closed-set membership check on every iteration rather than
# committing every row under the same literal.
_ROTATION: tuple[str, ...] = (
    PR_OPENED_EVENT_TYPE,
    PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
    PR_REVIEW_APPROVED_EVENT_TYPE,
)


async def _producer(
    repo: TaskRepository,
    *,
    producer: str,
    count: int,
) -> list[int]:
    inserted: list[int] = []
    for i in range(count):
        event_type = _ROTATION[i % len(_ROTATION)]
        payload: dict[str, object] = {
            "producer": producer,
            "index": i,
            "pr_url": f"https://github.com/x/y/pull/{i}",
            "pr_number": i,
            "head_sha": f"sha-{producer}-{i}",
        }
        event_id = await repo.emit_pr_event(
            event_type,
            plan_id=PLAN_ID,
            task_id=TASK_ID,
            payload=payload,
        )
        inserted.append(event_id)
    return inserted


async def test_concurrent_pr_events_preserve_monotonic_ids_and_payload_integrity(
    seeded_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> None:
    """Two producers × 50 events each → 100 strictly-increasing ids; payloads round-trip per producer."""
    a_ids, b_ids = await asyncio.gather(
        _producer(seeded_repo, producer="A", count=EVENTS_PER_PRODUCER),
        _producer(seeded_repo, producer="B", count=EVENTS_PER_PRODUCER),
    )
    assert len(a_ids) == EVENTS_PER_PRODUCER
    assert len(b_ids) == EVENTS_PER_PRODUCER

    all_ids = sorted(a_ids + b_ids)
    assert len(set(all_ids)) == 2 * EVENTS_PER_PRODUCER, "duplicate events.id detected"

    # ── Postgres side: rows match producer/index sentinels exactly ──
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, event_type, payload FROM events WHERE id = ANY($1::bigint[]) ORDER BY id",
            all_ids,
        )

    assert [row["id"] for row in rows] == all_ids, "rows fetched out of sequence"
    # Strictly increasing — pinned at the assertion level too.
    for previous, current in zip(rows, rows[1:], strict=False):
        assert current["id"] > previous["id"], (
            f"events.id not monotonically increasing: {previous['id']} → {current['id']}"
        )

    seen: set[tuple[str, int]] = set()
    for row in rows:
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        producer = payload["producer"]
        index = payload["index"]
        assert producer in {"A", "B"}, f"unexpected producer label: {producer!r}"
        assert isinstance(index, int)
        # Per-payload integrity: the head_sha must reflect the same
        # producer label that authored the event — proves no
        # cross-talk between drivers (no row carries A's payload
        # under B's sentinel or vice versa).
        assert payload["head_sha"] == f"sha-{producer}-{index}", f"cross-talk detected: payload={payload!r}"
        seen.add((producer, index))

    expected_pairs = {(producer, index) for producer in ("A", "B") for index in range(EVENTS_PER_PRODUCER)}
    assert seen == expected_pairs, f"missing or extra (producer, index) pairs: {seen ^ expected_pairs}"

    # ── JSONL mirror parity ──
    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    pr_jsonl = [
        line
        for line in jsonl_lines
        if isinstance(line.get("payload"), dict)
        and isinstance(line["payload"].get("producer"), str)
        and line["payload"].get("producer") in {"A", "B"}
    ]
    assert len(pr_jsonl) == 2 * EVENTS_PER_PRODUCER, (
        f"expected {2 * EVENTS_PER_PRODUCER} PR JSONL lines, got {len(pr_jsonl)}"
    )

    jsonl_seen: set[tuple[str, int]] = set()
    for line in pr_jsonl:
        payload = line["payload"]
        assert isinstance(payload, dict)
        producer_value = payload["producer"]
        index_value = payload["index"]
        assert isinstance(producer_value, str)
        assert isinstance(index_value, int)
        # No cross-talk in the JSONL mirror either.
        assert payload["head_sha"] == f"sha-{producer_value}-{index_value}"
        jsonl_seen.add((producer_value, index_value))
    assert jsonl_seen == expected_pairs


async def test_concurrent_pr_events_each_event_id_appears_once_in_db_and_jsonl(
    seeded_repo: TaskRepository,
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
) -> None:
    """No duplicate rows, no missing JSONL lines (cardinality match)."""
    a_ids, b_ids = await asyncio.gather(
        _producer(seeded_repo, producer="A", count=EVENTS_PER_PRODUCER),
        _producer(seeded_repo, producer="B", count=EVENTS_PER_PRODUCER),
    )
    all_ids = a_ids + b_ids

    async with db_pool.acquire() as conn:
        db_count = await conn.fetchval(
            "SELECT count(*)::int FROM events WHERE id = ANY($1::bigint[])",
            all_ids,
        )
    assert int(db_count) == 2 * EVENTS_PER_PRODUCER

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    pr_event_types = {
        PR_OPENED_EVENT_TYPE,
        PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
        PR_REVIEW_APPROVED_EVENT_TYPE,
    }
    pr_jsonl = [line for line in jsonl_lines if line["event_type"] in pr_event_types]
    assert len(pr_jsonl) == 2 * EVENTS_PER_PRODUCER, (
        f"jsonl missing PR event mirrors: have {len(pr_jsonl)}, expected {2 * EVENTS_PER_PRODUCER}"
    )
