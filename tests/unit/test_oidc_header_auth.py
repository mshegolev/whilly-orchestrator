"""Unit tests for E17 reverse-proxy header-trust auth (``whilly/api/oidc_header_auth.py``).

Pins the security invariants from the design doc
(``.planning/E15-E17-auth-security-design.md`` §3.2):

1. **Fail-closed** — enabled with an empty/invalid allowlist raises at config time.
2. **Peer IP only** — ``X-Forwarded-For`` is never trusted; only the direct peer.
3. **Transient** — the principal is attached to ``request.state``, no DB session.
4. **Audited** — trusted-peer header requests are recorded (``ok`` / ``missing_user``).
5. **Default off** — the header is ignored entirely when the flag is unset/0.
"""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from whilly.api import auth_audit_repo, users_repo
from whilly.api.oidc_header_auth import (
    TRUST_PROXY_AUTH_ENV,
    TRUSTED_PROXY_IPS_ENV,
    ProxyHeaderAuthConfig,
    ProxyHeaderAuthMiddleware,
)

# ─── Gate 1 + 5: config resolution / fail-closed (pure, no DB) ───────────────


def test_config_disabled_when_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TRUST_PROXY_AUTH_ENV, raising=False)
    cfg = ProxyHeaderAuthConfig.from_env()
    assert cfg.enabled is False
    assert cfg.networks == ()


def test_config_disabled_when_flag_zero_ignores_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRUST_PROXY_AUTH_ENV, "0")
    monkeypatch.setenv(TRUSTED_PROXY_IPS_ENV, "10.0.0.0/8")
    assert ProxyHeaderAuthConfig.from_env().enabled is False


def test_config_enabled_parses_cidr_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRUST_PROXY_AUTH_ENV, "1")
    monkeypatch.setenv(TRUSTED_PROXY_IPS_ENV, "10.0.0.0/24, 127.0.0.1/32")
    cfg = ProxyHeaderAuthConfig.from_env()
    assert cfg.enabled is True
    assert len(cfg.networks) == 2


def test_config_fail_closed_on_empty_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRUST_PROXY_AUTH_ENV, "1")
    monkeypatch.setenv(TRUSTED_PROXY_IPS_ENV, "   ")
    with pytest.raises(RuntimeError, match="empty"):
        ProxyHeaderAuthConfig.from_env()


def test_config_fail_closed_on_missing_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRUST_PROXY_AUTH_ENV, "1")
    monkeypatch.delenv(TRUSTED_PROXY_IPS_ENV, raising=False)
    with pytest.raises(RuntimeError, match="empty"):
        ProxyHeaderAuthConfig.from_env()


def test_config_fail_closed_on_invalid_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRUST_PROXY_AUTH_ENV, "1")
    monkeypatch.setenv(TRUSTED_PROXY_IPS_ENV, "10.0.0.0/24,not-an-ip")
    with pytest.raises(RuntimeError, match="invalid"):
        ProxyHeaderAuthConfig.from_env()


# ─── Gate 2: peer-IP allowlist semantics ─────────────────────────────────────


def test_peer_is_trusted_matches_only_allowlist() -> None:
    cfg = ProxyHeaderAuthConfig(enabled=True, networks=(ipaddress.ip_network("10.0.0.0/24"),))
    assert cfg.peer_is_trusted("10.0.0.5") is True
    assert cfg.peer_is_trusted("10.0.1.5") is False
    assert cfg.peer_is_trusted(None) is False
    assert cfg.peer_is_trusted("not-an-ip") is False


def test_middleware_refuses_disabled_config() -> None:
    async def _app(scope: object, receive: object, send: object) -> None:  # pragma: no cover - never called
        return None

    with pytest.raises(RuntimeError, match="disabled config"):
        ProxyHeaderAuthMiddleware(_app, pool=object(), config=ProxyHeaderAuthConfig(enabled=False))


# ─── Middleware behaviour (gates 2, 3, 4) ────────────────────────────────────

_TRUSTED_CFG = ProxyHeaderAuthConfig(enabled=True, networks=(ipaddress.ip_network("10.0.0.0/24"),))


def _build_app(cfg: ProxyHeaderAuthConfig) -> Starlette:
    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"principal": getattr(request.state, "proxy_principal", None)})

    app = Starlette(routes=[Route("/whoami", whoami)])
    # pool is a sentinel — the repo calls are monkeypatched in each test.
    app.add_middleware(ProxyHeaderAuthMiddleware, pool=object(), config=cfg)
    return app


async def _get(app: Starlette, *, peer: str, headers: dict[str, str] | None = None) -> object:
    transport = ASGITransport(app=app, client=(peer, 41234))
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        return await ac.get("/whoami", headers=headers or {})


def _patch_user(monkeypatch: pytest.MonkeyPatch, user: object) -> list[dict[str, object]]:
    async def _fake_get_user(pool: object, *, username: str) -> object:
        if user is None:
            return None
        return user

    recorded: list[dict[str, object]] = []

    async def _fake_audit(pool: object, **kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(users_repo, "get_user_by_username", _fake_get_user)
    monkeypatch.setattr(auth_audit_repo, "insert_attempt", _fake_audit)
    return recorded


async def test_trusted_peer_existing_user_sets_transient_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(username="alice", email="alice@example.test", role="admin")
    recorded = _patch_user(monkeypatch, user)
    resp = await _get(_build_app(_TRUSTED_CFG), peer="10.0.0.7", headers={"X-Forwarded-User": "Alice"})

    assert resp.status_code == 200
    principal = resp.json()["principal"]
    assert principal is not None
    assert principal["email"] == "alice@example.test"
    assert principal["session_id"] == "proxy:alice"
    # Gate 4: audited as a successful proxy login, with the real peer IP.
    assert recorded and recorded[-1]["outcome"] == "ok"
    assert recorded[-1]["ip"] == "10.0.0.7"


async def test_untrusted_peer_header_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(username="alice", email="alice@example.test", role="admin")
    recorded = _patch_user(monkeypatch, user)
    resp = await _get(_build_app(_TRUSTED_CFG), peer="192.168.1.5", headers={"X-Forwarded-User": "alice"})

    # Gate 2: untrusted peer ⇒ header ignored entirely, no lookup, no audit.
    assert resp.json()["principal"] is None
    assert recorded == []


async def test_x_forwarded_for_spoof_does_not_widen_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(username="alice", email="alice@example.test", role="admin")
    recorded = _patch_user(monkeypatch, user)
    # The attacker is on an untrusted peer but forges X-Forwarded-For to a
    # trusted IP. We must NOT trust it — only the direct peer counts.
    resp = await _get(
        _build_app(_TRUSTED_CFG),
        peer="203.0.113.9",
        headers={"X-Forwarded-User": "alice", "X-Forwarded-For": "10.0.0.7"},
    )
    assert resp.json()["principal"] is None
    assert recorded == []


async def test_trusted_peer_unknown_user_audited_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_user(monkeypatch, None)
    resp = await _get(_build_app(_TRUSTED_CFG), peer="10.0.0.7", headers={"X-Forwarded-User": "ghost"})

    assert resp.json()["principal"] is None
    assert recorded and recorded[-1]["outcome"] == "missing_user"
    assert recorded[-1]["username"] == "ghost"


async def test_trusted_peer_no_header_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_user(monkeypatch, SimpleNamespace(username="alice", email=None, role="admin"))
    resp = await _get(_build_app(_TRUSTED_CFG), peer="10.0.0.7")

    assert resp.json()["principal"] is None
    assert recorded == []


async def test_email_falls_back_to_username_when_user_has_no_email(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(username="svc", email=None, role="admin")
    _patch_user(monkeypatch, user)
    resp = await _get(_build_app(_TRUSTED_CFG), peer="10.0.0.1", headers={"X-Forwarded-User": "svc"})
    assert resp.json()["principal"]["email"] == "svc"


# ─── Gate 3: _authenticate_session honours the transient principal ───────────


async def test_authenticate_session_honours_proxy_principal() -> None:
    from whilly.api.auth_routes import _authenticate_session

    principal = {"email": "alice@example.test", "session_id": "proxy:alice", "expires_at_unix": 123}
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    request.state.proxy_principal = principal

    # pool / secret / cookie_name are unused on the proxy path — it returns
    # before touching the cookie. Passing sentinels proves that.
    result = await _authenticate_session(request, pool=object(), secret=b"x", cookie_name="whilly_session")
    assert result == principal
