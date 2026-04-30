"""Unit tests for the per-task TRIZ FAIL hook routing decision (TASK-106 / B2 fix).

The :meth:`whilly.adapters.db.repository.TaskRepository._maybe_emit_triz_event`
method has two write paths:

1. **Lifespan-flusher path** — when the repo was constructed (or
   late-bound) with an :class:`EventFlusherProtocol`, the TRIZ event
   is enqueued via ``flusher.enqueue(EventRecord(...))`` so the
   bulk-INSERT batcher (TASK-106) carries the row. VAL-CROSS-021
   names the flusher as the canonical carrier.

2. **Direct-INSERT fallback** — when no flusher is provided (CLI
   helpers, ``run_local_worker``, direct test fixtures), the hook
   falls back to ``conn.execute(_INSERT_EVENT_WITH_DETAIL_SQL, ...)``
   so VAL-CROSS-020's 200 ms latency budget is met for non-API callers.

These tests exercise the routing decision without requiring a
testcontainers Postgres — a fake asyncpg pool with a recorder
captures the SQL the fallback path issues. The flusher path is
verified at integration level in
:mod:`tests.integration.test_triz_via_flusher` (where the round-trip
to a real bulk INSERT can be observed).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from whilly.adapters.db.repository import TaskRepository
from whilly.api.event_flusher import EventFlusher, EventRecord
from whilly.core.models import Priority, Task, TaskStatus
from whilly.core.triz import ERROR_REASON_TIMEOUT, TrizFinding, TrizOutcome


# ─── fixtures / helpers ──────────────────────────────────────────────────


def _make_task(task_id: str = "T-unit-triz") -> Task:
    """Build a :class:`Task` value object for the FAIL hook to operate on."""
    return Task(
        id=task_id,
        status=TaskStatus.FAILED,
        dependencies=(),
        key_files=(),
        priority=Priority.MEDIUM,
        description="Fake task for TRIZ unit tests.",
        acceptance_criteria=(),
        test_steps=(),
        prd_requirement="",
        version=2,
    )


class _FakeConnection:
    """Records SQL ``execute`` calls without touching a real Postgres."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))


class _FakePoolAcquireCM:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakePool:
    """Minimal asyncpg.Pool stand-in: ``acquire()`` returns a single fake connection."""

    def __init__(self) -> None:
        self.conn = _FakeConnection()

    def acquire(self) -> _FakePoolAcquireCM:
        return _FakePoolAcquireCM(self.conn)


def _patch_triz(monkeypatch: pytest.MonkeyPatch, outcome: TrizOutcome) -> None:
    """Monkeypatch :func:`analyze_contradiction_with_outcome` to return ``outcome``.

    The repository imports this lazily inside the FAIL hook; we patch
    on the source module so the late import resolves to our stub.
    """
    monkeypatch.setattr(
        "whilly.core.triz.analyze_contradiction_with_outcome",
        lambda task: outcome,
    )


# ─── tests ──────────────────────────────────────────────────────────────


def test_repository_falls_back_to_direct_insert_when_no_flusher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``event_flusher`` is None, the TRIZ contradiction event is written via direct INSERT.

    Constructs a :class:`TaskRepository` without the optional kwarg
    (matching the local-worker / CLI path) and triggers the FAIL hook
    on a synthetic positive-verdict outcome. The fake pool's
    connection records exactly one ``INSERT INTO events`` execute,
    proving the fallback path is preserved (VAL-CROSS-020 latency
    budget for non-API callers).
    """
    finding = TrizFinding(contradiction_type="technical", reason="cache fast vs consistent")
    _patch_triz(monkeypatch, TrizOutcome(finding=finding, error_reason=None))

    pool = _FakePool()
    repo = TaskRepository(pool)  # type: ignore[arg-type]  — fake pool by design
    asyncio.run(repo._maybe_emit_triz_event(_make_task()))

    inserts = [(sql, args) for sql, args in pool.conn.executed if "INSERT INTO events" in sql]
    assert len(inserts) == 1, f"expected exactly one direct INSERT in the fallback path; saw {len(inserts)}"
    sql, args = inserts[0]
    # The fallback writes the 4-column shape: task_id, event_type, payload, detail.
    assert args[0] == "T-unit-triz"
    assert args[1] == "triz.contradiction"


def test_repository_falls_back_for_timeout_when_no_flusher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout / triz.error branch also uses the direct-INSERT fallback when no flusher is bound."""
    _patch_triz(
        monkeypatch,
        TrizOutcome(finding=None, error_reason=ERROR_REASON_TIMEOUT),
    )

    pool = _FakePool()
    repo = TaskRepository(pool)  # type: ignore[arg-type]
    asyncio.run(repo._maybe_emit_triz_event(_make_task("T-unit-triz-timeout")))

    inserts = [(sql, args) for sql, args in pool.conn.executed if "INSERT INTO events" in sql]
    assert len(inserts) == 1
    _sql, args = inserts[0]
    assert args[1] == "triz.error"


def test_repository_routes_through_flusher_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``event_flusher`` is provided, the TRIZ event is enqueued — no direct INSERT.

    This is the inverse of
    :func:`test_repository_falls_back_to_direct_insert_when_no_flusher`:
    a recorder around ``EventFlusher.enqueue`` proves the lifespan
    path is taken, and the fake pool's connection records zero
    ``INSERT INTO events`` calls (proving the legacy direct-INSERT
    side effect is suppressed).
    """
    finding = TrizFinding(contradiction_type="physical", reason="must be both A and not-A simultaneously")
    _patch_triz(monkeypatch, TrizOutcome(finding=finding, error_reason=None))

    captured: list[EventRecord] = []

    fake_flusher = MagicMock(spec=EventFlusher)
    fake_flusher.enqueue.side_effect = lambda record: captured.append(record)

    pool = _FakePool()
    repo = TaskRepository(pool, event_flusher=fake_flusher)  # type: ignore[arg-type]
    asyncio.run(repo._maybe_emit_triz_event(_make_task("T-unit-flusher")))

    inserts = [(sql, args) for sql, args in pool.conn.executed if "INSERT INTO events" in sql]
    assert inserts == [], f"flusher path should suppress direct INSERT; saw {len(inserts)} legacy execute calls"
    assert len(captured) == 1, "expected exactly one EventRecord enqueued onto the flusher"
    record = captured[0]
    assert record.event_type == "triz.contradiction"
    assert record.task_id == "T-unit-flusher"
    assert record.payload == {}
    assert record.detail is not None
    assert record.detail["contradiction_type"] == "physical"


def test_attach_event_flusher_late_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach_event_flusher`` lets the lifespan wire the flusher onto a pre-built repo.

    The FastAPI composition root constructs the repo before lifespan
    entry (so the auth dependency can resolve per-worker tokens) but
    the flusher is allocated inside the lifespan to bind its queue
    to the running event loop. This setter is the bridge.
    """
    finding = TrizFinding(contradiction_type="technical", reason="late-bound flusher")
    _patch_triz(monkeypatch, TrizOutcome(finding=finding, error_reason=None))

    captured: list[EventRecord] = []
    fake_flusher = MagicMock(spec=EventFlusher)
    fake_flusher.enqueue.side_effect = lambda record: captured.append(record)

    pool = _FakePool()
    repo = TaskRepository(pool)  # type: ignore[arg-type] — no flusher yet
    repo.attach_event_flusher(fake_flusher)

    asyncio.run(repo._maybe_emit_triz_event(_make_task("T-late-bind")))

    assert len(captured) == 1
    assert captured[0].event_type == "triz.contradiction"
    # Detach again — subsequent FAIL hook reverts to direct INSERT.
    repo.attach_event_flusher(None)
    asyncio.run(repo._maybe_emit_triz_event(_make_task("T-late-bind-2")))
    inserts = [(sql, args) for sql, args in pool.conn.executed if "INSERT INTO events" in sql]
    assert len(inserts) == 1, (
        f"after detaching the flusher the FAIL hook should revert to direct INSERT; saw {len(inserts)}"
    )
