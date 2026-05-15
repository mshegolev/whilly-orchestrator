"""Auth matrix integration tests for PRD-wui-multi-plan v2 Block 6.

Covers SC-1.3, SC-1.4, SC-5.1 from the migration PRD:

* SC-1.3 — CSRF middleware accepts ``Origin`` from the allowlist on a
  cookie-authenticated state-mutating request, and rejects mismatched
  ``Origin`` with 403 ``csrf_origin_check_failed``.
* SC-1.4 — the CSRF middleware is a no-op when the session cookie is
  absent (worker bearer / dashboard JWT traffic must pass through
  unaffected).
* SC-5.1 — the new CRUD surface (``/api/v1/plans``) is session-only:
  worker bearer tokens are rejected. Conversely the worker contract
  ``/tasks/claim`` rejects a session cookie alone (no bearer → 401).

Why a hand-rolled FastAPI app fixture instead of the production
:func:`whilly.adapters.transport.server.create_app`?
    ``create_app`` generates a *fresh* random ``dashboard_token_secret``
    on every call and does not stash it on ``app.state`` (the secret
    lives inside the closure). The matrix tests need the SAME secret
    that was used to sign the session cookie so they can mint a valid
    cookie value programmatically without going through the full
    magic-link → consume → set-cookie round-trip. Building a minimal
    app with a known secret keeps the test fast and focused on the
    auth-matrix invariants rather than the magic-link flow (the latter
    is covered by ``tests/test_magic_link_reuse.py``).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.auth import make_bearer_auth
from whilly.adapters.transport.server import CLAIM_PATH, create_app
from whilly.api import auth_tokens, sessions
from whilly.api.auth_routes import build_auth_router
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.api.plans_api import build_plans_router

pytestmark = DOCKER_REQUIRED

_TEST_SECRET: bytes = b"matrix-test-secret-32-bytes-pad!"
_GOOD_ORIGIN: str = "http://127.0.0.1:8000"
_BAD_ORIGIN: str = "http://evil.example.com"
_WORKER_TOKEN: str = "matrix-worker-bearer-token"


@pytest.fixture(autouse=True)
async def _truncate_auth_tables(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Reset ``magic_links`` and ``sessions`` between tests.

    The session-scoped DB conftest truncates events / tasks / plans
    only; the v2 auth tables need a per-test wipe so leftovers from a
    sibling test do not contaminate the matrix invariants below.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE magic_links, sessions")
    yield


@pytest.fixture
async def auth_app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """Build a minimal FastAPI app wired with auth + plans routers + CSRF + a faux /tasks/claim.

    Uses a known ``_TEST_SECRET`` so tests can mint session cookies and
    have ``verify_session_cookie_value`` accept them without round-tripping
    through the magic-link flow.
    """
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware, allowlist=[_GOOD_ORIGIN])
    app.include_router(build_auth_router(pool=db_pool, secret=_TEST_SECRET))
    app.include_router(build_plans_router(pool=db_pool, secret=_TEST_SECRET))

    # Lightweight stand-in for the worker contract: bearer-only.
    # Mirrors the production wiring's Depends(bearer) gate so we can
    # assert "cookie-only request → 401" without standing up the full
    # control-plane app.
    bearer = make_bearer_auth(_WORKER_TOKEN)

    @app.post(CLAIM_PATH, dependencies=[Depends(bearer)])
    async def claim_endpoint() -> dict[str, str]:
        return {"task_id": "noop"}

    yield app


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        yield client


async def _mint_session_cookie(db_pool: asyncpg.Pool) -> str:
    """Insert a sessions row and mint the matching signed cookie value."""
    session = await sessions.create_session(db_pool, email="matrix@example.com")
    ttl = max(1, int(session.expires_at.timestamp() - time.time()))
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET,
        session_id=session.session_id,
        email=session.email,
        ttl_seconds=ttl,
    )


# ─── SC-1.3 + SC-5.1: POST /api/v1/plans with cookie + good Origin ──────────


@pytest.mark.asyncio
async def test_post_plans_with_session_and_good_origin_is_not_csrf_blocked(
    auth_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A session cookie + allowlisted Origin must not be 403'd by CSRF.

    The endpoint may still return 400/422 for an invalid body shape,
    but it must NOT return 403 ``csrf_origin_check_failed`` — that
    would mean the CSRF middleware blocked a legitimate request.
    """
    cookie_value = await _mint_session_cookie(db_pool)
    response = await auth_client.post(
        "/api/v1/plans",
        json={"plan_id": "matrix-good-origin", "name": "Matrix good origin"},
        cookies={COOKIE_NAME: cookie_value},
        headers={"Origin": _GOOD_ORIGIN},
    )
    assert response.status_code != 403, (
        f"good-Origin cookie request was blocked by CSRF: status={response.status_code} body={response.text!r}"
    )
    # Success (201) or validation rejection (4xx) — both prove CSRF
    # passed the request through.
    assert response.status_code in {201, 400, 409, 422}, f"unexpected status {response.status_code}: {response.text!r}"


# ─── SC-1.3: POST /api/v1/plans with cookie + bad Origin ────────────────────


@pytest.mark.asyncio
async def test_post_plans_with_session_and_bad_origin_is_csrf_blocked(
    auth_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A session cookie + mismatched Origin must produce 403 csrf_origin_check_failed."""
    cookie_value = await _mint_session_cookie(db_pool)
    response = await auth_client.post(
        "/api/v1/plans",
        json={"plan_id": "matrix-bad-origin", "name": "Matrix bad origin"},
        cookies={COOKIE_NAME: cookie_value},
        headers={"Origin": _BAD_ORIGIN},
    )
    assert response.status_code == 403, f"expected 403, got {response.status_code}: {response.text!r}"
    body = response.json()
    assert body.get("error") == "csrf_origin_check_failed", body


# ─── SC-5.1: POST /api/v1/plans with worker bearer (no cookie) → 401 ────────


@pytest.mark.asyncio
async def test_post_plans_with_worker_bearer_is_rejected(
    auth_client: AsyncClient,
) -> None:
    """The new CRUD surface is session-only — worker bearer tokens must not pass.

    Note: no session cookie is sent, so CSRF middleware is a no-op
    (SC-1.4 fall-through); the rejection comes from
    ``authenticate_session_request`` returning 401.
    """
    response = await auth_client.post(
        "/api/v1/plans",
        json={"plan_id": "matrix-bearer", "name": "Matrix bearer"},
        headers={
            "Authorization": f"Bearer {_WORKER_TOKEN}",
            "Origin": _GOOD_ORIGIN,
        },
    )
    assert response.status_code == 401, response.text


# ─── SC-5.1: GET /api/v1/plans without auth → 401 ────────────────────────────


@pytest.mark.asyncio
async def test_get_plans_without_auth_is_rejected(auth_client: AsyncClient) -> None:
    """Listing plans requires a session — no anonymous reads on CRUD surface."""
    response = await auth_client.get("/api/v1/plans")
    assert response.status_code == 401, response.text


# ─── SC-5.1 (mirror): POST /tasks/claim with cookie only → 401 ──────────────


@pytest.mark.asyncio
async def test_tasks_claim_with_session_cookie_only_is_rejected(
    auth_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """The worker RPC surface rejects a cookie-only request.

    The cookie does not implicitly grant bearer-equivalent access:
    /tasks/claim requires ``Authorization: Bearer <worker_token>``.
    CSRF middleware will trigger first because the cookie is present
    AND the method is state-mutating AND the path is not exempt —
    but the request *does* carry the good Origin, so CSRF passes and
    the bearer-auth dependency returns 401.
    """
    cookie_value = await _mint_session_cookie(db_pool)
    response = await auth_client.post(
        CLAIM_PATH,
        json={"worker_id": "matrix-worker"},
        cookies={COOKIE_NAME: cookie_value},
        headers={"Origin": _GOOD_ORIGIN},
    )
    assert response.status_code == 401, response.text


# ─── SC-1.4: CSRF middleware is no-op when no cookie is present ─────────────


@pytest.mark.asyncio
async def test_csrf_middleware_does_not_trigger_without_cookie(
    auth_client: AsyncClient,
) -> None:
    """Bearer-only request with bad Origin must reach the handler (no CSRF gate).

    The CSRF middleware only protects cookie-authenticated mutations.
    Bearer-auth traffic is exempt by design (PRD §6.1) so the request
    flows through to the bearer dependency, which returns 401 for the
    missing/invalid token here (we send no Authorization header).
    A 403 ``csrf_origin_check_failed`` would be a regression.
    """
    response = await auth_client.post(
        CLAIM_PATH,
        json={"worker_id": "no-cookie-worker"},
        headers={"Origin": _BAD_ORIGIN},
    )
    # No cookie → CSRF does not run → bearer dep enforces auth → 401
    assert response.status_code == 401, response.text
    body = response.json()
    # Crucially: this is NOT a CSRF-shaped response.
    assert body.get("error") != "csrf_origin_check_failed", body


# ─── Smoke: full create_app stack does not break the matrix invariants ──────


@pytest.mark.asyncio
async def test_create_app_full_stack_csrf_matches_minimal_app(
    db_pool: asyncpg.Pool,
) -> None:
    """End-to-end smoke: the production ``create_app`` mounts CSRF correctly.

    We can't easily inject a cookie value because the production app
    mints its own ``dashboard_token_secret`` per call. But we *can*
    verify that GET /api/v1/plans without auth returns 401 (not 500,
    not 200) — proving the auth gate is wired into the production
    composition.
    """
    app = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token="matrix-bootstrap-token",
        claim_long_poll_timeout=0.2,
        claim_poll_interval=0.05,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            response = await client.get("/api/v1/plans")
            assert response.status_code == 401, response.text
