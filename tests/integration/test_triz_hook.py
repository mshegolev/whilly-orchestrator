"""Integration tests for the TRIZ executor hook (TASK-104b).

Mirrors VAL-TRIZ-001/002/004/008/009/010/011/015 in
``validation-contract.md``. Each test exercises the real
:meth:`TaskRepository.fail_task` against a testcontainers Postgres
seeded with a healthy plan + one CLAIMED task. The TRIZ subprocess
itself is mocked via :func:`monkeypatch.setattr` on
``whilly.core.triz.subprocess.run`` (and ``shutil.which``); the
``WHILLY_TRIZ_ENABLED`` env var is toggled per test to exercise the
gate.

Why integration vs. unit?
-------------------------
The hook composes ``fail_task``'s state-machine transition, the
``events.detail`` JSONB column write (VAL-TRIZ-008 / VAL-TRIZ-009),
and the ordering invariant between the FAIL row and the
``triz.contradiction`` row (VAL-TRIZ-010). Only a real database
exercises every piece end-to-end.
"""

from __future__ import annotations

import inspect
import json
import logging
import subprocess
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import TaskRepository
from whilly.core.models import TaskStatus

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-TRIZ-001"
TASK_ID = "T-TRIZ-001"
WORKER_ID = "w-triz-1"


# ─── seeding helpers ─────────────────────────────────────────────────────


async def _seed_plan_task_worker(
    pool: asyncpg.Pool,
    *,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    description: str = "Cache must be both consistent and eventually-consistent.",
    plan_id: str = PLAN_ID,
    task_id: str = TASK_ID,
    worker_id: str = WORKER_ID,
    version: int = 1,
) -> None:
    """Insert one plan + one worker + one task in the requested status."""
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", plan_id, "triz-test")
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            "host",
            "hash",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria, test_steps,
                prd_requirement, version, claimed_by, claimed_at
            )
            VALUES ($1, $2, $3, '[]'::jsonb, '[]'::jsonb,
                    'medium', $4, '[]'::jsonb, '[]'::jsonb,
                    '', $5, $6, NOW())
            """,
            task_id,
            plan_id,
            status.value,
            description,
            version,
            worker_id,
        )


# ─── helpers / fixtures ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_triz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-enable WHILLY_TRIZ_ENABLED for hook tests; individual tests opt out."""
    monkeypatch.setenv("WHILLY_TRIZ_ENABLED", "1")
    monkeypatch.setattr(
        "whilly.core.triz.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )


def _make_recording_run(*, stdout: str = "", returncode: int = 0, raises: BaseException | None = None):  # type: ignore[no-untyped-def]
    """Return a stub for ``subprocess.run`` that records calls + returns canned output."""
    calls: list[tuple[Any, dict[str, Any]]] = []

    def _stub(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if raises is not None:
            raise raises
        cmd = args[0] if args else kwargs.get("args")
        return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr="")

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


# ─── VAL-TRIZ-008 / VAL-TRIZ-009: fail_task signature accepts detail kwarg ─


def test_fail_task_signature_carries_keyword_only_detail() -> None:
    """``fail_task`` exposes ``detail: dict | None = None`` as keyword-only."""
    params = inspect.signature(TaskRepository.fail_task).parameters
    assert "detail" in params
    detail_param = params["detail"]
    assert detail_param.kind == inspect.Parameter.KEYWORD_ONLY
    assert detail_param.default is None


# ─── VAL-TRIZ-008: detail dict round-trips into events.detail JSONB ─────


async def test_fail_task_persists_detail_into_events_detail_column(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``detail={'k': 'v'}`` round-trips as JSONB on the FAIL event row."""
    # Disable TRIZ for this test — we only care about the FAIL row's detail.
    monkeypatch.delenv("WHILLY_TRIZ_ENABLED", raising=False)
    await _seed_plan_task_worker(db_pool)

    updated = await task_repo.fail_task(TASK_ID, version=1, reason="boom", detail={"k": "v"})
    assert updated.status == TaskStatus.FAILED

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT detail, payload FROM events WHERE task_id=$1 AND event_type='FAIL'",
            TASK_ID,
        )
    assert len(rows) == 1
    detail = rows[0]["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert detail == {"k": "v"}
    payload = rows[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["reason"] == "boom"


# ─── VAL-TRIZ-009: detail=None round-trips as SQL NULL ──────────────────


async def test_fail_task_without_detail_writes_sql_null(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``detail`` (or passing ``None``) writes SQL NULL — never literal ``null`` text or ``{}``."""
    monkeypatch.delenv("WHILLY_TRIZ_ENABLED", raising=False)
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="boom")

    async with db_pool.acquire() as conn:
        is_null = await conn.fetchval(
            "SELECT (detail IS NULL) FROM events WHERE task_id=$1 AND event_type='FAIL'",
            TASK_ID,
        )
        text_value = await conn.fetchval(
            "SELECT detail::text FROM events WHERE task_id=$1 AND event_type='FAIL'",
            TASK_ID,
        )
    assert is_null is True
    assert text_value is None  # not the string "null", not "{}"


# ─── VAL-TRIZ-001: contradiction-finding TRIZ run writes triz.contradiction ─


async def test_contradiction_finding_writes_triz_contradiction_event(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mocked TRIZ verdict carries into a separate ``triz.contradiction`` event row."""
    payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": (
                "improving consistency worsens replica latency by an order of magnitude — "
                "the cache cannot be both fully consistent and fast"
            ),
        }
    )
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _make_recording_run(stdout=payload))
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="agent crashed")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT detail FROM events WHERE task_id=$1 AND event_type='triz.contradiction'",
            TASK_ID,
        )
    assert len(rows) == 1
    detail = rows[0]["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert "contradiction_type" in detail
    assert "reason" in detail
    assert detail["contradiction_type"] == "technical"


# ─── VAL-TRIZ-010: FAIL row precedes triz.contradiction row ─────────────


async def test_fail_event_ordered_before_triz_contradiction(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both FAIL and triz.contradiction rows exist; FAIL ≤ triz in ``created_at``."""
    payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": "improving X worsens Y — clear technical contradiction",
        }
    )
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _make_recording_run(stdout=payload))
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="boom")

    async with db_pool.acquire() as conn:
        ordered = await conn.fetch(
            """
            SELECT event_type FROM events WHERE task_id=$1
              AND event_type IN ('FAIL', 'triz.contradiction')
            ORDER BY created_at ASC, id ASC
            """,
            TASK_ID,
        )
        status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", TASK_ID)
    assert [r["event_type"] for r in ordered] == ["FAIL", "triz.contradiction"]
    assert status == "FAILED"


# ─── VAL-TRIZ-002: no contradiction → no triz event row ─────────────────


async def test_no_contradiction_writes_no_triz_event(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``contradictory: false`` → FAIL row only, zero triz.contradiction rows."""
    payload = json.dumps({"contradictory": False, "contradiction_type": "", "reason": ""})
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _make_recording_run(stdout=payload))
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="boom")

    async with db_pool.acquire() as conn:
        triz_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type='triz.contradiction'",
            TASK_ID,
        )
        fail_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type='FAIL'",
            TASK_ID,
        )
        status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", TASK_ID)
    assert triz_count == 0
    assert fail_count == 1
    assert status == "FAILED"


# ─── VAL-TRIZ-004: timeout → triz.error event with reason='timeout' ─────


async def test_subprocess_timeout_writes_triz_error_event(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess TimeoutExpired → one triz.error row (reason='timeout'), no triz.contradiction."""
    monkeypatch.setattr(
        "whilly.core.triz.subprocess.run",
        _make_recording_run(raises=subprocess.TimeoutExpired(cmd=["claude"], timeout=25)),
    )
    await _seed_plan_task_worker(db_pool)

    updated = await task_repo.fail_task(TASK_ID, version=1, reason="boom")
    assert updated.status == TaskStatus.FAILED

    async with db_pool.acquire() as conn:
        err_rows = await conn.fetch(
            "SELECT detail FROM events WHERE task_id=$1 AND event_type='triz.error'",
            TASK_ID,
        )
        triz_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type='triz.contradiction'",
            TASK_ID,
        )
    assert len(err_rows) == 1
    detail = err_rows[0]["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert detail.get("reason") == "timeout"
    assert triz_count == 0


# ─── VAL-TRIZ-003: claude absent → no triz event, FAIL preserved ────────


async def test_claude_missing_skips_triz_but_keeps_fail(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``shutil.which('claude')`` returns None → no triz event row; FAIL still landed."""
    # Override the autouse "_enable_triz" patches: claude is absent.
    monkeypatch.setattr("whilly.core.triz.shutil.which", lambda _name: None)
    runner = _make_recording_run(stdout="should not run")
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)
    await _seed_plan_task_worker(db_pool)

    with caplog.at_level(logging.WARNING, logger="whilly.core.triz"):
        await task_repo.fail_task(TASK_ID, version=1, reason="boom")

    # triz analyzer never invoked claude, no triz events.
    assert runner.calls == []  # type: ignore[attr-defined]

    async with db_pool.acquire() as conn:
        triz_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type LIKE 'triz.%'",
            TASK_ID,
        )
        fail_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type='FAIL'",
            TASK_ID,
        )
        status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", TASK_ID)
    assert triz_rows == 0
    assert fail_rows == 1
    assert status == "FAILED"

    # Exactly one warning on whilly.core.triz logger (claude_missing).
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "whilly.core.triz"]
    assert len(warnings) == 1
    assert "claude" in warnings[0].getMessage().lower()


# ─── VAL-TRIZ-005 (integration counterpart): malformed JSON → no triz event ─


async def test_malformed_claude_output_writes_no_triz_event(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage stdout → analyze returns None, no triz.contradiction event row."""
    monkeypatch.setattr(
        "whilly.core.triz.subprocess.run",
        _make_recording_run(stdout="<<not json>>", returncode=0),
    )
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="boom")

    async with db_pool.acquire() as conn:
        triz_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type LIKE 'triz.%'",
            TASK_ID,
        )
        status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", TASK_ID)
    assert triz_count == 0
    assert status == "FAILED"


# ─── VAL-TRIZ-011: no TRIZ when feature flag off ────────────────────────


async def test_triz_disabled_records_no_triz_event(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WHILLY_TRIZ_ENABLED`` unset → analyze never invoked; no triz events."""
    monkeypatch.delenv("WHILLY_TRIZ_ENABLED", raising=False)
    runner = _make_recording_run(stdout="should not run")
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="boom")

    # Recording stub captured zero calls — analyze_contradiction was never called.
    assert runner.calls == []  # type: ignore[attr-defined]

    async with db_pool.acquire() as conn:
        triz_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id=$1 AND event_type LIKE 'triz.%'",
            TASK_ID,
        )
    assert triz_count == 0


@pytest.mark.parametrize("env_value", ["0", "false", "off", ""])
async def test_triz_disabled_for_non_one_env_values(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
) -> None:
    """Only ``WHILLY_TRIZ_ENABLED=1`` activates the hook; everything else is off."""
    monkeypatch.setenv("WHILLY_TRIZ_ENABLED", env_value)
    runner = _make_recording_run(stdout="should not run")
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)
    await _seed_plan_task_worker(db_pool)

    await task_repo.fail_task(TASK_ID, version=1, reason="boom")
    assert runner.calls == []  # type: ignore[attr-defined]


# ─── VAL-TRIZ-015: hook never re-raises into the executor ──────────────


@pytest.mark.parametrize(
    "stdout_or_raises",
    [
        ("garbage", None),  # malformed JSON
        ("", subprocess.TimeoutExpired(cmd=["claude"], timeout=25)),  # timeout
        ("", FileNotFoundError("vanished")),  # claude vanished
    ],
)
async def test_fail_task_returns_normally_on_every_failure_mode(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
    stdout_or_raises: tuple[str, BaseException | None],
) -> None:
    """For every documented failure mode, ``fail_task`` returns a FAILED Task without raising."""
    stdout, raises = stdout_or_raises
    monkeypatch.setattr(
        "whilly.core.triz.subprocess.run",
        _make_recording_run(stdout=stdout, raises=raises),
    )
    await _seed_plan_task_worker(db_pool)

    updated = await task_repo.fail_task(TASK_ID, version=1, reason="boom")
    assert updated.status == TaskStatus.FAILED


# ─── Wall-clock guard: triz hook does NOT extend fail_task beyond the 25s budget ─


async def test_fail_task_completes_promptly_on_mocked_subprocess(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mocked subprocess returns instantly → fail_task returns within a tight bound."""
    import time

    payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": "improving X worsens Y — a textbook technical contradiction",
        }
    )
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _make_recording_run(stdout=payload))
    await _seed_plan_task_worker(db_pool)

    t0 = time.monotonic()
    await task_repo.fail_task(TASK_ID, version=1, reason="boom")
    elapsed = time.monotonic() - t0

    # Tight bound — even on a slow CI box, mocked TRIZ should land in well
    # under a second; the 5s ceiling is a defence-in-depth signal that no
    # one accidentally added a real subprocess to the hook.
    assert elapsed < 5.0
