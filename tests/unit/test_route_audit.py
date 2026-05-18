"""Unit tests for the startup route audit (PRD-post-auth-hardening §Epic D, Item 13).

Pins three contracts:

1. ``audit_routes`` returns un-guarded paths (no side effects, no env var read).
2. ``enforce_audit`` raises ``RuntimeError`` when un-guarded routes exist AND
   ``WHILLY_ENABLE_ROUTE_AUDIT=1``.
3. ``enforce_audit`` is a no-op when neither env knob is set (default).

The intentional-unguarded-route fixture (a bare ``GET /secret``) is the
AC's canonical regression test — adding such a route to a production
deploy with the audit enabled must crash startup loudly.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from whilly.api.route_audit import (
    ENABLE_ENV,
    SKIP_ENV,
    audit_routes,
    enforce_audit,
)


def _bare_app_with_unguarded_route() -> FastAPI:
    app = FastAPI()

    @app.get("/secret")
    async def _secret() -> dict[str, str]:
        return {"secret": "leaked"}

    return app


def _bare_app_with_only_whitelisted_routes() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login")
    async def _login() -> dict[str, str]:
        return {"page": "login"}

    return app


@pytest.fixture(autouse=True)
def _isolate_audit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    monkeypatch.delenv(SKIP_ENV, raising=False)


def test_audit_routes_flags_bare_unguarded_route() -> None:
    """The pure inspection function returns the unguarded paths list."""
    app = _bare_app_with_unguarded_route()
    assert audit_routes(app) == ["/secret"]


def test_audit_routes_passes_whitelisted_paths() -> None:
    app = _bare_app_with_only_whitelisted_routes()
    assert audit_routes(app) == []


def test_enforce_audit_default_off_does_not_raise_on_bare_route() -> None:
    """No env vars → audit is a no-op even when unguarded routes exist."""
    app = _bare_app_with_unguarded_route()
    # No env vars set by the autouse fixture.
    enforce_audit(app)  # must not raise


def test_enforce_audit_enabled_raises_on_unguarded_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: with WHILLY_ENABLE_ROUTE_AUDIT=1 and a bare /secret route → RuntimeError."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    app = _bare_app_with_unguarded_route()
    with pytest.raises(RuntimeError, match=r"Unguarded route: '/secret'"):
        enforce_audit(app)


def test_enforce_audit_enabled_passes_when_only_whitelisted_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enable + no unguarded routes → no exception."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    app = _bare_app_with_only_whitelisted_routes()
    enforce_audit(app)


def test_skip_env_overrides_enable_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """WHILLY_SKIP_ROUTE_AUDIT=1 wins over WHILLY_ENABLE_ROUTE_AUDIT=1."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(SKIP_ENV, "1")
    app = _bare_app_with_unguarded_route()
    enforce_audit(app)  # must not raise


def test_enforce_audit_lists_multiple_unguarded_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple bare routes → error message names the first and counts the rest."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    app = FastAPI()

    @app.get("/secret-a")
    async def _a() -> dict[str, str]:
        return {}

    @app.get("/secret-b")
    async def _b() -> dict[str, str]:
        return {}

    with pytest.raises(RuntimeError) as exc_info:
        enforce_audit(app)
    assert "/secret-a" in str(exc_info.value)
    assert "1 others" in str(exc_info.value)


# ─── End-to-end: create_app boots clean with audit OFF (default) ────────────


def test_create_app_does_not_raise_with_audit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: create_app's startup path tolerates audit-off by default. This
    is the contract that defends against PRD R1 — the audit cannot break
    production startup unless an operator explicitly opts in.
    """
    # We don't actually create the full app (would need a real pool), but
    # we do verify that enforce_audit handles an app with the existing
    # whilly route shape — even with un-guarded inline-auth routes — without
    # raising when the env knob is off.
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    monkeypatch.delenv(SKIP_ENV, raising=False)
    # Build an app with a route that the existing code has (no Depends
    # auth, route body would call _authenticate_session inline).
    app = FastAPI()

    @app.get("/api/v1/plans")
    async def _plans() -> list[dict[str, str]]:
        return []

    enforce_audit(app)  # default off → no raise
