"""End-to-end integration test for ``whilly_logs/whilly_events.jsonl``.

Pins the v4.3.1 backwards-compatibility contract
(VAL-CROSS-BACKCOMPAT-907): every CLAIM / START / COMPLETE row written
by the local orchestrator must also be appended to
``whilly_logs/whilly_events.jsonl`` so operators who used to
``tail -f`` that file continue to see the same audit stream after the
v4.4 distributed split.

Strategy
--------
Reuse the same fixture pattern from
:mod:`tests.integration.test_local_worker` (testcontainers Postgres
+ ``run_run_command`` injection seam + ``_fake_runner_complete``):

1. Seed a plan with two PENDING tasks.
2. Override ``WHILLY_LOG_DIR`` to a per-test ``tmp_path`` so the
   sink writes outside the repo working tree (a stray
   ``whilly_logs/`` left behind in CI would taint other tests'
   ``git status`` assertions).
3. Drive ``whilly run --plan ...`` through both tasks via the fake
   runner.
4. Assert:
   * ``<tmp_path>/whilly_events.jsonl`` exists and is non-empty.
   * Each line is JSON-parseable and carries the canonical keys.
   * At least one ``CLAIM``, ``START``, ``COMPLETE`` event is present.
   * The CLAIM payload carries the v4.4.0-baseline shape (``worker_id``,
     ``task_id``, ``plan_id``, ``claimed_at``, ``version``).
   * Total line count == event-count for these two tasks (3 events
     per task = 6 minimum), proving every database event was
     mirrored.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from tests.integration.test_local_worker import (
    _fake_runner_complete,
    _seed_plan_with_one_task,
)
from whilly.audit import DEFAULT_JSONL_FILENAME, LOG_DIR_ENV
from whilly.cli.run import EXIT_OK, run_run_command

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-JSONL-E2E"
TASK_ID_A = "T-JSONL-E2E-1"
TASK_ID_B = "T-JSONL-E2E-2"


@pytest.fixture
def db_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Surface the testcontainers DSN through ``WHILLY_DATABASE_URL``."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", postgres_dsn)
    yield postgres_dsn


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate the JSONL sink to a per-test directory.

    The audit sink resolves its directory from ``WHILLY_LOG_DIR``; setting
    it to ``tmp_path`` keeps the test's writes out of the repo so we
    don't pollute the developer's working tree. The fixture returns the
    ``Path`` so callers can build the expected file path directly.
    """
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    return tmp_path


async def _seed_second_task(pool: asyncpg.Pool, plan_id: str, task_id: str) -> None:
    """Insert a second PENDING task into an existing plan."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'high', $3, '[]'::jsonb, '[]'::jsonb, 'FR-1.6', 0)
            """,
            task_id,
            plan_id,
            f"jsonl e2e task {task_id}",
        )


async def test_run_command_writes_whilly_events_jsonl(
    db_pool: asyncpg.Pool,
    db_url: str,
    whilly_log_dir: Path,
) -> None:
    """Local orchestrator mirrors every event into ``whilly_events.jsonl``."""
    await _seed_plan_with_one_task(db_pool, PLAN_ID, TASK_ID_A)
    await _seed_second_task(db_pool, PLAN_ID, TASK_ID_B)

    exit_code = await asyncio.to_thread(
        run_run_command,
        [
            "--plan",
            PLAN_ID,
            "--max-iterations",
            "10",
            "--idle-wait",
            "0.01",
            "--heartbeat-interval",
            "60.0",
        ],
        runner=_fake_runner_complete,
        install_signal_handlers=False,
    )
    assert exit_code == EXIT_OK, f"whilly run exited with {exit_code}, expected {EXIT_OK}"

    # File existence + non-empty contents.
    jsonl_path = whilly_log_dir / DEFAULT_JSONL_FILENAME
    assert jsonl_path.is_file(), f"{jsonl_path} was not created"
    raw = jsonl_path.read_text(encoding="utf-8")
    assert raw, "JSONL file exists but is empty"

    # Each non-blank line parses as JSON with the canonical keys.
    expected_keys = {"ts", "event", "event_type", "task_id", "plan_id", "payload"}
    parsed_lines: list[dict[str, object]] = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        obj = json.loads(line)
        assert isinstance(obj, dict), f"line is not a JSON object: {line!r}"
        assert set(obj.keys()) == expected_keys, f"line missing canonical keys: {sorted(obj.keys())!r}"
        parsed_lines.append(obj)

    # ── Database parity check ─────────────────────────────────────
    # Every events row written by the orchestrator for these two
    # tasks must have a corresponding JSONL line. We bound the lower
    # end (3 events × 2 tasks = 6) but tolerate additional rows
    # added by future features so the assertion stays robust.
    async with db_pool.acquire() as conn:
        db_events = await conn.fetch(
            "SELECT event_type, task_id FROM events WHERE task_id = ANY($1::text[])",
            [TASK_ID_A, TASK_ID_B],
        )
    db_event_count = len(db_events)
    assert db_event_count >= 6, f"expected at least 6 events across two tasks, got {db_event_count}"

    # Filter the JSONL lines to just the per-task lines for these two
    # specific tasks (sweeps with task_id=None or other task ids are
    # ignored so the cardinality match is robust).
    task_lines = [line for line in parsed_lines if line["task_id"] in {TASK_ID_A, TASK_ID_B}]
    assert len(task_lines) >= db_event_count, (
        f"JSONL has {len(task_lines)} task-scoped lines, "
        f"DB has {db_event_count} task-scoped events — sink dropped writes"
    )

    # ── Per-event assertions ──────────────────────────────────────
    event_types = {line["event_type"] for line in parsed_lines}
    assert "CLAIM" in event_types, f"no CLAIM events in JSONL: {event_types!r}"
    assert "START" in event_types, f"no START events in JSONL: {event_types!r}"
    assert "COMPLETE" in event_types, f"no COMPLETE events in JSONL: {event_types!r}"

    # CLAIM payload carries the v4.4.0 enriched shape
    # (VAL-CROSS-BACKCOMPAT-912).
    claim_lines = [line for line in parsed_lines if line["event_type"] == "CLAIM"]
    assert claim_lines, "no CLAIM lines to inspect"
    sample_claim = claim_lines[0]
    payload = sample_claim["payload"]
    assert isinstance(payload, dict)
    for key in ("worker_id", "task_id", "plan_id", "claimed_at", "version"):
        assert key in payload, f"CLAIM payload missing {key!r}: {payload!r}"

    # COMPLETE payload carries usage + version.
    complete_lines = [line for line in parsed_lines if line["event_type"] == "COMPLETE"]
    assert complete_lines, "no COMPLETE lines to inspect"
    sample_complete = complete_lines[0]
    complete_payload = sample_complete["payload"]
    assert isinstance(complete_payload, dict)
    assert "version" in complete_payload, f"COMPLETE payload missing 'version': {complete_payload!r}"

    # Every line has a non-empty ISO ``ts`` value.
    for line in parsed_lines:
        ts_value = line["ts"]
        assert isinstance(ts_value, str) and ts_value, f"empty/missing ts on line: {line!r}"
