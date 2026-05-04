"""Integration tests for the M3-extended /health endpoints (m3-prometheus-metrics).

Covers:
* GET /health returns 200 with extended JSON body (db_reachable,
  listener_connected, queue_depth, status) — VAL-M3-HEALTH-901
* /health remains unauthenticated (no bearer required)
* /health/live returns 200 unconditionally (k8s liveness)
* /health/ready returns 200 only when DB reachable AND listener task alive
* /health body keeps backwards-compatible ``status: ok`` field
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app
from whilly.api.metrics import METRICS_TOKEN_ENV

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-health-test"


@pytest.fixture
async def app(db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    monkeypatch.setenv(METRICS_TOKEN_ENV, "health-test-token")
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
        sse_ping_seconds=1,
        metrics_token="health-test-token",
        metrics_refresh_interval_seconds=0.5,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_returns_200_no_auth(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200


async def test_health_body_includes_extended_keys(client: AsyncClient) -> None:
    response = await client.get("/health")
    body = response.json()
    assert body["status"] == "ok"
    assert body["db_reachable"] is True
    assert "listener_connected" in body
    assert isinstance(body["queue_depth"], int)


async def test_health_live_unconditional_200(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_ready_returns_200_when_healthy(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db_reachable"] is True


async def test_health_route_is_distinct_from_live_and_ready(app: FastAPI) -> None:
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/health" in paths
    assert "/health/live" in paths
    assert "/health/ready" in paths


async def test_health_does_not_require_auth(client: AsyncClient) -> None:
    response_no_auth = await client.get("/health")
    response_bad_auth = await client.get("/health", headers={"Authorization": "Bearer wrong"})
    assert response_no_auth.status_code == response_bad_auth.status_code == 200
