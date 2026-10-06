"""Product routes share admin authentication and cookie CSRF protection."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.api.product_swarm import build_product_router
from whilly.api.product_workflow import build_product_workflow_router

SECRET = b"product-test-secret-long-enough-0000"


@pytest.fixture
async def client(monkeypatch):
    principal = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(users_repo, "get_user_by_session_email", AsyncMock(return_value=principal))
    monkeypatch.setattr(
        sessions,
        "verify_session",
        AsyncMock(
            return_value=SimpleNamespace(
                email="admin@example.com", session_id="test", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
            )
        ),
    )
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware)
    app.include_router(build_product_router(None, SECRET, "/trusted/registry.json"))
    app.include_router(build_product_workflow_router(None, SECRET, "/trusted/registry.json"))
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test", email="admin@example.com", ttl_seconds=3600
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
        cookies={COOKIE_NAME: cookie},
    ) as http:
        yield http, principal


@pytest.mark.parametrize(
    "path", ["/swarm/product", "/api/v1/swarm/features", "/api/v1/swarm/products/default/messages"]
)
async def test_non_admin_cannot_read(client, path):
    http, principal = client
    principal.role = "operator"
    assert (await http.get(path)).status_code == 403


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/swarm/products/default/discuss", {"body": "hello"}),
        ("/api/v1/swarm/features/f1/plan", {}),
        ("/api/v1/swarm/features/f1/run", {"workers": 1}),
        ("/api/v1/swarm/features/f1/approve", {"revision": 1, "digest": "x"}),
        ("/api/v1/swarm/features/f1/publish", {}),
    ],
)
async def test_csrf_before_any_write(client, path, body):
    http, _ = client
    http.headers["Origin"] = "https://untrusted.example"
    assert (await http.post(path, json=body)).status_code == 403


async def test_admin_ui_and_no_worker_bearer(client):
    http, _ = client
    assert (await http.get("/swarm/product")).status_code == 200
    http.cookies.clear()
    http.headers["Authorization"] = "Bearer worker-token"
    assert (await http.get("/swarm/product")).status_code == 401


async def test_missing_planner_is_visible_and_rejected_before_jobs(client):
    http, _ = client
    page = await http.get("/swarm/product")
    assert "planner_setup_required" in page.text
    for path, body in [
        ("/api/v1/swarm/products/default/discuss", {"body": "hello"}),
        ("/api/v1/swarm/features/f1/plan", {}),
    ]:
        response = await http.post(path, json=body)
        assert response.status_code == 409
        assert response.json()["detail"] == "planner_setup_required"


async def test_only_configured_planners_enabled_in_ui(client, monkeypatch):
    from whilly.api import product_workflow

    monkeypatch.setattr(
        product_workflow, "load_registry", lambda _: SimpleNamespace(profiles={"planner-strong-codex": object()})
    )
    http, _ = client
    page = await http.get("/swarm/product")
    assert '<option value="planner-strong-codex" >' in page.text
    assert '<option value="planner-strong" disabled>' in page.text
    assert '<option value="">' in page.text
