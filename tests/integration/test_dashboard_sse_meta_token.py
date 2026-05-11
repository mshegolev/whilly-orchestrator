"""Dashboard short-lived token integration tests.

Pins the VAL-CROSS-DEMO-014 fix: the anonymous dashboard page must be
able to open its live SSE channel without exposing a long-lived worker
bearer token in the browser.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-dashboard-token-test"


@pytest.fixture
async def app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
        sse_ping_seconds=1,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _extract_meta_token(html: str) -> str:
    match = re.search(r'<meta name="whilly-events-token" content="([^"]+)">', html)
    assert match, f"dashboard token meta tag missing from HTML:\n{html[:500]}"
    return match.group(1)


def _decode_payload(token: str) -> dict[str, object]:
    parts = token.split(".")
    assert len(parts) == 3
    raw = parts[1]
    raw += "=" * (-len(raw) % 4)
    payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    assert isinstance(payload, dict)
    return payload


async def test_dashboard_embeds_short_lived_events_token(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    token = _extract_meta_token(response.text)

    payload = _decode_payload(token)
    assert payload["iss"] == "whilly"
    assert "events.stream" in payload["scope"]
    assert "tasks.read" in payload["scope"]
    assert int(payload["exp"]) - int(payload["iat"]) <= 3600
    assert f"/events/stream?token={token}" in response.text


async def test_events_stream_accepts_dashboard_token_query(client: AsyncClient, app: FastAPI) -> None:
    token = _extract_meta_token((await client.get("/")).text)
    app.state.event_notify_broker = None

    response = await client.get(f"/events/stream?token={token}")
    assert response.status_code == 503
    assert response.json()["detail"] == "event broker not initialised"


async def test_events_stream_accepts_dashboard_token_bearer(client: AsyncClient, app: FastAPI) -> None:
    token = _extract_meta_token((await client.get("/")).text)
    app.state.event_notify_broker = None

    response = await client.get("/events/stream", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json()["detail"] == "event broker not initialised"


async def test_tasks_api_accepts_dashboard_read_token(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    token = _extract_meta_token((await client.get("/")).text)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ('dashboard-token-plan', 'Dashboard token plan')")

    response = await client.get(f"/api/v1/tasks?plan_id=dashboard-token-plan&token={token}")
    assert response.status_code == 200
    assert response.json()["tasks"] == []
