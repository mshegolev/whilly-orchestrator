"""Unit tests for the TOTP second-factor surface (PRD §Epic E Item 14b).

Critical regression coverage: the submit_login state-machine change is
the highest-risk part of this PR. Tests pin BOTH branches —

* totp_enabled=False (default) → submit_login is byte-equivalent to
  pre-E14b. The maybe_intercept_for_totp helper returns None and the
  existing audit + session-creation path runs.
* totp_enabled=True + user has TOTP → submit_login returns 303 to
  /auth/totp with a signed pending cookie; no session is created
  until the second-factor verifies.

Plus contract pins for:
- pending cookie signature round-trip + tampering rejection
- /me/totp/setup GET renders the otpauth URI
- /me/totp/setup POST verifies the code + persists enabled=TRUE
- /auth/totp POST with valid code mints the real session cookie
- /auth/totp POST with bad code increments the per-cookie attempt
  counter and bounces after PENDING_MAX_ATTEMPTS
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

# pyotp ships in the [totp] optional extras only. CI runs without it
# installed by default, so guard the whole module — every test in this
# file needs pyotp for code generation/verification.
pyotp = pytest.importorskip("pyotp")

from fastapi import FastAPI  # noqa: E402 — must come after pytest.importorskip gate
from httpx import ASGITransport, AsyncClient  # noqa: E402

from whilly.api import (  # noqa: E402
    auth_tokens,
    rate_limit,
    sessions,
    totp_repo,
    totp_routes,
    users_repo,
)
from whilly.api.auth_routes import build_auth_router  # noqa: E402
from whilly.api.csrf import COOKIE_NAME  # noqa: E402
from whilly.api.totp_routes import (  # noqa: E402
    PENDING_COOKIE_NAME,
    PENDING_MAX_ATTEMPTS,
    TOTP_ENABLED_ENV,
    _mint_pending_cookie,
    _verify_pending_cookie,
    build_totp_router,
)

_TEST_SECRET: bytes = b"e14b-test-secret-32-bytes-padxxx"
_TEST_SESSION_ID: str = "00000000-1111-2222-3333-444444444444"
_USERNAME: str = "alice"
_EMAIL: str = f"{_USERNAME}@local"
_TEST_TOTP_SECRET: str = pyotp.random_base32()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(TOTP_ENABLED_ENV, raising=False)
    yield


def _user() -> users_repo.User:
    return users_repo.User(
        username=_USERNAME,
        email=None,
        role="operator",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=False,
    )


def _session() -> Any:
    class _S:
        def __init__(self) -> None:
            self.session_id = _TEST_SESSION_ID
            self.email = _EMAIL
            self.expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    return _S()


def _totp_row(enabled: bool = True, *, secret: str | None = None) -> totp_repo.UserTotpSecret:
    return totp_repo.UserTotpSecret(
        username=_USERNAME,
        secret=secret or _TEST_TOTP_SECRET,
        enabled=enabled,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )


def _mint_session_cookie() -> str:
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET, session_id=_TEST_SESSION_ID, email=_EMAIL, ttl_seconds=3600
    )


# ─── pending cookie signature contract ──────────────────────────────────────


def test_pending_cookie_roundtrip() -> None:
    raw = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    payload = _verify_pending_cookie(_TEST_SECRET, raw)
    assert payload is not None
    assert payload["u"] == _USERNAME
    assert payload["a"] == 0


def test_pending_cookie_rejects_tampering() -> None:
    raw = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    # Flip a byte in the body.
    tampered = "x" + raw[1:]
    assert _verify_pending_cookie(_TEST_SECRET, tampered) is None


def test_pending_cookie_rejects_wrong_secret() -> None:
    raw = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    assert _verify_pending_cookie(b"other-secret-32-bytes-padding!!", raw) is None


# ─── submit_login state-machine regression: TOTP disabled = unchanged ──────


@pytest.fixture
async def auth_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(rate_limit, "allow", lambda key: True)
    monkeypatch.setattr(users_repo, "verify_credentials", AsyncMock(return_value=_user()))
    monkeypatch.setattr(users_repo, "update_last_login", AsyncMock(return_value=None))
    monkeypatch.setattr(sessions, "create_session", AsyncMock(return_value=_session()))
    from whilly.api import auth_audit_repo

    monkeypatch.setattr(auth_audit_repo, "insert_attempt", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(build_auth_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


@pytest.mark.asyncio
async def test_submit_login_unchanged_when_totp_flag_off(
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With WHILLY_TOTP_ENABLED unset, submit_login goes straight to /
    even if the user has a TOTP secret enrolled. The intercept never fires."""
    # Even with TOTP enrolled on the row, flag-off must skip the lookup.
    get_totp_mock = AsyncMock(return_value=_totp_row())
    monkeypatch.setattr(totp_repo, "get_totp_secret", get_totp_mock)
    resp = await auth_client.post(
        "/auth/login",
        data=dict(username=_USERNAME, password="X"),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Should redirect to / (or /auth/change-password if must_change). NOT /auth/totp.
    assert "/auth/totp" not in (resp.headers.get("location") or "")
    # Flag-off short-circuits BEFORE any TOTP DB lookup.
    assert get_totp_mock.await_count == 0


@pytest.mark.asyncio
async def test_submit_login_unchanged_when_user_has_no_totp(
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON but user has no TOTP row → same as flag-off; redirect to /."""
    monkeypatch.setenv(TOTP_ENABLED_ENV, "1")
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=None))
    resp = await auth_client.post(
        "/auth/login",
        data=dict(username=_USERNAME, password="X"),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/auth/totp" not in (resp.headers.get("location") or "")


@pytest.mark.asyncio
async def test_submit_login_redirects_to_totp_when_enabled_and_enrolled(
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON + user has TOTP enabled → 303 to /auth/totp with pending cookie."""
    monkeypatch.setenv(TOTP_ENABLED_ENV, "1")
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=_totp_row(enabled=True)))
    create_session_mock = AsyncMock(return_value=_session())
    monkeypatch.setattr(sessions, "create_session", create_session_mock)
    resp = await auth_client.post(
        "/auth/login",
        data=dict(username=_USERNAME, password="X"),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/auth/totp"
    # No real session cookie was minted.
    assert COOKIE_NAME not in resp.cookies
    # Pending cookie WAS set.
    set_cookie = resp.headers.get("set-cookie", "")
    assert PENDING_COOKIE_NAME in set_cookie
    # And sessions.create_session must NOT have been called yet.
    assert create_session_mock.await_count == 0


@pytest.mark.asyncio
async def test_submit_login_unchanged_when_user_totp_disabled_but_enrolled(
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON + user enrolled but enabled=FALSE (mid-setup) → no intercept."""
    monkeypatch.setenv(TOTP_ENABLED_ENV, "1")
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=_totp_row(enabled=False)))
    resp = await auth_client.post(
        "/auth/login",
        data=dict(username=_USERNAME, password="X"),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/auth/totp" not in (resp.headers.get("location") or "")


# ─── /me/totp/setup ─────────────────────────────────────────────────────────


@pytest.fixture
async def totp_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    # The verify endpoint now IP-rate-limits and consults a server-side lockout
    # (fix for the cookie-replay brute-force). Neutralise both by default so the
    # functional verify tests are deterministic; individual tests override them.
    monkeypatch.setattr(rate_limit, "allow", lambda key: True)
    monkeypatch.setattr(users_repo, "is_account_locked", AsyncMock(return_value=False))
    monkeypatch.setattr(users_repo, "register_failed_second_factor", AsyncMock(return_value=False))
    app = FastAPI()
    app.include_router(build_totp_router(pool=None, secret=_TEST_SECRET))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


@pytest.mark.asyncio
async def test_totp_setup_get_renders_otpauth_uri(totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=None))
    resp = await totp_client.get("/me/totp/setup", cookies={COOKIE_NAME: _mint_session_cookie()})
    assert resp.status_code == 200
    assert "otpauth://totp/" in resp.text
    assert _USERNAME in resp.text


@pytest.mark.asyncio
async def test_totp_setup_post_wrong_code_returns_422(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(totp_repo, "upsert_totp_secret", AsyncMock(return_value=None))
    secret_b32 = pyotp.random_base32()
    resp = await totp_client.post(
        "/me/totp/setup",
        cookies={COOKIE_NAME: _mint_session_cookie()},
        data=dict(secret_b32=secret_b32, code="000000"),  # noqa: C408
    )
    assert resp.status_code == 422
    # Apostrophe in "didn't" gets HTML-escaped to &#39; — match a substring
    # without one to keep the assertion brittleness-free.
    assert "verify" in resp.text and "totp-error" in resp.text


@pytest.mark.asyncio
async def test_totp_setup_post_valid_code_persists_enabled(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(totp_repo, "upsert_totp_secret", upsert_mock)
    secret_b32 = pyotp.random_base32()
    correct_code = pyotp.TOTP(secret_b32).now()
    resp = await totp_client.post(
        "/me/totp/setup",
        cookies={COOKIE_NAME: _mint_session_cookie()},
        data=dict(secret_b32=secret_b32, code=correct_code),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert upsert_mock.await_count == 1
    assert upsert_mock.await_args.kwargs["enabled"] is True


# ─── /auth/totp verification ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_totp_verify_get_without_pending_cookie_redirects_to_login(
    totp_client: AsyncClient,
) -> None:
    resp = await totp_client.get("/auth/totp", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_totp_verify_post_correct_code_mints_session_and_clears_pending(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=_totp_row()))
    monkeypatch.setattr(sessions, "create_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "update_last_login", AsyncMock(return_value=None))
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    correct_code = pyotp.TOTP(_TEST_TOTP_SECRET).now()
    resp = await totp_client.post(
        "/auth/totp",
        cookies={PENDING_COOKIE_NAME: pending},
        data=dict(code=correct_code),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] in ("/", "/auth/change-password")
    # Real session cookie was minted.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "whilly_session=" in set_cookie
    # Pending cookie was cleared.
    assert "whilly_totp_pending=;" in set_cookie or "Max-Age=0" in set_cookie


@pytest.mark.asyncio
async def test_totp_verify_post_wrong_code_increments_attempts(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=_totp_row()))
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    resp = await totp_client.post(
        "/auth/totp",
        cookies={PENDING_COOKIE_NAME: pending},
        data=dict(code="000000"),  # noqa: C408
    )
    assert resp.status_code == 422
    assert "Wrong code" in resp.text
    # Pending cookie re-issued with bumped attempt counter.
    set_cookie = resp.headers.get("set-cookie", "")
    assert PENDING_COOKIE_NAME in set_cookie


@pytest.mark.asyncio
async def test_totp_verify_post_too_many_failures_clears_cookie(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=_totp_row()))
    # Pre-set attempts to the max so the next wrong code trips the lock.
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, attempts=PENDING_MAX_ATTEMPTS - 1)
    resp = await totp_client.post(
        "/auth/totp",
        cookies={PENDING_COOKIE_NAME: pending},
        data=dict(code="000000"),  # noqa: C408
    )
    assert resp.status_code == 429
    # Pending cookie cleared.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "whilly_totp_pending=;" in set_cookie or "Max-Age=0" in set_cookie


# ─── brute-force hardening (fix: client-cookie counter was bypassable) ──────


@pytest.mark.asyncio
async def test_totp_verify_rate_limited_returns_429(totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 1: the verify endpoint is now IP-rate-limited like the password one."""
    from whilly.api import auth_audit_repo

    monkeypatch.setattr(rate_limit, "allow", lambda key: False)
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_audit_repo, "insert_attempt", audit)
    get_totp = AsyncMock(return_value=_totp_row())
    monkeypatch.setattr(totp_repo, "get_totp_secret", get_totp)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    resp = await totp_client.post(
        "/auth/totp",
        cookies={PENDING_COOKIE_NAME: pending},
        data=dict(code="000000"),  # noqa: C408
    )
    assert resp.status_code == 429
    # Rate-limited before any TOTP secret lookup, and audited as 'rate_limited'.
    assert get_totp.await_count == 0
    assert audit.await_args.kwargs["outcome"] == "rate_limited"


@pytest.mark.asyncio
async def test_totp_verify_server_lock_beats_fresh_cookie(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 2 / the core fix: a pristine ``a=0`` pending cookie AND a correct
    code are still rejected when the server-side lockout is set — proving the
    cookie-replay bypass of the per-cookie counter no longer grants attempts."""
    monkeypatch.setattr(users_repo, "is_account_locked", AsyncMock(return_value=True))
    create = AsyncMock(return_value=_session())
    monkeypatch.setattr(sessions, "create_session", create)
    get_totp = AsyncMock(return_value=_totp_row())
    monkeypatch.setattr(totp_repo, "get_totp_secret", get_totp)
    fresh_cookie = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, attempts=0)
    correct_code = pyotp.TOTP(_TEST_TOTP_SECRET).now()
    resp = await totp_client.post(
        "/auth/totp",
        cookies={PENDING_COOKIE_NAME: fresh_cookie},
        data=dict(code=correct_code),  # noqa: C408
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # No session minted, and we never even reached the TOTP verify.
    assert create.await_count == 0
    assert get_totp.await_count == 0
    set_cookie = resp.headers.get("set-cookie", "")
    assert "whilly_totp_pending=;" in set_cookie or "Max-Age=0" in set_cookie


@pytest.mark.asyncio
async def test_totp_verify_wrong_code_bumps_server_side_counter(
    totp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong code increments the server-side budget, not just the cookie."""
    monkeypatch.setattr(totp_repo, "get_totp_secret", AsyncMock(return_value=_totp_row()))
    bump = AsyncMock(return_value=False)
    monkeypatch.setattr(users_repo, "register_failed_second_factor", bump)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME)
    resp = await totp_client.post(
        "/auth/totp",
        cookies={PENDING_COOKIE_NAME: pending},
        data=dict(code="000000"),  # noqa: C408
    )
    assert resp.status_code == 422
    bump.assert_awaited_once()
    assert bump.await_args.kwargs["username"] == _USERNAME


# silence unused-import lint
_ = totp_routes
