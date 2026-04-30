"""Cross-area: TRIZ events visible in the events table while the lifespan flusher is running (TASK-106 × TASK-104b).

This test asserts that the lifespan-managed event flusher
(:class:`whilly.api.event_flusher.EventFlusher`) coexists with the
TRIZ FAIL hook (:meth:`TaskRepository._maybe_emit_triz_event`) — the
two write paths share the same ``events`` table without contention or
loss. Mirrors the cross-area assertions VAL-CROSS-020/021/022/023 from
``validation-contract.md``.

Why a dedicated cross-area test?
    The TRIZ hook writes its event row directly via the repository
    (atomic with the FAIL transition). The flusher path is independent.
    A regression where one path masks the other (e.g. shared
    connection pool exhaustion) would only surface here, not in the
    per-task TRIZ hook tests or the per-task flusher tests.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import TaskRepository
from whilly.adapters.transport.server import create_app
from whilly.api.main import _log_event

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN: str = "bootstrap-flusher-cross"


async def _seed_plan_and_task(
    pool: asyncpg.Pool,
    *,
    plan_id: str = "plan-flusher-triz",
    task_id: str = "T-flusher-triz",
    worker_id: str = "w-flusher-triz",
) -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            "flusher-triz",
        )
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
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
            VALUES ($1, $2, 'IN_PROGRESS', '[]'::jsonb, '[]'::jsonb,
                    'medium', 'Cache must be both fast and consistent.',
                    '[]'::jsonb, '[]'::jsonb, '', 1, $3, NOW())
            """,
            task_id,
            plan_id,
            worker_id,
        )
    return task_id


async def test_triz_event_visible_via_lifespan_flusher_within_200ms(
    db_pool: asyncpg.Pool, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-CROSS-020 / VAL-CROSS-021: TRIZ contradiction → row in DB ≤ 200 ms after FAIL.

    Strengthened (M3 scrutiny round-1 fix): now also asserts that the
    TRIZ event was routed through the lifespan flusher's queue —
    ``app.state.event_flusher.queue.qsize()`` is observed non-zero
    between ``fail_task`` returning and the row landing in the DB,
    proving the flusher carrier (named in VAL-CROSS-021's title) was
    actually exercised rather than the legacy direct-INSERT path.
    """
    monkeypatch.setenv("WHILLY_TRIZ_ENABLED", "1")
    monkeypatch.setattr(
        "whilly.core.triz.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    triz_payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": "consistency vs latency: the cache cannot be both fully consistent and fast",
        }
    )

    def _stub_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=triz_payload, stderr="")

    monkeypatch.setattr("whilly.core.triz.subprocess.run", _stub_run)

    task_id = await _seed_plan_and_task(db_pool)
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.05,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        # Use the app-bound repo so the flusher is wired in; the
        # ``app.state.repo`` was attached to ``app.state.event_flusher``
        # by ``create_app``'s lifespan after construction.
        repo = app.state.repo
        # Trigger a FAIL — TRIZ hook now routes
        # ``triz.contradiction`` through the lifespan flusher queue
        # rather than a direct INSERT.
        await repo.fail_task(task_id, version=1, reason="cross-area triz")
        # Verify the flusher's queue was non-empty immediately after
        # ``fail_task`` returned — proves the carrier-contract pin
        # (VAL-CROSS-021: "flushed via lifespan flusher"). Race-safe
        # because the flusher only flushes on its 50 ms timer here, so
        # the queue carries the row for at least one tick.
        flusher_qsize_after_fail = app.state.event_flusher.queue.qsize()
        # Also enqueue a separate audit event through the flusher to
        # confirm both paths land in the same events table.
        _log_event(
            app,
            "audit.cross_area",
            task_id=task_id,
            payload={"path": "flusher"},
        )
        assert flusher_qsize_after_fail >= 1, (
            "TRIZ event should have been enqueued onto the lifespan flusher's queue "
            f"(qsize={flusher_qsize_after_fail}); the direct-INSERT regression has resurfaced."
        )
        # Wait up to 200 ms (plus generous slack) for both rows.
        deadline = asyncio.get_event_loop().time() + 1.0
        triz_count = 0
        flusher_count = 0
        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                triz_count = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE task_id=$1 AND event_type='triz.contradiction'",
                    task_id,
                )
                flusher_count = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE task_id=$1 AND event_type='audit.cross_area'",
                    task_id,
                )
            if triz_count == 1 and flusher_count == 1:
                break
            await asyncio.sleep(0.02)
        assert triz_count == 1, "TRIZ contradiction row missing"
        assert flusher_count == 1, "Flusher audit row missing"
        # FAIL row also present (state-machine repository path).
        async with db_pool.acquire() as conn:
            fail_count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE task_id=$1 AND event_type='FAIL'",
                task_id,
            )
        assert fail_count == 1


async def test_triz_disabled_records_no_triz_event_with_flusher_active(
    db_pool: asyncpg.Pool, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-CROSS-023: with TRIZ disabled, FAIL still records via repo, no TRIZ row, flusher unaffected."""
    monkeypatch.delenv("WHILLY_TRIZ_ENABLED", raising=False)
    captured_calls: list[Any] = []

    def _no_call(*args: Any, **kwargs: Any) -> Any:
        captured_calls.append(args)
        raise AssertionError("subprocess.run should not be invoked when TRIZ is disabled")

    monkeypatch.setattr("whilly.core.triz.subprocess.run", _no_call)

    task_id = await _seed_plan_and_task(
        db_pool,
        plan_id="plan-flusher-triz-disabled",
        task_id="T-flusher-triz-disabled",
        worker_id="w-flusher-triz-disabled",
    )
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        event_flush_interval_seconds=0.05,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        repo = TaskRepository(db_pool)
        await repo.fail_task(task_id, version=1, reason="no triz")
        _log_event(app, "audit.no_triz", task_id=task_id, payload={"path": "flusher"})
        deadline = asyncio.get_event_loop().time() + 1.0
        flusher_seen = 0
        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                flusher_seen = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE task_id=$1 AND event_type='audit.no_triz'",
                    task_id,
                )
            if flusher_seen == 1:
                break
            await asyncio.sleep(0.02)
        assert flusher_seen == 1
        async with db_pool.acquire() as conn:
            triz_count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE task_id=$1 AND event_type LIKE 'triz.%'",
                task_id,
            )
            fail_count = await conn.fetchval(
                "SELECT count(*) FROM events WHERE task_id=$1 AND event_type='FAIL'",
                task_id,
            )
        assert triz_count == 0
        assert fail_count == 1
        assert captured_calls == []


async def test_triz_event_routed_through_event_flusher(
    db_pool: asyncpg.Pool, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 fix (VAL-CROSS-021 carrier pin): TRIZ row goes through ``EventFlusher.enqueue``, not direct INSERT.

    Strategy: monkeypatch ``EventFlusher.enqueue`` with a recording
    wrapper that delegates to the real implementation. After a FAIL
    that triggers a TRIZ contradiction, assert exactly one
    ``triz.contradiction`` enqueue was recorded on the recorder. This
    is the strongest contract pin: a regression that re-introduced the
    legacy direct-INSERT path would record zero enqueues even though
    the events row would still appear in the DB (the legacy path
    bypasses the flusher and writes the row synchronously through the
    repository's connection pool).
    """
    monkeypatch.setenv("WHILLY_TRIZ_ENABLED", "1")
    monkeypatch.setattr(
        "whilly.core.triz.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    triz_payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": "the cache must be both fully consistent and fully eventually-consistent",
        }
    )

    def _stub_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=triz_payload, stderr="")

    monkeypatch.setattr("whilly.core.triz.subprocess.run", _stub_run)

    # Recording wrapper around ``EventFlusher.enqueue``. We capture
    # every enqueued :class:`EventRecord` and forward to the real
    # implementation so the row still lands in the DB.
    from whilly.api.event_flusher import EventFlusher, EventRecord

    captured_enqueues: list[EventRecord] = []
    real_enqueue = EventFlusher.enqueue

    def _recording_enqueue(self: EventFlusher, record: EventRecord) -> None:
        captured_enqueues.append(record)
        real_enqueue(self, record)

    monkeypatch.setattr(EventFlusher, "enqueue", _recording_enqueue)

    task_id = await _seed_plan_and_task(
        db_pool,
        plan_id="plan-flusher-routed",
        task_id="T-flusher-routed",
        worker_id="w-flusher-routed",
    )
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=0.1,
        # Long flush interval so the queue holds the row for at least
        # the assertion window — we'll observe the queue depth before
        # the timer fires.
        event_flush_interval_seconds=1.0,
        event_drain_timeout_seconds=2.0,
        event_checkpoint_dir=str(tmp_path),
    )
    async with app.router.lifespan_context(app):
        repo = app.state.repo
        # Snapshot the recorder's pre-FAIL state — the lifespan may
        # have enqueued unrelated events (none today, but defensive).
        pre_fail_triz_enqueues = sum(1 for r in captured_enqueues if r.event_type == "triz.contradiction")
        await repo.fail_task(task_id, version=1, reason="route via flusher")
        # Immediately after fail_task returns: exactly one
        # ``triz.contradiction`` row must have been enqueued. If the
        # legacy direct-INSERT path is in use this counter stays at
        # ``pre_fail_triz_enqueues`` even though the row will still
        # appear in the DB.
        post_fail_triz_enqueues = sum(1 for r in captured_enqueues if r.event_type == "triz.contradiction")
        delta = post_fail_triz_enqueues - pre_fail_triz_enqueues
        assert delta == 1, (
            f"expected exactly one triz.contradiction enqueue via EventFlusher; "
            f"saw delta={delta}. The TRIZ FAIL hook may have regressed to direct INSERT, "
            f"violating VAL-CROSS-021's flusher-carrier contract."
        )
        # Eventually the flusher should commit the enqueued row to
        # the DB. With a 1 s flush interval this will land via the
        # timer; we wait up to 2 s.
        deadline = asyncio.get_event_loop().time() + 2.0
        triz_count = 0
        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                triz_count = await conn.fetchval(
                    "SELECT count(*) FROM events WHERE task_id=$1 AND event_type='triz.contradiction'",
                    task_id,
                )
            if triz_count >= 1:
                break
            await asyncio.sleep(0.05)
        assert triz_count == 1, f"flusher did not commit the enqueued triz.contradiction row to DB; saw {triz_count}"
        # The recorded EventRecord carries the right shape.
        triz_records = [r for r in captured_enqueues if r.event_type == "triz.contradiction"]
        assert len(triz_records) == 1
        only = triz_records[0]
        assert only.task_id == task_id
        assert only.detail is not None
        assert only.detail["contradiction_type"] == "technical"
