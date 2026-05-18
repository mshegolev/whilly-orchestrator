"""Unit tests for :mod:`whilly.api.tasks_api_crud` reset endpoints.

PRD-post-auth-hardening §Epic B, Item 3. Pins the contract of
``GET /api/v1/tasks/{id}/reset-preview`` and ``POST /api/v1/tasks/{id}/reset``
without a real Postgres pool. The asyncpg ``Pool``/``Connection`` surface
is faked with ``AsyncMock`` so all error-path conditions can be triggered
deterministically.

Coverage targets all six branches from the PRD AC:
1. 200 happy path — failed task resets to pending
2. 404 unknown task (reset-preview)
3. 409 task currently in_progress claimed by a worker (reset-preview)
4. 400 wrong status (reset-preview rejects e.g. PENDING/RUNNING)
5. Cascade list shape (downstream blocked tasks included in preview)
6. No-mapping cascade (reset_task happy path with downstream)

Plus reset-task-specific paths: 409 when UPDATE did not include the
target (already claimed between preview and confirm), 400 on invalid
cascade_ids body shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions
from whilly.api.csrf import COOKIE_NAME
from whilly.api.tasks_api_crud import build_tasks_crud_router

_TEST_SECRET: bytes = b"b3-test-secret-32-bytes-padddingx"
_TEST_SESSION_ID: str = "b3-session-id"
_TEST_EMAIL: str = "operator@local"
_PLAN_ID: str = "demo-plan"
_TASK_ID: str = "TASK-1"


def _make_session() -> Any:
    class _S:
        session_id = _TEST_SESSION_ID
        email = _TEST_EMAIL
        import datetime as _dt

        expires_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)

    return _S()


def _mint_cookie() -> str:
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET,
        session_id=_TEST_SESSION_ID,
        email=_TEST_EMAIL,
        ttl_seconds=3600,
    )


class _FakeConn:
    """asyncpg-like connection. ``fetchrow``/``fetch`` are configurable per test."""

    def __init__(
        self,
        *,
        fetchrow_result: Any = None,
        fetch_result: list[Any] | None = None,
    ) -> None:
        self.fetchrow = AsyncMock(return_value=fetchrow_result)
        self.fetch = AsyncMock(return_value=fetch_result or [])
        self.execute = AsyncMock(return_value=None)

    def transaction(self) -> Any:
        # asyncpg's conn.transaction() returns a context manager.
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm


class _FakePool:
    """asyncpg-like pool whose ``acquire()`` yields the same ``_FakeConn``."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=self._conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticate every request by default — tests can re-patch if they want 401."""
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_make_session()))


def _build_client(conn: _FakeConn) -> AsyncClient:
    pool = _FakePool(conn)
    app = FastAPI()
    app.include_router(build_tasks_crud_router(pool=pool, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")


@pytest.fixture
async def conn_factory() -> AsyncIterator[Any]:
    """Yield a builder so each test can configure its conn."""

    def _build(**kwargs: Any) -> _FakeConn:
        return _FakeConn(**kwargs)

    yield _build


# ─── reset-preview: 200 happy path with downstream cascade ──────────────────


@pytest.mark.asyncio
async def test_reset_preview_returns_target_and_downstream(
    conn_factory: Any,
) -> None:
    """AC1+5: failed task → 200 with cascade list of downstream blocked tasks."""
    target = {"id": _TASK_ID, "status": "FAILED", "version": 3, "claimed_by": None}
    downstream = [
        {"id": "DOWN-1", "status": "FAILED", "version": 2},
        {"id": "DOWN-2", "status": "SKIPPED", "version": 1},
    ]
    conn = conn_factory(fetchrow_result=target, fetch_result=downstream)
    async with _build_client(conn) as ac:
        resp = await ac.get(
            f"/api/v1/tasks/{_TASK_ID}/reset-preview",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == _TASK_ID
    assert body["status"] == "FAILED"
    assert body["version"] == 3
    assert body["downstream_blocked"] == [
        {"id": "DOWN-1", "status": "FAILED", "version": 2},
        {"id": "DOWN-2", "status": "SKIPPED", "version": 1},
    ]


# ─── reset-preview: 404 unknown task ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_preview_unknown_task_returns_404(conn_factory: Any) -> None:
    """AC2: fetchrow returns None → 404."""
    conn = conn_factory(fetchrow_result=None)
    async with _build_client(conn) as ac:
        resp = await ac.get(
            f"/api/v1/tasks/{_TASK_ID}/reset-preview",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
        )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ─── reset-preview: 409 task is currently claimed ───────────────────────────


@pytest.mark.asyncio
async def test_reset_preview_claimed_task_returns_409(conn_factory: Any) -> None:
    """AC3: task with claimed_by populated → 409."""
    target = {"id": _TASK_ID, "status": "IN_PROGRESS", "version": 5, "claimed_by": "worker-7"}
    conn = conn_factory(fetchrow_result=target)
    async with _build_client(conn) as ac:
        resp = await ac.get(
            f"/api/v1/tasks/{_TASK_ID}/reset-preview",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
        )
    assert resp.status_code == 409
    assert "worker-7" in resp.json()["detail"]


# ─── reset-preview: 400 wrong status (already PENDING) ──────────────────────


@pytest.mark.asyncio
async def test_reset_preview_already_pending_returns_400(conn_factory: Any) -> None:
    """AC4: PENDING/RUNNING etc. are not resettable; only FAILED/SKIPPED/DONE."""
    target = {"id": _TASK_ID, "status": "PENDING", "version": 0, "claimed_by": None}
    conn = conn_factory(fetchrow_result=target)
    async with _build_client(conn) as ac:
        resp = await ac.get(
            f"/api/v1/tasks/{_TASK_ID}/reset-preview",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
        )
    assert resp.status_code == 400
    assert "FAILED/SKIPPED/DONE" in resp.json()["detail"]


# ─── reset-preview: 200 with empty cascade (no downstream) ──────────────────


@pytest.mark.asyncio
async def test_reset_preview_no_mapping_returns_empty_cascade(
    conn_factory: Any,
) -> None:
    """AC6 variant: target exists, no downstream → empty cascade list."""
    target = {"id": _TASK_ID, "status": "DONE", "version": 1, "claimed_by": None}
    conn = conn_factory(fetchrow_result=target, fetch_result=[])
    async with _build_client(conn) as ac:
        resp = await ac.get(
            f"/api/v1/tasks/{_TASK_ID}/reset-preview",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
        )
    assert resp.status_code == 200
    assert resp.json()["downstream_blocked"] == []


# ─── reset-task: 200 happy path with cascade ────────────────────────────────


@pytest.mark.asyncio
async def test_reset_task_happy_path_returns_reset_ids(conn_factory: Any) -> None:
    """POST /reset with cascade_ids → 200 with the reset_ids list (target + cascade)."""
    updated_rows = [
        {"id": _TASK_ID, "status": "PENDING", "version": 4},
        {"id": "DOWN-1", "status": "PENDING", "version": 3},
    ]
    conn = conn_factory(fetch_result=updated_rows)
    async with _build_client(conn) as ac:
        resp = await ac.post(
            f"/api/v1/tasks/{_TASK_ID}/reset",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
            json={"cascade_ids": ["DOWN-1"]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert _TASK_ID in body["reset_ids"]
    assert "DOWN-1" in body["reset_ids"]


# ─── reset-task: 409 when target not in UPDATE RETURNING ────────────────────


@pytest.mark.asyncio
async def test_reset_task_target_claimed_returns_409(conn_factory: Any) -> None:
    """If UPDATE skipped the target (claimed_by != NULL or wrong status now), → 409."""
    # Only a downstream row was updated; the target was filtered out by the WHERE.
    conn = conn_factory(fetch_result=[{"id": "DOWN-1", "status": "PENDING", "version": 2}])
    async with _build_client(conn) as ac:
        resp = await ac.post(
            f"/api/v1/tasks/{_TASK_ID}/reset",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
            json={"cascade_ids": ["DOWN-1"]},
        )
    assert resp.status_code == 409
    assert "not reset" in resp.json()["detail"]


# ─── reset-task: 400 on invalid cascade_ids body shape ──────────────────────


@pytest.mark.asyncio
async def test_reset_task_invalid_cascade_ids_returns_400(conn_factory: Any) -> None:
    conn = conn_factory(fetch_result=[])
    async with _build_client(conn) as ac:
        resp = await ac.post(
            f"/api/v1/tasks/{_TASK_ID}/reset",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
            json={"cascade_ids": "not-a-list"},
        )
    assert resp.status_code == 400
    assert "list of strings" in resp.json()["detail"]


# ─── reset-task: empty body is tolerated (no cascade) ───────────────────────


@pytest.mark.asyncio
async def test_reset_task_empty_body_just_resets_target(conn_factory: Any) -> None:
    """Hitting POST with no body should be interpreted as cascade_ids=[]."""
    conn = conn_factory(fetch_result=[{"id": _TASK_ID, "status": "PENDING", "version": 4}])
    async with _build_client(conn) as ac:
        resp = await ac.post(
            f"/api/v1/tasks/{_TASK_ID}/reset",
            params={"plan_id": _PLAN_ID},
            cookies={COOKIE_NAME: _mint_cookie()},
        )
    assert resp.status_code == 200
    assert resp.json() == {"reset_ids": [_TASK_ID]}
