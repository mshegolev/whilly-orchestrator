"""Swarm navigation is available only for signed-in, swarm-enabled dashboards."""

from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from starlette.requests import Request

from whilly.api import dashboard


@pytest.mark.parametrize("signed_in,enabled", [(True, True), (True, False), (False, True)])
async def test_swarm_navigation(monkeypatch, signed_in, enabled):
    app = FastAPI()
    if enabled:
        router = APIRouter()
        router.add_api_route("/swarm", lambda: None, name="swarm_ui")
        app.include_router(router)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "app": app})
    monkeypatch.setattr(dashboard, "fetch_operator_snapshot", AsyncMock(side_effect=RuntimeError("offline")))
    response = await dashboard.render_dashboard(
        request=request, pool=None, auth_email="admin@example.com" if signed_in else None
    )
    html = response.body.decode()
    assert ('href="/swarm"' in html) == (signed_in and enabled)
