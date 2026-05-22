"""Unit tests for :class:`whilly.api.must_change_gate.MustChangePasswordGateMiddleware`.

These pin the contract of PRD-post-auth-hardening §Epic C, Item 6 without
requiring a real Postgres pool — :func:`whilly.api.sessions.verify_session`
and :func:`whilly.api.users_repo.get_user_by_username` are monkeypatched
with :class:`AsyncMock`s so the tests can assert call counts to verify
caching behaviour (AC: ≤ 1 DB lookup over 5 rapid requests).

Integration coverage against a real DB lives in the auth-matrix tests
once D9's full change-password round-trip exists; this file is the
narrow contract pin so a regression in the middleware itself surfaces
without testcontainers.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, must_change_gate, sessions, users_repo
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.api.must_change_gate import (
    CACHE_TTL_SECONDS,
    CHANGE_PASSWORD_PATH,
    MustChangePasswordGateMiddleware,
    _clear_cache,
    invalidate_session,
)

# 32-byte secret used to sign session cookies in these tests. Must be a
# `bytes` long enough for HMAC-SHA256 — any constant string of the right
# length is fine.
_TEST_SECRET: bytes = b"gate-test-secret-32-bytes-padxx!"
_GOOD_ORIGIN: str = "http://127.0.0.1:8000"
_TEST_SESSION_ID: str = "test-session-id-abc123"
_TEST_USERNAME: str = "alice"
_TEST_EMAIL: str = f"{_TEST_USERNAME}@local"


@pytest.fixture(autouse=True)
def _reset_gate_cache() -> Iterator[None]:
    """Wipe the gate's process-local cache between tests."""
    _clear_cache()
    yield
    _clear_cache()


def _make_session_record() -> Any:
    """Build a session-like object with the attributes the gate reads."""

    class _SessionStub:
        session_id = _TEST_SESSION_ID
        email = _TEST_EMAIL
        # Real :class:`whilly.api.sessions.Session` carries more — the gate
        # only reads ``email``, so the stub stays narrow.
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    return _SessionStub()


def _make_user(*, must_change: bool) -> users_repo.User:
    """Construct a User dataclass instance with the ``must_change`` flag set."""
    return users_repo.User(
        username=_TEST_USERNAME,
        email=None,
        role="operator",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=must_change,
    )


def _mint_cookie(*, session_id: str = _TEST_SESSION_ID) -> str:
    """Mint a session cookie that the gate's signature check will accept."""
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET,
        session_id=session_id,
        email=_TEST_EMAIL,
        ttl_seconds=3600,
    )


@pytest.fixture
def patched_sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch ``sessions.verify_session`` to return a fixed session stub."""
    mock = AsyncMock(return_value=_make_session_record())
    monkeypatch.setattr(sessions, "verify_session", mock)
    return mock


@pytest.fixture
def patched_users_must_change(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the gate's user resolver to return a must-change user."""
    mock = AsyncMock(return_value=_make_user(must_change=True))
    # The gate imports the resolver directly into its own namespace, so
    # patching the module-level binding inside ``must_change_gate`` is
    # required — patching ``users_repo.get_user_by_session_email`` alone would
    # not redirect the call site inside ``_lookup_must_change``.
    monkeypatch.setattr(must_change_gate, "get_user_by_session_email", mock)
    return mock


@pytest.fixture
def patched_users_ok(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the gate's user resolver to return a no-must-change user."""
    mock = AsyncMock(return_value=_make_user(must_change=False))
    monkeypatch.setattr(must_change_gate, "get_user_by_session_email", mock)
    return mock


def _build_app() -> FastAPI:
    """Minimal app with the gate + CSRF + a couple of routes to drive."""
    app = FastAPI()
    # Ordering: gate added FIRST, CSRF added SECOND. Starlette LIFO means
    # CSRF is the outer layer at runtime (CSRF runs first, then gate).
    app.add_middleware(
        MustChangePasswordGateMiddleware,
        pool=None,  # type: ignore[arg-type] — gate never touches the pool, mocks intercept
        secret=_TEST_SECRET,
    )
    app.add_middleware(WhillySessionCSRFMiddleware, allowlist=[_GOOD_ORIGIN])

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get(CHANGE_PASSWORD_PATH)
    async def change_pw_form() -> dict[str, str]:
        return {"form": "change-password"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/static/app.css")
    async def static_asset() -> dict[str, str]:
        return {"asset": "css"}

    @app.post("/api/v1/foo")
    async def post_foo() -> dict[str, str]:
        return {"posted": "yes"}

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
        yield ac


# ─── AC1: GET / with must_change=True redirects ─────────────────────────────


@pytest.mark.asyncio
async def test_root_with_must_change_redirects_303(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    """Authenticated GET / with must_change_password=True → 303 to change-password."""
    cookie = _mint_cookie()
    response = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == CHANGE_PASSWORD_PATH
    # Sanity: the gate actually consulted the DB once.
    assert patched_sessions.await_count == 1
    assert patched_users_must_change.await_count == 1


# ─── AC2: GET /auth/change-password with the flag set passes through ────────


@pytest.mark.asyncio
async def test_change_password_path_passes_through_when_must_change_true(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    """The whitelist must include the change-password path itself."""
    cookie = _mint_cookie()
    response = await client.get(CHANGE_PASSWORD_PATH, cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"form": "change-password"}
    # Whitelist short-circuits BEFORE any DB call.
    assert patched_sessions.await_count == 0
    assert patched_users_must_change.await_count == 0


# ─── AC3: after invalidation, the next request to / returns 200 ─────────────


@pytest.mark.asyncio
async def test_invalidate_session_clears_cache_after_password_change(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    patched_sessions: AsyncMock,
) -> None:
    """Simulate the post-password-change flow: first call sees must_change=True
    and is cached; ``invalidate_session`` drops the entry; the next lookup
    returns must_change=False and the request to / is served (200).
    """
    user_mock = AsyncMock(side_effect=[_make_user(must_change=True), _make_user(must_change=False)])
    monkeypatch.setattr(must_change_gate, "get_user_by_session_email", user_mock)
    cookie = _mint_cookie()

    # First request — cache miss, returns the must_change=True user → 303.
    r1 = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert r1.status_code == 303

    # Simulate the change-password handler's invalidation step.
    invalidate_session(_TEST_SESSION_ID)

    # Second request — cache miss again because we invalidated; returns the
    # second side_effect (must_change=False) → 200.
    r2 = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert r2.status_code == 200
    assert user_mock.await_count == 2


# ─── AC4: 5 rapid requests result in ≤ 1 DB lookup (cache hit) ──────────────


@pytest.mark.asyncio
async def test_cache_hit_collapses_five_rapid_requests_to_one_db_lookup(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    """The 30-s cache must collapse 5 successive requests into a single DB pair.

    PRD AC: 'Cache hit confirmed by asserting DB query count ≤ 1 for 5
    rapid successive requests (mock.call_count)'. The gate makes two DB
    calls on a miss (verify_session + get_user_by_username); the cache
    is keyed at the verdict level, so both must be called exactly once.
    """
    cookie = _mint_cookie()
    for _ in range(5):
        response = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
        assert response.status_code == 303
    assert patched_sessions.await_count == 1
    assert patched_users_must_change.await_count == 1


# ─── Whitelist: /health, /static/* bypass without any DB hit ────────────────


@pytest.mark.asyncio
async def test_health_path_bypasses_gate(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    cookie = _mint_cookie()
    response = await client.get("/health", cookies={COOKIE_NAME: cookie})
    assert response.status_code == 200
    assert patched_sessions.await_count == 0
    assert patched_users_must_change.await_count == 0


@pytest.mark.asyncio
async def test_static_path_bypasses_gate(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    cookie = _mint_cookie()
    response = await client.get("/static/app.css", cookies={COOKIE_NAME: cookie})
    assert response.status_code == 200
    assert patched_sessions.await_count == 0
    assert patched_users_must_change.await_count == 0


# ─── No cookie → no DB hit, passes through ──────────────────────────────────


@pytest.mark.asyncio
async def test_request_without_cookie_passes_through_no_db_hit(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    """Bearer/JWT traffic doesn't carry the session cookie; gate must
    pass through without any DB call.
    """
    response = await client.get("/")
    assert response.status_code == 200
    assert patched_sessions.await_count == 0
    assert patched_users_must_change.await_count == 0


# ─── Bad cookie signature → passes through (gate fail-opens) ────────────────


@pytest.mark.asyncio
async def test_malformed_cookie_fails_open_without_db_hit(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    response = await client.get("/", cookies={COOKIE_NAME: "not-a-real-cookie"})
    assert response.status_code == 200
    assert patched_sessions.await_count == 0
    assert patched_users_must_change.await_count == 0


# ─── user lookup returns None (e.g. magic-link user) → pass through ─────────


@pytest.mark.asyncio
async def test_user_not_found_passes_through(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    patched_sessions: AsyncMock,
) -> None:
    monkeypatch.setattr(must_change_gate, "get_user_by_session_email", AsyncMock(return_value=None))
    cookie = _mint_cookie()
    response = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert response.status_code == 200


# ─── must_change=False user → pass through without redirect ─────────────────


@pytest.mark.asyncio
async def test_user_without_must_change_passes_through(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_ok: AsyncMock,
) -> None:
    cookie = _mint_cookie()
    response = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert response.status_code == 200
    assert patched_users_ok.await_count == 1


# ─── AC5: CSRF middleware still protects POST routes (ordering check) ───────


@pytest.mark.asyncio
async def test_csrf_still_blocks_bad_origin_post_with_session(
    client: AsyncClient,
    patched_sessions: AsyncMock,
    patched_users_ok: AsyncMock,
) -> None:
    """The gate sitting under CSRF must not steal traffic CSRF wants to reject.

    A cookie-authenticated POST from a disallowed Origin should be 403'd
    by the (outermost) CSRF middleware before the gate runs. If the gate
    were outermost, this would be a 200 or a redirect — both wrong.
    """
    cookie = _mint_cookie()
    response = await client.post(
        "/api/v1/foo",
        cookies={COOKIE_NAME: cookie},
        headers={"Origin": "http://evil.example.com"},
    )
    assert response.status_code == 403
    assert "csrf_origin_check_failed" in response.text
    # The gate never ran — CSRF rejected the request before lookups.
    assert patched_sessions.await_count == 0
    assert patched_users_ok.await_count == 0


# ─── Cache TTL: after expiry, the next request re-hits the DB ───────────────


@pytest.mark.asyncio
async def test_cache_expires_after_ttl_seconds(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    patched_sessions: AsyncMock,
    patched_users_must_change: AsyncMock,
) -> None:
    """After CACHE_TTL_SECONDS, the gate must re-consult the DB.

    Faked by advancing :func:`time.monotonic` past the TTL boundary
    between two requests. The verdict here happens to stay the same,
    but the call_count is what matters: 2 lookups, not 1.
    """
    cookie = _mint_cookie()

    # First request — cache miss, 1 lookup.
    r1 = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert r1.status_code == 303
    assert patched_users_must_change.await_count == 1

    # Advance monotonic clock past the TTL.
    real_monotonic = time.monotonic
    base = real_monotonic()

    def _shifted() -> float:
        return base + CACHE_TTL_SECONDS + 1.0

    monkeypatch.setattr(must_change_gate.time, "monotonic", _shifted)

    # Second request — cache expired, 1 fresh lookup.
    r2 = await client.get("/", cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert r2.status_code == 303
    assert patched_users_must_change.await_count == 2
