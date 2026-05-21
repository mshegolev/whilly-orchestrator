"""Unit tests for the /auth/change-password must-change gate (Finding 6).

The forced-flow ``POST /auth/change-password`` sets a new password WITHOUT the
current one. That is only acceptable while the account is genuinely in the
must-change state; for any other session it must route to ``/me/password``
(which requires the current password) so a hijacked/idle session cannot rotate
the password. No Postgres — the DB call sites are monkeypatched (pool=None).
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api.auth_routes import build_auth_router
from whilly.api.csrf import COOKIE_NAME

_SECRET: bytes = b"f6-test-secret-32-bytes-paddingx"
_SESSION_ID: str = "f6-session-id-abc123"
_USERNAME: str = "alice"
_EMAIL: str = f"{_USERNAME}@local"
_NEW_PW: str = "new-strong-pw-12+"  # 17 chars, satisfies _MIN_PASSWORD_LENGTH


def _session() -> Any:
    class _S:
        session_id = _SESSION_ID
        email = _EMAIL
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    return _S()


def _user(*, must_change: bool) -> users_repo.User:
    return users_repo.User(
        username=_USERNAME,
        email=None,
        role="operator",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=must_change,
    )


def _cookie() -> str:
    return auth_tokens.mint_session_cookie_value(_SECRET, session_id=_SESSION_ID, email=_EMAIL, ttl_seconds=3600)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(build_auth_router(pool=None, secret=_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


async def _post(client: AsyncClient, *, with_cookie: bool) -> Any:
    cookies = {COOKIE_NAME: _cookie()} if with_cookie else {}
    return await client.post(
        "/auth/change-password",
        cookies=cookies,
        data={"new_password": _NEW_PW, "confirm_new_password": _NEW_PW},
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_proceeds_when_must_change_password_set(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user(must_change=True)))
    set_pw = AsyncMock(return_value=None)
    monkeypatch.setattr(users_repo, "set_password", set_pw)
    resp = await _post(client, with_cookie=True)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    set_pw.assert_awaited_once()


@pytest.mark.asyncio
async def test_redirects_to_me_password_when_not_must_change(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user(must_change=False)))
    set_pw = AsyncMock(return_value=None)
    monkeypatch.setattr(users_repo, "set_password", set_pw)
    resp = await _post(client, with_cookie=True)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/me/password"
    # The password was NOT rotated without the current one.
    assert set_pw.await_count == 0


@pytest.mark.asyncio
async def test_redirects_when_user_row_missing(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=None))
    set_pw = AsyncMock(return_value=None)
    monkeypatch.setattr(users_repo, "set_password", set_pw)
    resp = await _post(client, with_cookie=True)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/me/password"
    assert set_pw.await_count == 0


@pytest.mark.asyncio
async def test_unauthenticated_redirects_to_login(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=None))
    set_pw = AsyncMock(return_value=None)
    monkeypatch.setattr(users_repo, "set_password", set_pw)
    resp = await _post(client, with_cookie=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert set_pw.await_count == 0
