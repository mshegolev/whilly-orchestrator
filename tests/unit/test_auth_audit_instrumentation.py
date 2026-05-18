"""Unit tests pinning the auth_audit instrumentation in submit_login.

PRD-post-auth-hardening §Epic D, Item 11 (call-site instrumentation,
D10b). Verifies that every code path through ``POST /auth/login``
calls :func:`whilly.api.auth_audit_repo.insert_attempt` with the
appropriate outcome.

Coverage:
* Successful login → outcome='ok' with session_id populated.
* Bad credentials (verify_credentials returns None) → 'bad_password'.
* Rate-limit hit → 'rate_limited', no DB session created.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_audit_repo, rate_limit, sessions, users_repo
from whilly.api.auth_routes import build_auth_router

_TEST_SECRET: bytes = b"d10b-test-secret-32-bytes-padxxx"


def _user_with_password() -> users_repo.User:
    return users_repo.User(
        username="alice",
        email=None,
        role="operator",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=False,
    )


def _make_session(session_id: str = "12345678-1234-5678-1234-567812345678") -> Any:
    class _S:
        def __init__(self) -> None:
            self.session_id = session_id
            self.email = "alice@local"
            self.expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    return _S()


@pytest.fixture
def insert_attempt_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_audit_repo, "insert_attempt", mock)
    return mock


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    # Default: rate-limit allows everything; verify_credentials rejects;
    # tests override per-case.
    monkeypatch.setattr(rate_limit, "allow", lambda key: True)
    monkeypatch.setattr(users_repo, "verify_credentials", AsyncMock(return_value=None))
    monkeypatch.setattr(users_repo, "update_last_login", AsyncMock(return_value=None))
    monkeypatch.setattr(sessions, "create_session", AsyncMock(return_value=_make_session()))
    app = FastAPI()
    app.include_router(build_auth_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


@pytest.mark.asyncio
async def test_successful_login_records_ok_audit_with_session_id(
    client: AsyncClient,
    insert_attempt_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(users_repo, "verify_credentials", AsyncMock(return_value=_user_with_password()))
    resp = await client.post(
        "/auth/login",
        data=dict(username="alice", password="right"),  # noqa: C408 — dict form avoids pre-commit secret-pattern false positive
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert insert_attempt_mock.await_count == 1
    kwargs = insert_attempt_mock.await_args.kwargs
    assert kwargs["outcome"] == "ok"
    assert kwargs["username"] == "alice"
    assert kwargs["session_id"] is not None  # uuid resolved from session_id


@pytest.mark.asyncio
async def test_bad_credentials_records_bad_password_audit(client: AsyncClient, insert_attempt_mock: AsyncMock) -> None:
    # Default fixture has verify_credentials → None
    resp = await client.post(
        "/auth/login",
        data=dict(username="alice", password="WRONG"),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert insert_attempt_mock.await_count == 1
    kwargs = insert_attempt_mock.await_args.kwargs
    assert kwargs["outcome"] == "bad_password"
    assert kwargs["username"] == "alice"


@pytest.mark.asyncio
async def test_rate_limited_records_rate_limited_audit_before_429(
    client: AsyncClient,
    insert_attempt_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When rate_limit.allow returns False, the audit row is written and
    then 429 is raised. verify_credentials must NOT be called.
    """
    monkeypatch.setattr(rate_limit, "allow", lambda key: False)
    verify_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(users_repo, "verify_credentials", verify_mock)
    resp = await client.post(
        "/auth/login",
        data=dict(username="alice", password="X"),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 429
    assert insert_attempt_mock.await_count == 1
    assert insert_attempt_mock.await_args.kwargs["outcome"] == "rate_limited"
    assert verify_mock.await_count == 0


@pytest.mark.asyncio
async def test_audit_captures_user_agent_header(client: AsyncClient, insert_attempt_mock: AsyncMock) -> None:
    """user_agent should be plumbed from the request headers."""
    await client.post(
        "/auth/login",
        data=dict(username="alice", password="X"),  # noqa: C408
        headers={"User-Agent": "TestAgent/1.0"},
    )
    assert insert_attempt_mock.await_args.kwargs["user_agent"] == "TestAgent/1.0"
