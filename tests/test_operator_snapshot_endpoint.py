"""Tests for GET /api/v1/operator/snapshot endpoint.

Uses a minimal fake pool so no real Postgres or Docker is required.
The pool stubs asyncpg's acquire / fetch / fetchrow / fetchval surface
so the auth gate and snapshot builder both get empty-but-valid results.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from whilly.adapters.transport.server import create_app
from whilly.operator_snapshot_codec import snapshot_from_dict


class _FakeConn:
    """Minimal asyncpg Connection stub — all queries return empty results."""

    async def fetch(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return []

    async def fetchrow(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def fetchval(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class _FakePool:
    """Minimal asyncpg Pool stub whose acquire() yields a _FakeConn."""

    @asynccontextmanager
    async def acquire(self):  # type: ignore[override]
        yield _FakeConn()


@pytest.fixture
def fake_pool() -> _FakePool:
    return _FakePool()


def _client(pool: _FakePool) -> AsyncClient:
    app = create_app(pool, worker_token="legacy-worker", bootstrap_token="legacy-boot")  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_snapshot_requires_bearer(fake_pool: _FakePool) -> None:
    async with _client(fake_pool) as c:
        resp = await c.get("/api/v1/operator/snapshot")
    assert resp.status_code == 401


async def test_snapshot_rejects_bad_bearer(fake_pool: _FakePool) -> None:
    async with _client(fake_pool) as c:
        resp = await c.get(
            "/api/v1/operator/snapshot",
            headers={"Authorization": "Bearer nope"},
        )
    assert resp.status_code == 403


async def test_snapshot_returns_payload_with_legacy_token(fake_pool: _FakePool) -> None:
    async with _client(fake_pool) as c:
        resp = await c.get(
            "/api/v1/operator/snapshot",
            headers={"Authorization": "Bearer legacy-worker"},
        )
    assert resp.status_code == 200
    snap = snapshot_from_dict(resp.json())
    assert snap.summary.total_tasks >= 0
