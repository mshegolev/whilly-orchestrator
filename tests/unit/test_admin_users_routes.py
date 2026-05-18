"""Unit tests for :mod:`whilly.api.admin_users_routes`.

PRD-post-auth-hardening §Epic D, Item 10. Verifies the 403/200 role
gate and the CRUD surface contract (create / set_role / reset_password /
delete) plus the auth-audit browse pagination.

asyncpg is faked end-to-end — sessions.verify_session and the entire
users_repo + auth_audit_repo surface are monkeypatched with
:class:`AsyncMock`s. The pool argument is ``None`` because no DB call
site is reached; tests assert on mock call args.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_audit_repo, auth_tokens, sessions, users_repo
from whilly.api.admin_users_routes import build_admin_users_router
from whilly.api.csrf import COOKIE_NAME

_TEST_SECRET: bytes = b"d10-test-secret-32-bytes-paddxxx"
_TEST_SESSION_ID: str = "d10-session-id"
_ADMIN_USERNAME: str = "alice"
_ADMIN_EMAIL: str = f"{_ADMIN_USERNAME}@local"
_OPERATOR_USERNAME: str = "bob"
_OPERATOR_EMAIL: str = f"{_OPERATOR_USERNAME}@local"


def _session(email: str) -> Any:
    class _S:
        session_id = _TEST_SESSION_ID
        email_ = email

        def __init__(self) -> None:
            self.email = email

        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    return _S()


def _user(username: str, role: str) -> users_repo.User:
    return users_repo.User(
        username=username,
        email=None,
        role=role,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=False,
    )


def _mint_cookie(email: str = _ADMIN_EMAIL) -> str:
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET,
        session_id=_TEST_SESSION_ID,
        email=email,
        ttl_seconds=3600,
    )


@pytest.fixture
def patch_admin(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Wire mocks so the admin guard sees an admin user."""
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session(_ADMIN_EMAIL)))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user(_ADMIN_USERNAME, "admin")))
    return {
        "list_users": AsyncMock(return_value=[_user(_ADMIN_USERNAME, "admin"), _user(_OPERATOR_USERNAME, "operator")]),
        "create_user": AsyncMock(return_value=None),
        "set_role": AsyncMock(return_value=None),
        "delete_user": AsyncMock(return_value=True),
        "reset_password_to_random": AsyncMock(return_value="rand-pw-abc123"),
        "list_attempts": AsyncMock(return_value=[]),
    }


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, patch_admin: dict) -> AsyncIterator[AsyncClient]:
    # Patch users_repo CRUD functions
    monkeypatch.setattr(users_repo, "list_users", patch_admin["list_users"])
    monkeypatch.setattr(users_repo, "create_user", patch_admin["create_user"])
    monkeypatch.setattr(users_repo, "set_role", patch_admin["set_role"])
    monkeypatch.setattr(users_repo, "delete_user", patch_admin["delete_user"])
    monkeypatch.setattr(users_repo, "reset_password_to_random", patch_admin["reset_password_to_random"])
    monkeypatch.setattr(auth_audit_repo, "list_attempts", patch_admin["list_attempts"])
    # Patch the binding inside admin_users_routes (since it does `from whilly.api import users_repo`,
    # accessing users_repo.X is attribute lookup on the module, so the above patches DO reach it)
    app = FastAPI()
    app.state.pool = object()  # sentinel — never dereferenced because mocks intercept
    app.include_router(build_admin_users_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


# ─── 403 on non-admin ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_users_returns_403_for_operator_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authenticated session with role!=admin → 403 on any /admin/* route."""
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session(_OPERATOR_EMAIL)))
    monkeypatch.setattr(
        users_repo, "get_user_by_username", AsyncMock(return_value=_user(_OPERATOR_USERNAME, "operator"))
    )
    app = FastAPI()
    app.state.pool = object()
    app.include_router(build_admin_users_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        resp = await ac.get("/admin/users", cookies={COOKIE_NAME: _mint_cookie(_OPERATOR_EMAIL)})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_users_returns_401_without_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """No session at all → 401 (from _authenticate_session)."""
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=None))
    app = FastAPI()
    app.state.pool = object()
    app.include_router(build_admin_users_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        resp = await ac.get("/admin/users")
    assert resp.status_code == 401


# ─── 200 admin paths ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_list_users(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.get("/admin/users", cookies={COOKIE_NAME: _mint_cookie()})
    assert resp.status_code == 200
    assert _ADMIN_USERNAME in resp.text
    assert _OPERATOR_USERNAME in resp.text
    assert patch_admin["list_users"].await_count >= 1


@pytest.mark.asyncio
async def test_admin_can_create_user(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.post(
        "/admin/users/create",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "username": "newguy",
            "email": "new@example.com",
            "role": "operator",
            "initial_password": "tempPW-12chars",
        },
    )
    assert resp.status_code == 200
    assert patch_admin["create_user"].await_count == 1
    kwargs = patch_admin["create_user"].await_args.kwargs
    assert kwargs["username"] == "newguy"
    assert kwargs["role"] == "operator"


@pytest.mark.asyncio
async def test_admin_create_with_invalid_role_returns_400(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.post(
        "/admin/users/create",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "username": "x",
            "email": "",
            "role": "superuser",  # invalid
            "initial_password": "pw",
        },
    )
    assert resp.status_code == 400
    assert "invalid role" in resp.text
    assert patch_admin["create_user"].await_count == 0


@pytest.mark.asyncio
async def test_admin_can_set_role(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.post(
        f"/admin/users/{_OPERATOR_USERNAME}/role",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={"role": "admin"},
    )
    assert resp.status_code == 200
    assert patch_admin["set_role"].await_count == 1


@pytest.mark.asyncio
async def test_admin_can_reset_password_and_flash_shows_new_value(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.post(
        f"/admin/users/{_OPERATOR_USERNAME}/reset-password",
        cookies={COOKIE_NAME: _mint_cookie()},
    )
    assert resp.status_code == 200
    # The flash message exposes the random password ONCE to the admin.
    assert "rand-pw-abc123" in resp.text
    assert patch_admin["reset_password_to_random"].await_count == 1


@pytest.mark.asyncio
async def test_admin_can_delete_user(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.post(
        f"/admin/users/{_OPERATOR_USERNAME}/delete",
        cookies={COOKIE_NAME: _mint_cookie()},
    )
    assert resp.status_code == 200
    assert patch_admin["delete_user"].await_count == 1


@pytest.mark.asyncio
async def test_admin_cannot_delete_self(client: AsyncClient, patch_admin: dict) -> None:
    """Self-delete is blocked at 400 — no admin can brick the last admin row."""
    resp = await client.post(
        f"/admin/users/{_ADMIN_USERNAME}/delete",
        cookies={COOKIE_NAME: _mint_cookie()},
    )
    assert resp.status_code == 400
    assert "currently signed-in" in resp.text
    assert patch_admin["delete_user"].await_count == 0


@pytest.mark.asyncio
async def test_admin_delete_second_attempt_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, patch_admin: dict
) -> None:
    """AC: idempotent — second delete (row gone) → 404 page."""
    patch_admin["delete_user"].return_value = False
    monkeypatch.setattr(users_repo, "delete_user", patch_admin["delete_user"])
    resp = await client.post(
        f"/admin/users/{_OPERATOR_USERNAME}/delete",
        cookies={COOKIE_NAME: _mint_cookie()},
    )
    assert resp.status_code == 404
    assert "not found" in resp.text


# ─── auth-audit browse ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_auth_audit_renders_empty(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.get("/admin/auth-audit", cookies={COOKIE_NAME: _mint_cookie()})
    assert resp.status_code == 200
    assert "no audit rows" in resp.text


@pytest.mark.asyncio
async def test_admin_auth_audit_renders_with_filter(client: AsyncClient, patch_admin: dict) -> None:
    patch_admin["list_attempts"].return_value = [
        {
            "id": 1,
            "ts": datetime.datetime(2026, 5, 18, 12, 0, 0, tzinfo=datetime.timezone.utc),
            "username": "alice",
            "ip": "1.2.3.4",
            "user_agent": "test-agent",
            "outcome": "ok",
            "session_id": None,
        }
    ]
    resp = await client.get("/admin/auth-audit?username=alice", cookies={COOKIE_NAME: _mint_cookie()})
    assert resp.status_code == 200
    assert "1.2.3.4" in resp.text
    kwargs = patch_admin["list_attempts"].await_args.kwargs
    assert kwargs["username_filter"] == "alice"
    assert kwargs["offset"] == 0  # page=1 → offset=0


@pytest.mark.asyncio
async def test_admin_auth_audit_pagination_passes_offset(client: AsyncClient, patch_admin: dict) -> None:
    resp = await client.get("/admin/auth-audit?page=3", cookies={COOKIE_NAME: _mint_cookie()})
    assert resp.status_code == 200
    kwargs = patch_admin["list_attempts"].await_args.kwargs
    assert kwargs["offset"] == 100  # page=3 with page_size=50 → offset=100
    assert kwargs["limit"] == 50
