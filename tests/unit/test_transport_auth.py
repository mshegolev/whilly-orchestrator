"""Unit tests for :mod:`whilly.adapters.transport.auth` (TASK-021a2, PRD FR-1.2 / TC-6).

This module is the edge of authentication for the worker ↔ control-plane
HTTP API: every worker-facing route depends on it, so its correctness is
load-bearing. These tests exercise the AC for TASK-021a2:

* :func:`make_bearer_auth` rejects missing / malformed / wrong-token
  Authorization headers with HTTP 401 and the RFC 6750
  ``WWW-Authenticate: Bearer`` header;
* :func:`make_bootstrap_auth` is mechanically identical but bound to a
  *different* secret — i.e. a valid per-worker token does not let the
  caller register, and a valid bootstrap token does not let the caller
  claim a task. This is the whole point of the split (TASK-021b).
* the lazy module-level :func:`bearer_auth` / :func:`bootstrap_auth`
  shims read the right env var on first use, raise ``RuntimeError`` if
  the env var is missing, and stay bound across calls (the binding is
  cached, not re-read every request);
* the bearer extraction is case-insensitive on the *scheme* but
  case-sensitive on the *token* (RFC 7235 §2.1 / RFC 6750 §2.1);
* end-to-end: a real :class:`fastapi.FastAPI` app with a route guarded
  by ``Depends(make_bearer_auth(...))`` returns 401 / 200 as expected
  through :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from whilly.adapters.transport import auth as auth_module
from whilly.adapters.transport.auth import (
    BOOTSTRAP_TOKEN_ENV,
    WORKER_TOKEN_ENV,
    bearer_auth,
    bootstrap_auth,
    make_bearer_auth,
    make_bootstrap_auth,
    reset_lazy_dependencies,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lazy_state() -> Iterator[None]:
    """Clear cached lazy bindings between tests.

    The module-level :func:`bearer_auth` / :func:`bootstrap_auth` shims
    cache their token on first use (see module docstring). Without
    resetting between tests, a test that sets ``WHILLY_WORKER_TOKEN=A``
    would leak that binding into a later test that sets it to ``B``.
    """
    reset_lazy_dependencies()
    yield
    reset_lazy_dependencies()


@pytest.fixture
def patched_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Convenience handle for tests that mutate transport-auth env vars.

    Just returns the monkeypatch fixture itself — the alias makes
    intention clearer at the call site (``patched_env.setenv(...)``
    reads better than ``monkeypatch.setenv(...)`` when several env vars
    are touched in one test).
    """
    return monkeypatch


# ---------------------------------------------------------------------------
# make_bearer_auth — direct factory tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_auth_accepts_correct_token() -> None:
    dep = make_bearer_auth("s3cr3t")
    # Returns None on success — FastAPI dependencies that gate access
    # don't return a value, they just refrain from raising.
    result = await dep("Bearer s3cr3t")
    assert result is None


@pytest.mark.asyncio
async def test_bearer_auth_rejects_wrong_token() -> None:
    dep = make_bearer_auth("s3cr3t")
    with pytest.raises(HTTPException) as exc_info:
        await dep("Bearer wrong-token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid token"


@pytest.mark.asyncio
async def test_bearer_auth_rejects_missing_header() -> None:
    dep = make_bearer_auth("s3cr3t")
    with pytest.raises(HTTPException) as exc_info:
        await dep(None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing bearer token"


@pytest.mark.asyncio
async def test_bearer_auth_rejects_empty_token_after_prefix() -> None:
    """``Authorization: Bearer `` (whitespace only after prefix) → 401.

    Without the explicit empty-token check, ``compare_digest("", "")``
    would short-circuit ``True`` if both the client and server somehow
    ended up with empty strings — a misconfiguration we want to fail
    loudly on, not silently let through.
    """
    dep = make_bearer_auth("s3cr3t")
    with pytest.raises(HTTPException) as exc_info:
        await dep("Bearer    ")  # only whitespace after prefix
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "empty bearer token"


@pytest.mark.asyncio
async def test_bearer_auth_rejects_wrong_scheme() -> None:
    dep = make_bearer_auth("s3cr3t")
    with pytest.raises(HTTPException) as exc_info:
        await dep("Basic dXNlcjpwYXNz")  # base64 user:pass
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid authorization scheme"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
async def test_bearer_auth_scheme_is_case_insensitive(scheme: str) -> None:
    """RFC 7235 §2.1: ``auth-scheme`` is case-insensitive."""
    dep = make_bearer_auth("s3cr3t")
    await dep(f"{scheme} s3cr3t")  # must not raise


@pytest.mark.asyncio
async def test_bearer_auth_token_is_case_sensitive() -> None:
    """RFC 6750 §2.1: the token itself is opaque and case-sensitive."""
    dep = make_bearer_auth("MixedCaseToken")
    with pytest.raises(HTTPException) as exc_info:
        await dep("Bearer mixedcasetoken")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_bearer_auth_401_carries_www_authenticate_header() -> None:
    """RFC 6750 §3 requires ``WWW-Authenticate: Bearer`` on 401."""
    dep = make_bearer_auth("s3cr3t")
    with pytest.raises(HTTPException) as exc_info:
        await dep(None)
    headers = exc_info.value.headers or {}
    assert "WWW-Authenticate" in headers
    assert headers["WWW-Authenticate"].startswith("Bearer ")
    assert 'realm="whilly"' in headers["WWW-Authenticate"]


def test_make_bearer_auth_rejects_empty_expected_token() -> None:
    """Defence-in-depth: factory refuses to bind to an empty secret.

    Without this guard, ``make_bearer_auth("")`` plus a client sending
    ``Authorization: Bearer `` could conspire to authenticate as
    "anyone with an empty token" via a single ``compare_digest("", "")``
    short-circuit. We close that off by making the factory raise.
    """
    with pytest.raises(RuntimeError, match="non-empty"):
        make_bearer_auth("")


# ---------------------------------------------------------------------------
# make_bootstrap_auth — same shape, different secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_auth_accepts_correct_token() -> None:
    dep = make_bootstrap_auth("bootstrap-secret")
    await dep("Bearer bootstrap-secret")  # must not raise


@pytest.mark.asyncio
async def test_bootstrap_auth_rejects_wrong_token() -> None:
    dep = make_bootstrap_auth("bootstrap-secret")
    with pytest.raises(HTTPException) as exc_info:
        await dep("Bearer not-the-bootstrap-token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid bootstrap token"


@pytest.mark.asyncio
async def test_bootstrap_token_is_separate_from_worker_token() -> None:
    """A valid worker token does not authenticate the register endpoint.

    This is the whole point of having two factories with two secrets
    (PRD FR-1.2): if rotating the bootstrap secret didn't lock out new
    workers without invalidating already-issued bearer tokens, the
    split would have no operational value.
    """
    bearer = make_bearer_auth("worker-token")
    bootstrap = make_bootstrap_auth("bootstrap-token")

    # Worker token authenticates the bearer surface ...
    await bearer("Bearer worker-token")
    # ... but is rejected on the bootstrap surface.
    with pytest.raises(HTTPException) as exc_info:
        await bootstrap("Bearer worker-token")
    assert exc_info.value.status_code == 401


def test_make_bootstrap_auth_rejects_empty_expected_token() -> None:
    with pytest.raises(RuntimeError, match="non-empty"):
        make_bootstrap_auth("")


# ---------------------------------------------------------------------------
# Lazy module-level shims (env-driven)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_bearer_auth_reads_env_on_first_use(
    patched_env: pytest.MonkeyPatch,
) -> None:
    patched_env.setenv(WORKER_TOKEN_ENV, "from-env")
    await bearer_auth("Bearer from-env")  # must not raise

    with pytest.raises(HTTPException):
        await bearer_auth("Bearer wrong")


@pytest.mark.asyncio
async def test_lazy_bearer_auth_caches_binding(
    patched_env: pytest.MonkeyPatch,
) -> None:
    """Once bound, the lazy shim ignores subsequent env mutations.

    This is intentional (see module docstring): config drift mid-process
    should never silently change the auth surface. Operators rotate by
    restarting; tests rotate by calling :func:`reset_lazy_dependencies`.
    """
    patched_env.setenv(WORKER_TOKEN_ENV, "first")
    await bearer_auth("Bearer first")

    # Mutating the env after first use must not change the bound token.
    patched_env.setenv(WORKER_TOKEN_ENV, "second")
    with pytest.raises(HTTPException):
        await bearer_auth("Bearer second")
    # Old value still works because the binding is cached.
    await bearer_auth("Bearer first")


@pytest.mark.asyncio
async def test_lazy_bearer_auth_raises_on_missing_env(
    patched_env: pytest.MonkeyPatch,
) -> None:
    patched_env.delenv(WORKER_TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=WORKER_TOKEN_ENV):
        await bearer_auth("Bearer anything")


@pytest.mark.asyncio
async def test_lazy_bearer_auth_raises_on_empty_env(
    patched_env: pytest.MonkeyPatch,
) -> None:
    """Empty (whitespace-only) env value is treated as missing.

    There is no way to disable auth via env (PRD FR-1.2): an empty
    token is a misconfiguration, not a "disable auth" toggle.
    """
    patched_env.setenv(WORKER_TOKEN_ENV, "   ")
    with pytest.raises(RuntimeError, match=WORKER_TOKEN_ENV):
        await bearer_auth("Bearer anything")


@pytest.mark.asyncio
async def test_lazy_bootstrap_auth_uses_separate_env(
    patched_env: pytest.MonkeyPatch,
) -> None:
    patched_env.setenv(BOOTSTRAP_TOKEN_ENV, "boot")
    patched_env.setenv(WORKER_TOKEN_ENV, "wkr")

    await bootstrap_auth("Bearer boot")
    with pytest.raises(HTTPException):
        # Worker token is not the bootstrap secret.
        await bootstrap_auth("Bearer wkr")


def test_reset_lazy_dependencies_clears_cache(
    patched_env: pytest.MonkeyPatch,
) -> None:
    """Test-only API: re-binding requires an explicit reset."""
    # Sanity: shims are None by default.
    assert auth_module._lazy_bearer is None
    assert auth_module._lazy_bootstrap is None

    # Force-bind via direct factory call (bypassing the env path).
    auth_module._lazy_bearer = make_bearer_auth("a")
    auth_module._lazy_bootstrap = make_bootstrap_auth("b")
    assert auth_module._lazy_bearer is not None
    assert auth_module._lazy_bootstrap is not None

    reset_lazy_dependencies()
    assert auth_module._lazy_bearer is None
    assert auth_module._lazy_bootstrap is None


# ---------------------------------------------------------------------------
# End-to-end: FastAPI route guarded by Depends(make_bearer_auth(...))
# ---------------------------------------------------------------------------


def _build_app(token: str) -> FastAPI:
    """Spin up a minimal FastAPI app with one auth-gated route.

    Using a fresh app per test (rather than module-level) means the
    dependency closure is bound to the *test's* token, so cases that
    need different tokens don't fight over a shared `app` object.
    """
    app = FastAPI()
    bearer = make_bearer_auth(token)

    @app.get("/protected", dependencies=[Depends(bearer)])
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_e2e_protected_route_returns_200_with_correct_bearer() -> None:
    client = TestClient(_build_app("s3cr3t"))
    r = client.get("/protected", headers={"Authorization": "Bearer s3cr3t"})
    assert r.status_code == 200
    assert r.json() == {"ok": "yes"}


def test_e2e_protected_route_returns_401_without_header() -> None:
    client = TestClient(_build_app("s3cr3t"))
    r = client.get("/protected")
    assert r.status_code == 401
    # FastAPI surfaces ``WWW-Authenticate`` from the HTTPException headers
    # — the same RFC 6750-compliant value the helper builds.
    assert r.headers.get("WWW-Authenticate", "").startswith("Bearer ")
    assert r.json() == {"detail": "missing bearer token"}


def test_e2e_protected_route_returns_401_with_wrong_token() -> None:
    client = TestClient(_build_app("s3cr3t"))
    r = client.get("/protected", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid token"}


def test_e2e_protected_route_returns_401_with_wrong_scheme() -> None:
    client = TestClient(_build_app("s3cr3t"))
    r = client.get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid authorization scheme"}


# ---------------------------------------------------------------------------
# Type discipline — make sure the public AuthDependency alias is a callable
# ---------------------------------------------------------------------------


def test_dependency_factory_returns_callable() -> None:
    """The factory result must be a callable suitable for ``Depends(...)``.

    A subtle regression would be if a refactor returned a coroutine
    object instead of an async function — FastAPI would call it and
    get back a non-callable, then blow up at request time. ``cast``
    is just to satisfy mypy that the result is what we say it is.
    """
    dep = cast(object, make_bearer_auth("s3cr3t"))
    assert callable(dep)
