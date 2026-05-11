from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.transport.server import create_app

pytestmark = DOCKER_REQUIRED


@pytest.fixture
async def app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=None,
        claim_long_poll_timeout=0.1,
        claim_poll_interval=0.05,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_admin_can_pause_resume_and_read_control_state(
    client: AsyncClient,
    task_repo: TaskRepository,
) -> None:
    token = "admin-control-state"
    await task_repo.mint_bootstrap_token(token, owner_email="lead@example.com", is_admin=True)
    headers = {"Authorization": f"Bearer {token}"}

    initial = await client.get("/api/v1/admin/workers/control-state", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["paused"] is False

    paused = await client.post(
        "/api/v1/admin/workers/pause",
        json={"reason": "release gate"},
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    paused_body = paused.json()
    assert paused_body["paused"] is True
    assert paused_body["pause_reason"] == "release gate"
    assert paused_body["paused_by"] == "lead@example.com"

    resumed = await client.post("/api/v1/admin/workers/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    resumed_body = resumed.json()
    assert resumed_body["paused"] is False
    assert resumed_body["pause_reason"] is None
    assert resumed_body["paused_by"] is None


async def test_non_admin_cannot_mutate_control_state(
    client: AsyncClient,
    task_repo: TaskRepository,
) -> None:
    token = "operator-control-state"
    await task_repo.mint_bootstrap_token(token, owner_email="operator@example.com", is_admin=False)

    response = await client.post(
        "/api/v1/admin/workers/pause",
        json={"reason": "not allowed"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


async def test_worker_bearer_can_read_control_state(
    client: AsyncClient,
    task_repo: TaskRepository,
) -> None:
    admin_token = "admin-control-state-worker-read"
    await task_repo.mint_bootstrap_token(admin_token, owner_email="lead@example.com", is_admin=True)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    registered = await client.post(
        "/workers/register",
        json={"hostname": "worker-control-state-reader"},
        headers=admin_headers,
    )
    assert registered.status_code == 201, registered.text
    worker_token = registered.json()["token"]

    await client.post(
        "/api/v1/admin/workers/pause",
        json={"reason": "operator gate"},
        headers=admin_headers,
    )

    response = await client.get(
        "/workers/control-state",
        headers={"Authorization": f"Bearer {worker_token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["paused"] is True
    assert response.json()["pause_reason"] == "operator gate"
