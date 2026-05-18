"""Unit tests for the active-sessions UI (PRD §Epic E Item 16)."""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions
from whilly.api.auth_routes import build_auth_router
from whilly.api.csrf import COOKIE_NAME

_TEST_SECRET: bytes = b"e16-test-secret-32-bytes-paddxxx"
_TEST_SESSION_ID: str = "e16-session-id-current"
_OTHER_SESSION_ID: str = "e16-session-id-other"
_FOREIGN_SESSION_ID: str = "e16-session-id-foreign"
_EMAIL: str = "alice@local"
_FOREIGN_EMAIL: str = "bob@local"


def _session(session_id: str = _TEST_SESSION_ID, email: str = _EMAIL) -> Any:
    class _S:
        def __init__(self) -> None:
            self.session_id = session_id
            self.email = email
            self.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
            self.last_seen_at = datetime.datetime.now(datetime.timezone.utc)
            self.expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
            self.revoked_at = None

    return _S()


def _mint(session_id: str = _TEST_SESSION_ID, email: str = _EMAIL) -> str:
    return auth_tokens.mint_session_cookie_value(_TEST_SECRET, session_id=session_id, email=email, ttl_seconds=3600)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    # By default, verify_session returns the principal session
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(
        sessions,
        "list_active_sessions_for_email",
        AsyncMock(return_value=[_session(), _session(_OTHER_SESSION_ID)]),
    )
    monkeypatch.setattr(sessions, "revoke_session", AsyncMock(return_value=True))
    app = FastAPI()
    app.include_router(build_auth_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


@pytest.mark.asyncio
async def test_get_me_sessions_unauthenticated_redirects_to_login(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=None))
    resp = await client.get("/me/sessions", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_get_me_sessions_lists_user_sessions(client: AsyncClient) -> None:
    resp = await client.get("/me/sessions", cookies={COOKIE_NAME: _mint()})
    assert resp.status_code == 200
    assert _TEST_SESSION_ID[:12] in resp.text
    assert _OTHER_SESSION_ID[:12] in resp.text
    assert "this device" in resp.text  # current-session tag


@pytest.mark.asyncio
async def test_revoke_other_session_keeps_user_signed_in(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoking a non-current session re-renders the page with a flash;
    the user is NOT logged out.
    """
    # verify_session is called TWICE: once for principal auth, once for target lookup.
    # Make both return successfully (principal current, target = the other session).
    seq = [_session(_TEST_SESSION_ID), _session(_OTHER_SESSION_ID)]
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(side_effect=seq))
    monkeypatch.setattr(
        sessions, "list_active_sessions_for_email", AsyncMock(return_value=[_session(_TEST_SESSION_ID)])
    )
    resp = await client.post(
        f"/me/sessions/{_OTHER_SESSION_ID}/revoke",
        cookies={COOKIE_NAME: _mint()},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "revoked" in resp.text


@pytest.mark.asyncio
async def test_revoke_current_session_clears_cookie_and_redirects_to_login(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revoking the CURRENT session → 303 to /login + cookie deletion."""
    # principal auth + target lookup both return the same session_id.
    seq = [_session(_TEST_SESSION_ID), _session(_TEST_SESSION_ID)]
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(side_effect=seq))
    resp = await client.post(
        f"/me/sessions/{_TEST_SESSION_ID}/revoke",
        cookies={COOKIE_NAME: _mint()},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # Cookie deletion: Set-Cookie with Max-Age=0 or expires=epoch.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "whilly_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=Thu, 01 Jan 1970" in set_cookie


@pytest.mark.asyncio
async def test_revoke_foreign_session_returns_404(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticated as alice, attempting to revoke bob's session → 404.

    Defends against session-id-guessing privilege escalation.
    """
    seq = [_session(_TEST_SESSION_ID, _EMAIL), _session(_FOREIGN_SESSION_ID, _FOREIGN_EMAIL)]
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(side_effect=seq))
    revoke_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(sessions, "revoke_session", revoke_mock)
    resp = await client.post(
        f"/me/sessions/{_FOREIGN_SESSION_ID}/revoke",
        cookies={COOKIE_NAME: _mint()},
    )
    assert resp.status_code == 404
    # Critical: revoke_session must NOT have been called.
    assert revoke_mock.await_count == 0


@pytest.mark.asyncio
async def test_revoke_nonexistent_session_returns_404(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Target session_id doesn't exist (verify_session returns None) → 404."""
    seq = [_session(_TEST_SESSION_ID), None]
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(side_effect=seq))
    resp = await client.post(
        "/me/sessions/nope-not-a-real-session/revoke",
        cookies={COOKIE_NAME: _mint()},
    )
    assert resp.status_code == 404
