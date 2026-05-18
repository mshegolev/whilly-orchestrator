"""Unit tests for the voluntary self-service password-change routes.

Implements the AC pin for PRD-post-auth-hardening §Epic D, Item 9 — the
:func:`whilly.api.auth_routes.build_auth_router` factory's ``GET/POST
/me/password`` endpoints. The four ACs from the plan JSON are:

1. Correct current password → 303 redirect to ``/`` with success flash.
2. Wrong current password → 422 with form error "Current password is
   incorrect".
3. ``new_password != confirm_new_password`` → 422 with appropriate error.
4. Unauthenticated request → 303 to ``/login``.

This file pins all four explicitly plus auxiliary paths (short new
password, gate-cache invalidation on success, GET form rendering).

No Postgres required — :func:`whilly.api.sessions.verify_session`,
:func:`whilly.api.users_repo.verify_credentials`, and
:func:`whilly.api.users_repo.set_password` are monkeypatched with
``AsyncMock``s. The pool argument flows through to these mocks and is
never dereferenced, so passing ``None`` is safe.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, must_change_gate, sessions, users_repo
from whilly.api.auth_routes import build_auth_router
from whilly.api.csrf import COOKIE_NAME

_TEST_SECRET: bytes = b"d9-test-secret-32-bytes-paddingx"
_TEST_SESSION_ID: str = "d9-session-id-abc123"
_TEST_USERNAME: str = "alice"
_TEST_EMAIL: str = f"{_TEST_USERNAME}@local"
_GOOD_CURRENT_PASSWORD: str = "current-good-password"
_NEW_PASSWORD_OK: str = "new-strong-pw-12+"  # 17 chars, satisfies _MIN_PASSWORD_LENGTH=12


def _make_session() -> Any:
    """Build a session-like object the route's _authenticate_session reads."""

    class _SessionStub:
        session_id = _TEST_SESSION_ID
        email = _TEST_EMAIL
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    return _SessionStub()


def _make_user() -> users_repo.User:
    return users_repo.User(
        username=_TEST_USERNAME,
        email=None,
        role="operator",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=False,
    )


def _mint_cookie() -> str:
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET,
        session_id=_TEST_SESSION_ID,
        email=_TEST_EMAIL,
        ttl_seconds=3600,
    )


@pytest.fixture
def patched_verify_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default: session exists and resolves to our test user."""
    mock = AsyncMock(return_value=_make_session())
    monkeypatch.setattr(sessions, "verify_session", mock)
    return mock


@pytest.fixture
def patched_verify_credentials_ok(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default: current_password is accepted, returning the user."""
    mock = AsyncMock(return_value=_make_user())
    monkeypatch.setattr(users_repo, "verify_credentials", mock)
    return mock


@pytest.fixture
def patched_set_password(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(users_repo, "set_password", mock)
    return mock


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A minimal app wired with build_auth_router + no CSRF (POST tests need to
    submit form data; CSRF is verified elsewhere — see test_auth_matrix.py).
    The pool is ``None`` because every DB call site is monkeypatched.
    """
    app = FastAPI()
    app.include_router(build_auth_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


# ─── GET form — auth required ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_me_password_unauthenticated_redirects_to_login(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session cookie at all → 303 to /login."""
    # verify_session won't be called because there's no cookie to verify, but
    # patch it anyway so an accidental call doesn't hit a real DB.
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=None))
    response = await client.get("/me/password", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_get_me_password_authenticated_renders_form(
    client: AsyncClient,
    patched_verify_session: AsyncMock,
) -> None:
    """Valid session cookie → 200 HTML with the form."""
    response = await client.get("/me/password", cookies={COOKIE_NAME: _mint_cookie()})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert 'name="current_password"' in body
    assert 'name="new_password"' in body
    assert 'name="confirm_new_password"' in body
    assert 'action="/me/password"' in body


# ─── AC4: unauthenticated POST redirects to /login ──────────────────────────


@pytest.mark.asyncio
async def test_post_me_password_unauthenticated_redirects_to_login(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST without a session cookie → 303 to /login. set_password must not
    be called (otherwise an attacker could change any password by guessing
    the username).
    """
    set_pw_mock = AsyncMock()
    monkeypatch.setattr(users_repo, "set_password", set_pw_mock)
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=None))
    response = await client.post(
        "/me/password",
        data={
            "current_password": _GOOD_CURRENT_PASSWORD,
            "new_password": _NEW_PASSWORD_OK,
            "confirm_new_password": _NEW_PASSWORD_OK,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert set_pw_mock.await_count == 0


# ─── AC2: wrong current password → 422 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_post_me_password_wrong_current_password_returns_422(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    patched_verify_session: AsyncMock,
    patched_set_password: AsyncMock,
) -> None:
    """verify_credentials returns None → 422 with the exact error message
    'Current password is incorrect.' (matches PRD AC text).
    """
    monkeypatch.setattr(users_repo, "verify_credentials", AsyncMock(return_value=None))
    response = await client.post(
        "/me/password",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "current_password": "WRONG",
            "new_password": _NEW_PASSWORD_OK,
            "confirm_new_password": _NEW_PASSWORD_OK,
        },
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Current password is incorrect" in response.text
    assert patched_set_password.await_count == 0


# ─── AC3: mismatched new passwords → 422 ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_me_password_mismatched_new_passwords_returns_422(
    client: AsyncClient,
    patched_verify_session: AsyncMock,
    patched_verify_credentials_ok: AsyncMock,
    patched_set_password: AsyncMock,
) -> None:
    """Mismatch between new_password and confirm_new_password → 422.

    Importantly, current_password is validated BEFORE the mismatch check,
    so we have to pass a good current_password through the verify mock.
    set_password must not be called.
    """
    response = await client.post(
        "/me/password",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "current_password": _GOOD_CURRENT_PASSWORD,
            "new_password": _NEW_PASSWORD_OK,
            "confirm_new_password": _NEW_PASSWORD_OK + "-different",
        },
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "do not match" in response.text
    assert patched_set_password.await_count == 0


# ─── Auxiliary: short new password → 422 ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_me_password_short_new_password_returns_422(
    client: AsyncClient,
    patched_verify_session: AsyncMock,
    patched_verify_credentials_ok: AsyncMock,
    patched_set_password: AsyncMock,
) -> None:
    """new_password shorter than _MIN_PASSWORD_LENGTH (12) → 422."""
    response = await client.post(
        "/me/password",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "current_password": _GOOD_CURRENT_PASSWORD,
            "new_password": "shortpw",
            "confirm_new_password": "shortpw",
        },
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "at least 12" in response.text
    assert patched_set_password.await_count == 0


# ─── AC1: success → 303 to / ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_me_password_success_redirects_to_root(
    client: AsyncClient,
    patched_verify_session: AsyncMock,
    patched_verify_credentials_ok: AsyncMock,
    patched_set_password: AsyncMock,
) -> None:
    """Happy path: correct current + matching new + long enough → 303 to /,
    set_password called exactly once with the new password.
    """
    response = await client.post(
        "/me/password",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "current_password": _GOOD_CURRENT_PASSWORD,
            "new_password": _NEW_PASSWORD_OK,
            "confirm_new_password": _NEW_PASSWORD_OK,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert patched_set_password.await_count == 1
    call_kwargs = patched_set_password.await_args.kwargs
    assert call_kwargs["username"] == _TEST_USERNAME
    assert call_kwargs["new_password"] == _NEW_PASSWORD_OK


# ─── must_change_gate cache invalidation on success ─────────────────────────


@pytest.mark.asyncio
async def test_post_me_password_success_invalidates_gate_cache(
    client: AsyncClient,
    patched_verify_session: AsyncMock,
    patched_verify_credentials_ok: AsyncMock,
    patched_set_password: AsyncMock,
) -> None:
    """A successful voluntary change must drop the gate's cached
    must_change verdict so a returning user who pre-emptively cleared
    must_change_password isn't bounced back to /auth/change-password.
    """
    # Pre-seed the cache for this session_id so we can assert it was popped.
    must_change_gate._cache[_TEST_SESSION_ID] = (True, 9_999_999.0)
    assert _TEST_SESSION_ID in must_change_gate._cache

    response = await client.post(
        "/me/password",
        cookies={COOKIE_NAME: _mint_cookie()},
        data={
            "current_password": _GOOD_CURRENT_PASSWORD,
            "new_password": _NEW_PASSWORD_OK,
            "confirm_new_password": _NEW_PASSWORD_OK,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _TEST_SESSION_ID not in must_change_gate._cache
