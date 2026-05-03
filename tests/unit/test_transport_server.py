"""Unit tests for :mod:`whilly.adapters.transport.server` (TASK-021a3, PRD FR-1.2 / TC-6).

The server module is the composition root of the control-plane HTTP API:
it wires the asyncpg pool, the auth dependencies and (in TASK-021b/c) the
worker-facing routes into a single FastAPI app via :func:`create_app`.
For TASK-021a3 the surface is intentionally narrow — factory + ``/health``
+ ``/docs`` — and these tests pin exactly that surface so later
extensions don't accidentally break the public contract.

What we cover here
------------------
* :func:`create_app` returns a FastAPI app with the routes wired:
  ``/health`` (pinging the pool), ``/docs`` (Swagger UI),
  ``/openapi.json`` (the spec).
* ``/health`` returns 200 ``{"status": "ok"}`` against a healthy pool,
  503 against a pool whose ``acquire()`` / ``fetchval()`` raises, and
  503 if ``SELECT 1`` returns something other than ``1`` (defence-in-
  depth path that would normally only trigger against a misconfigured
  proxy or a buggy test stub).
* ``/health`` requires no ``Authorization`` header — sending one with a
  wrong token still returns 200, because /health predates auth on the
  request flow (PRD: probe must work without credentials).
* :func:`create_app` token resolution: explicit kwarg wins; missing
  kwargs fall back to env; missing env *and* missing kwarg raises
  :class:`RuntimeError` with a message that names the env var so
  operators don't have to grep.
* ``/docs`` and ``/openapi.json`` are reachable without auth — operators
  expect them on the FastAPI defaults.

How we test without a real Postgres
-----------------------------------
The factory's only dependency on asyncpg is the ``pool.acquire()`` async
context manager that yields a connection with ``fetchval(sql)``. We hand
the factory a :class:`_FakePool` that implements exactly those two
methods — no socket, no Postgres, no testcontainers needed. Integration
coverage (real pool, real Postgres) lives in the TASK-021b/c integration
suites where the auth and route wiring matters end-to-end.

Why we don't use :mod:`unittest.mock` for the pool: a hand-rolled fake
makes the failure-mode tests (``raise on acquire``, ``return -1 from
fetchval``) easy to read at the call site without setting up
``side_effect`` chains. The fake is ~30 lines and reads as a contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whilly.adapters.transport import auth as auth_module
from whilly.adapters.transport.auth import (
    BOOTSTRAP_TOKEN_ENV,
    WORKER_TOKEN_ENV,
)
from whilly.adapters.transport.server import HEALTH_PATH, create_app


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
#
# ``_FakeConn`` and ``_FakePool`` mimic just enough of asyncpg's surface
# for the /health probe. The real :class:`asyncpg.Pool` is structurally
# compatible (its ``acquire()`` returns an async context manager that
# yields a Connection with ``fetchval(sql) -> Any``); the integration
# tests in TASK-021b/c verify the structural compatibility against a
# real Postgres so we don't have to maintain it here.


class _FakeConn:
    """Stub for ``asyncpg.Connection`` — only ``fetchval`` is exercised."""

    def __init__(self, *, value: Any = 1, raise_on_fetchval: BaseException | None = None) -> None:
        self._value = value
        self._raise_on_fetchval = raise_on_fetchval

    async def fetchval(self, query: str, *args: Any) -> Any:
        # Test-only sanity check — if the server module ever issues a
        # query other than SELECT 1 from the health probe, the test
        # contract changes and this assertion fires loudly.
        assert query.strip().upper() == "SELECT 1", query
        if self._raise_on_fetchval is not None:
            raise self._raise_on_fetchval
        return self._value


class _AcquireCtx:
    """Async context manager wrapper that yields a :class:`_FakeConn`.

    asyncpg's :meth:`Pool.acquire` returns an awaitable that *also*
    works as an async context manager (handy for the
    ``async with pool.acquire() as conn:`` pattern). Our fake only
    implements the context-manager half because that's the only shape
    server.py uses.
    """

    def __init__(self, conn: _FakeConn, *, raise_on_acquire: BaseException | None = None) -> None:
        self._conn = conn
        self._raise_on_acquire = raise_on_acquire

    async def __aenter__(self) -> _FakeConn:
        if self._raise_on_acquire is not None:
            raise self._raise_on_acquire
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakePool:
    """Stub for ``asyncpg.Pool``.

    Constructor flags choose which failure mode (if any) the next
    ``acquire()`` triggers. A single instance can serve many concurrent
    requests because ``_AcquireCtx`` is created fresh per call — the
    fake is intentionally not stateful.
    """

    def __init__(
        self,
        *,
        fetchval_value: Any = 1,
        raise_on_acquire: BaseException | None = None,
        raise_on_fetchval: BaseException | None = None,
    ) -> None:
        self._fetchval_value = fetchval_value
        self._raise_on_acquire = raise_on_acquire
        self._raise_on_fetchval = raise_on_fetchval
        self.acquire_calls = 0

    def acquire(self) -> _AcquireCtx:
        self.acquire_calls += 1
        return _AcquireCtx(
            _FakeConn(
                value=self._fetchval_value,
                raise_on_fetchval=self._raise_on_fetchval,
            ),
            raise_on_acquire=self._raise_on_acquire,
        )


def _as_pool(fake: _FakePool) -> asyncpg.Pool:
    """Cast a :class:`_FakePool` to :class:`asyncpg.Pool` for the public API.

    :func:`create_app` is typed as ``pool: asyncpg.Pool`` because that's
    what production callers pass. The fake is structurally compatible
    (only ``acquire()`` is touched); we cast at the boundary so the
    test bodies stay free of type ignores.
    """
    return cast(asyncpg.Pool, fake)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_auth_caches() -> Iterator[None]:
    """Clear the lazy auth shims so cross-test env mutations stay isolated.

    :mod:`whilly.adapters.transport.auth` caches the token-bound closure
    on first use. ``create_app`` doesn't touch the lazy shims directly
    (it goes through the explicit factories), but a leftover cache from
    a previous test could mask a token bug here.
    """
    auth_module.reset_lazy_dependencies()
    yield
    auth_module.reset_lazy_dependencies()


@pytest.fixture
def healthy_pool() -> _FakePool:
    """Default ``_FakePool`` whose ``SELECT 1`` returns ``1``."""
    return _FakePool()


@pytest.fixture
def app(healthy_pool: _FakePool) -> FastAPI:
    """Standard app instance with explicit (env-free) tokens."""
    return create_app(
        _as_pool(healthy_pool),
        worker_token="worker-tok-test",
        bootstrap_token="bootstrap-tok-test",
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Starlette TestClient that runs the app's ``lifespan`` automatically.

    Using ``with TestClient(app) as client`` is what triggers the
    lifespan startup/shutdown — without the ``with`` block the lifespan
    never runs and ``app.state.pool`` stays unset, masking bugs in
    handlers that read state.
    """
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# /health — happy path
# ---------------------------------------------------------------------------


def test_health_returns_200_against_healthy_pool(client: TestClient, healthy_pool: _FakePool) -> None:
    """The probe pings the pool exactly once and reports ``status=ok``."""
    response = client.get(HEALTH_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Probe must actually call acquire — a misimplemented endpoint that
    # always returns 200 without touching the pool would silently mask a
    # broken Postgres link in production.
    assert healthy_pool.acquire_calls == 1


def test_health_works_without_authorization_header(client: TestClient) -> None:
    """``/health`` is unauthenticated — kube probes don't carry tokens."""
    response = client.get(HEALTH_PATH)
    # No Authorization header is sent — the route must still answer 200,
    # not 401. This is the load-bearing AC for TASK-021a3.
    assert response.status_code == 200


def test_health_ignores_invalid_authorization_header(client: TestClient) -> None:
    """A wrong/garbage Authorization header doesn't break ``/health``.

    The probe must not be coupled to the auth dependency at all — even
    a syntactically-broken header should pass through. Otherwise an
    operator who briefly mis-rotates the bearer token would also lose
    their liveness probe and cascade-fail the deployment.
    """
    response = client.get(HEALTH_PATH, headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_path_is_hidden_from_openapi_schema(app: FastAPI) -> None:
    """The probe is intentionally excluded from ``/docs`` (operator concern, not API surface)."""
    schema = app.openapi()
    # Type narrowing: openapi() returns dict[str, Any]; the "paths" key
    # is always present on a non-empty FastAPI app.
    paths = cast(dict[str, Any], schema.get("paths", {}))
    assert HEALTH_PATH not in paths


# ---------------------------------------------------------------------------
# /health — failure paths
# ---------------------------------------------------------------------------


def test_health_returns_503_when_acquire_raises() -> None:
    """A pool that can't hand out connections is unhealthy by definition."""
    pool = _FakePool(raise_on_acquire=ConnectionError("postgres unreachable"))
    app = create_app(
        _as_pool(pool),
        worker_token="w",
        bootstrap_token="b",
    )
    with TestClient(app) as client:
        response = client.get(HEALTH_PATH)
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    # The detail string must surface the underlying error so the
    # operator sees "postgres unreachable" in their probe-failure logs
    # instead of a generic "service unavailable" placeholder.
    assert "postgres unreachable" in body["detail"]


def test_health_returns_503_when_fetchval_raises() -> None:
    """A connection that can't run ``SELECT 1`` is unhealthy too.

    Distinct from acquire-failure because the failure mode is different
    (the pool is alive but the DB is broken, e.g. recovery mode).
    """
    pool = _FakePool(raise_on_fetchval=RuntimeError("query timeout"))
    app = create_app(
        _as_pool(pool),
        worker_token="w",
        bootstrap_token="b",
    )
    with TestClient(app) as client:
        response = client.get(HEALTH_PATH)
    assert response.status_code == 503
    assert "query timeout" in response.json()["detail"]


def test_health_returns_503_when_select_1_returns_unexpected_value() -> None:
    """Defensive: a proxy / mocked pool returning the wrong scalar must fail closed."""
    pool = _FakePool(fetchval_value=0)
    app = create_app(
        _as_pool(pool),
        worker_token="w",
        bootstrap_token="b",
    )
    with TestClient(app) as client:
        response = client.get(HEALTH_PATH)
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "unexpected SELECT 1 result" in body["detail"]


# ---------------------------------------------------------------------------
# OpenAPI / Swagger UI
# ---------------------------------------------------------------------------


def test_openapi_json_is_reachable_without_auth(client: TestClient) -> None:
    """The OpenAPI spec is on the FastAPI default path and unauthenticated."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Whilly Control Plane"
    # Sanity: it's the right shape — has ``paths`` and ``info``. We don't
    # assert on specific paths because /workers/* and /tasks/* arrive in
    # TASK-021b/c and we don't want this test to require updating then.
    assert "info" in schema and "paths" in schema


def test_swagger_ui_is_reachable_without_auth(client: TestClient) -> None:
    """``/docs`` (Swagger UI) is on the FastAPI default — operators expect it there."""
    response = client.get("/docs")
    assert response.status_code == 200
    # Swagger UI is HTML; just verify a marker element is present
    # rather than parsing — FastAPI controls the exact template and we
    # don't want to couple this test to its internals.
    assert "swagger-ui" in response.text.lower()


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def test_create_app_uses_explicit_tokens_over_env(
    monkeypatch: pytest.MonkeyPatch,
    healthy_pool: _FakePool,
) -> None:
    """Kwargs win over env so tests don't have to mutate ``os.environ``."""
    # Set env to something different from what we pass — the kwargs must
    # be the ones that bind. We can't directly observe the bound token
    # from here (it's captured by the auth closure on app.state), but a
    # 401 path test in TASK-021b proves it end-to-end. Here we just
    # assert ``create_app`` doesn't raise when given valid kwargs even
    # if env is empty.
    monkeypatch.delenv(WORKER_TOKEN_ENV, raising=False)
    monkeypatch.delenv(BOOTSTRAP_TOKEN_ENV, raising=False)
    # Should not raise — kwargs supply the tokens.
    app = create_app(
        _as_pool(healthy_pool),
        worker_token="wt",
        bootstrap_token="bt",
    )
    assert isinstance(app, FastAPI)


def test_create_app_falls_back_to_env_when_kwargs_omitted(
    monkeypatch: pytest.MonkeyPatch,
    healthy_pool: _FakePool,
) -> None:
    """Missing kwargs read the configured env vars (production deployment shape)."""
    monkeypatch.setenv(WORKER_TOKEN_ENV, "from-env-w")
    monkeypatch.setenv(BOOTSTRAP_TOKEN_ENV, "from-env-b")
    # Should not raise — env supplies the tokens.
    app = create_app(_as_pool(healthy_pool))
    assert isinstance(app, FastAPI)


def test_create_app_accepts_missing_worker_token(
    monkeypatch: pytest.MonkeyPatch,
    healthy_pool: _FakePool,
) -> None:
    """Per TASK-101, ``WHILLY_WORKER_TOKEN`` is optional.

    The steady-state RPC surface validates per-worker bearers against
    ``workers.token_hash`` (TASK-101 / PRD FR-1.2). The legacy shared
    bearer is opt-in via the env var and emits a one-shot deprecation
    warning when it fires; without it the dep is purely DB-backed.
    Therefore ``create_app`` must NOT raise when the env var is unset,
    even though it still requires the bootstrap secret.
    """
    monkeypatch.delenv(WORKER_TOKEN_ENV, raising=False)
    monkeypatch.setenv(BOOTSTRAP_TOKEN_ENV, "b")
    app = create_app(_as_pool(healthy_pool))
    assert isinstance(app, FastAPI)


def test_create_app_accepts_missing_bootstrap_token_env(
    monkeypatch: pytest.MonkeyPatch,
    healthy_pool: _FakePool,
) -> None:
    """M2: ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` is optional now that the
    bootstrap dep consults the per-operator ``bootstrap_tokens`` table
    (migration 009). The env var only acts as a one-minor-version
    legacy fallback that emits a deprecation warning when its path is
    taken — without the env, ``create_app`` still succeeds and the
    DB-backed lookup is the sole authority.
    """
    monkeypatch.setenv(WORKER_TOKEN_ENV, "w")
    monkeypatch.delenv(BOOTSTRAP_TOKEN_ENV, raising=False)
    app = create_app(_as_pool(healthy_pool))
    assert isinstance(app, FastAPI)


def test_create_app_rejects_explicit_blank_worker_token(healthy_pool: _FakePool) -> None:
    """An explicit blank string is still a misconfiguration even though the
    legacy bearer is now optional (TASK-101).

    The intent for "disable the legacy fallback" is expressed by passing
    ``None`` (or omitting the kwarg + leaving the env unset). An
    explicit ``"   "`` is far more likely to be ``.env`` line noise
    than a deliberate "disable auth" toggle, and we surface it loudly
    so the operator can fix the misconfiguration rather than silently
    accept it.
    """
    with pytest.raises(RuntimeError) as excinfo:
        create_app(
            _as_pool(healthy_pool),
            worker_token="   ",
            bootstrap_token="b",
        )
    # The error message should mention the env var name so the operator
    # knows which slot they actually mis-supplied.
    assert WORKER_TOKEN_ENV in str(excinfo.value)


def test_create_app_rejects_explicit_blank_bootstrap_token(healthy_pool: _FakePool) -> None:
    """Symmetric to the per-worker case (PRD FR-1.2)."""
    with pytest.raises(RuntimeError) as excinfo:
        create_app(
            _as_pool(healthy_pool),
            worker_token="w",
            bootstrap_token="",
        )
    assert BOOTSTRAP_TOKEN_ENV in str(excinfo.value)


# ---------------------------------------------------------------------------
# Claim long-poll knobs — fail fast at construction time on bad values
# ---------------------------------------------------------------------------


def test_create_app_rejects_zero_claim_poll_interval(healthy_pool: _FakePool) -> None:
    """A zero interval would tight-loop the database — fail fast at construction time.

    ``asyncio.sleep(0)`` is not a guaranteed yield in every event-
    loop implementation, so a zero interval would also defeat the
    cancellation contract the route relies on. We surface this at
    create_app time (loud, immediate) rather than letting it manifest
    as runaway DB load (silent, disastrous).
    """
    with pytest.raises(ValueError) as excinfo:
        create_app(
            _as_pool(healthy_pool),
            worker_token="w",
            bootstrap_token="b",
            claim_poll_interval=0.0,
        )
    assert "claim_poll_interval" in str(excinfo.value)


def test_create_app_rejects_negative_claim_poll_interval(healthy_pool: _FakePool) -> None:
    """Negative interval — same reasoning as the zero case."""
    with pytest.raises(ValueError):
        create_app(
            _as_pool(healthy_pool),
            worker_token="w",
            bootstrap_token="b",
            claim_poll_interval=-0.5,
        )


def test_create_app_rejects_negative_claim_long_poll_timeout(healthy_pool: _FakePool) -> None:
    """A negative budget makes the loop nonsensical (deadline is in the past)."""
    with pytest.raises(ValueError) as excinfo:
        create_app(
            _as_pool(healthy_pool),
            worker_token="w",
            bootstrap_token="b",
            claim_long_poll_timeout=-1.0,
        )
    assert "claim_long_poll_timeout" in str(excinfo.value)


def test_create_app_accepts_zero_claim_long_poll_timeout(healthy_pool: _FakePool) -> None:
    """A zero budget is legal — degenerate case where the loop polls exactly once.

    Useful for tests that want the immediate-204-on-empty-queue
    behaviour without burning real wall-clock time. ``create_app``
    must not reject this; only the strictly-negative case is invalid.
    """
    app = create_app(
        _as_pool(healthy_pool),
        worker_token="w",
        bootstrap_token="b",
        claim_long_poll_timeout=0.0,
    )
    assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# Lifespan / app.state wiring
# ---------------------------------------------------------------------------


def test_lifespan_attaches_pool_and_auth_deps_to_app_state(
    healthy_pool: _FakePool,
) -> None:
    """Handlers added in TASK-021b/c reach the pool and auth via ``request.app.state``.

    We register a tiny inspector route on the app, drive a request
    through it under the lifespan, and read the captured state. This
    pins the public lifecycle contract: when ``with TestClient(app)``
    is open, ``app.state.pool`` / ``.bearer_dep`` / ``.bootstrap_dep``
    are the values passed to (or derived inside) ``create_app``.
    """
    pool = healthy_pool
    app = create_app(
        _as_pool(pool),
        worker_token="w",
        bootstrap_token="b",
    )

    captured: dict[str, Any] = {}

    @app.get("/__inspect_state", include_in_schema=False)
    async def inspect() -> dict[str, str]:
        captured["pool_is_same"] = str(app.state.pool is _as_pool(pool))
        captured["has_bearer_dep"] = str(app.state.bearer_dep is not None)
        captured["has_bootstrap_dep"] = str(app.state.bootstrap_dep is not None)
        captured["has_admin_dep"] = str(app.state.admin_dep is not None)
        return {"ok": "true"}

    with TestClient(app) as client:
        response = client.get("/__inspect_state")
    assert response.status_code == 200
    assert captured["pool_is_same"] == "True"
    assert captured["has_bearer_dep"] == "True"
    assert captured["has_bootstrap_dep"] == "True"
    assert captured["has_admin_dep"] == "True"


def test_lifespan_clears_pool_reference_on_shutdown(healthy_pool: _FakePool) -> None:
    """After the lifespan exits, ``app.state.pool`` is cleared.

    This is what lets the caller close the pool without leaving a
    dangling reference on a long-lived ``app`` (e.g. test harnesses
    that rebuild the app between cases but reuse the pool).
    """
    app = create_app(
        _as_pool(healthy_pool),
        worker_token="w",
        bootstrap_token="b",
    )
    with TestClient(app):
        # During the lifespan, pool is set.
        assert app.state.pool is _as_pool(healthy_pool)
    # After the ``with`` block exits, lifespan finally-clause has run.
    assert app.state.pool is None


# ---------------------------------------------------------------------------
# Smoke — the OpenAPI spec is JSON-decodable end-to-end
# ---------------------------------------------------------------------------


def test_openapi_response_is_json_serialisable(client: TestClient) -> None:
    """Sanity: the spec round-trips through json.dumps (catches non-serialisable defaults)."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    # response.json() already parsed it; round-tripping through
    # json.dumps catches FastAPI accidentally embedding objects that
    # are JSON-decodable in the response but not re-encodable.
    json.dumps(response.json())
