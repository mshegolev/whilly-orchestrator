"""Unit tests for the WebAuthn surface (PRD §Epic E Item 15 / E15).

No DB and no real authenticator: ``build_webauthn_router(pool=None, ...)`` plus
mocked repos/sessions, exactly like ``tests/unit/test_totp_routes.py``. Option
generation uses the real ``webauthn`` library (it works offline); only the two
``verify_*`` calls are patched — and the patches capture the kwargs so we can
assert the security anchors (``expected_origin`` / ``expected_rp_id`` /
``expected_challenge`` / ``credential_current_sign_count``) come from server
config and the redeemed cookie, never from a request header.
"""

from __future__ import annotations

import datetime
import types
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest

# webauthn ships in the [webauthn] optional extra (pulled into [dev]).
webauthn = pytest.importorskip("webauthn")

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from webauthn.helpers.exceptions import InvalidAuthenticationResponse  # noqa: E402

from whilly.api import auth_tokens, sessions, users_repo, webauthn_repo  # noqa: E402
from whilly.api.csrf import COOKIE_NAME  # noqa: E402
from whilly.api.second_factor import PENDING_COOKIE_NAME, PENDING_MAX_ATTEMPTS, _mint_pending_cookie  # noqa: E402
from whilly.api.webauthn_routes import (  # noqa: E402
    PUBLIC_ORIGIN_ENV,
    REG_COOKIE_NAME,
    WEBAUTHN_ENABLED_ENV,
    WebAuthnConfig,
    _b64url_decode,
    _b64url_encode,
    build_webauthn_router,
    webauthn_enabled,
)

_TEST_SECRET: bytes = b"e15-test-secret-32-bytes-padxxxx"
_USERNAME: str = "alice"
_EMAIL: str = f"{_USERNAME}@local"
_SESSION_ID: str = "00000000-1111-2222-3333-444444444444"
_ORIGIN: str = "https://whilly.test"
_RP_ID: str = "whilly.test"
_CHALLENGE: bytes = b"challenge-bytes-exactly-32-bytes"
_CRED_ID: bytes = b"\x01\x02\x03credential-id"


@pytest.fixture(autouse=True)
def _origin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, _ORIGIN)
    monkeypatch.delenv(WEBAUTHN_ENABLED_ENV, raising=False)
    monkeypatch.delenv("WHILLY_WEBAUTHN_RP_ID", raising=False)
    yield


def _user(role: str = "admin") -> users_repo.User:
    return users_repo.User(
        username=_USERNAME,
        email=None,
        role=role,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=False,
    )


def _session() -> object:
    return types.SimpleNamespace(
        session_id=_SESSION_ID,
        email=_EMAIL,
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    )


def _stored_cred(sign_count: int = 2) -> webauthn_repo.WebAuthnCredential:
    return webauthn_repo.WebAuthnCredential(
        username=_USERNAME,
        credential_id=_CRED_ID,
        public_key=b"\xa5cose",
        sign_count=sign_count,
        transports=["internal"],
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_used_at=None,
    )


def _session_cookie() -> str:
    return auth_tokens.mint_session_cookie_value(_TEST_SECRET, session_id=_SESSION_ID, email=_EMAIL, ttl_seconds=3600)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(build_webauthn_router(pool=None, secret=_TEST_SECRET, cookie_secure=False))  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=_ORIGIN) as ac:
        yield ac


# ── config: fail-closed origin (security gate #2/#3) ───────────────────────


def test_config_fail_closed_on_empty_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    with pytest.raises(RuntimeError):
        WebAuthnConfig.from_env()


def test_config_fail_closed_on_invalid_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, "not-a-url")
    with pytest.raises(RuntimeError):
        WebAuthnConfig.from_env()


def test_config_derives_rp_id_from_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, "https://whilly.test:8443/")
    cfg = WebAuthnConfig.from_env()
    assert cfg.rp_id == "whilly.test"
    assert cfg.expected_origin == "https://whilly.test:8443"


def test_webauthn_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert webauthn_enabled() is False
    monkeypatch.setenv(WEBAUTHN_ENABLED_ENV, "1")
    assert webauthn_enabled() is True
    monkeypatch.setenv(WEBAUTHN_ENABLED_ENV, "off")
    assert webauthn_enabled() is False


# ── admin gate on enrolment ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_form_requires_session(client: AsyncClient) -> None:
    resp = await client.get("/me/webauthn")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_register_form_rejects_non_admin(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user(role="operator")))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    resp = await client.get("/me/webauthn")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_register_form_admin_ok(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", AsyncMock(return_value=[]))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    resp = await client.get("/me/webauthn")
    assert resp.status_code == 200
    assert "Register a passkey" in resp.text


# ── registration ceremony ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_begin_returns_options_and_sets_reg_cookie(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", AsyncMock(return_value=[]))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    resp = await client.post("/me/webauthn/register/begin")
    assert resp.status_code == 200
    body = resp.json()
    assert "challenge" in body and body["rp"]["id"] == _RP_ID
    assert any(REG_COOKIE_NAME in v for v in resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_register_finish_without_cookie_400(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    resp = await client.post("/me/webauthn/register/finish", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_register_finish_success_inserts_credential(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    insert_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(webauthn_repo, "insert_credential", insert_mock)
    captured: dict = {}

    def _fake_verify_reg(**kwargs: object) -> object:
        captured.update(kwargs)
        return types.SimpleNamespace(credential_id=_CRED_ID, credential_public_key=b"\xa5cose", sign_count=0)

    monkeypatch.setattr(webauthn, "verify_registration_response", _fake_verify_reg)
    reg_cookie = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, challenge=_b64url_encode(_CHALLENGE))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    client.cookies.set(REG_COOKIE_NAME, reg_cookie)
    resp = await client.post("/me/webauthn/register/finish", json={"id": "x", "response": {"transports": ["usb"]}})
    assert resp.status_code == 200 and resp.json()["verified"] is True
    # Origin / RP-ID come from server config; challenge from the signed cookie.
    assert captured["expected_origin"] == _ORIGIN
    assert captured["expected_rp_id"] == _RP_ID
    assert captured["expected_challenge"] == _CHALLENGE
    assert insert_mock.await_args.kwargs["credential_id"] == _CRED_ID
    assert insert_mock.await_args.kwargs["transports"] == ["usb"]


@pytest.mark.asyncio
async def test_register_finish_wrong_user_cookie_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # A reg cookie bound to a DIFFERENT user than the authenticated admin must
    # be refused (cannot enroll a key for someone else — security gate #4).
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    other = _mint_pending_cookie(_TEST_SECRET, username="mallory", challenge=_b64url_encode(_CHALLENGE))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    client.cookies.set(REG_COOKIE_NAME, other)
    resp = await client.post("/me/webauthn/register/finish", json={})
    assert resp.status_code == 400


# ── second-factor assertion ceremony ────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_begin_without_pending_401(client: AsyncClient) -> None:
    resp = await client.post("/auth/webauthn/begin")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_begin_rebinds_fresh_challenge(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", AsyncMock(return_value=[_stored_cred()]))
    client.cookies.set(PENDING_COOKIE_NAME, _mint_pending_cookie(_TEST_SECRET, username=_USERNAME))
    resp = await client.post("/auth/webauthn/begin")
    assert resp.status_code == 200
    options = resp.json()
    assert options["rpId"] == _RP_ID
    # The re-minted pending cookie must now carry a challenge (gate #1).
    from whilly.api.second_factor import _verify_pending_cookie

    new_pending = next(
        v.split(f"{PENDING_COOKIE_NAME}=")[1].split(";")[0]
        for v in resp.headers.get_list("set-cookie")
        if PENDING_COOKIE_NAME in v
    )
    payload = _verify_pending_cookie(_TEST_SECRET, new_pending)
    assert payload is not None and isinstance(payload.get("c"), str)


@pytest.mark.asyncio
async def test_auth_verify_success_mints_session(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webauthn_repo, "get_credential_by_id", AsyncMock(return_value=_stored_cred(sign_count=2)))
    bump_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(webauthn_repo, "bump_sign_count", bump_mock)
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    monkeypatch.setattr(users_repo, "update_last_login", AsyncMock(return_value=None))
    monkeypatch.setattr(sessions, "create_session", AsyncMock(return_value=_session()))
    captured: dict = {}

    def _fake_verify_auth(**kwargs: object) -> object:
        captured.update(kwargs)
        return types.SimpleNamespace(new_sign_count=9)

    monkeypatch.setattr(webauthn, "verify_authentication_response", _fake_verify_auth)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, challenge=_b64url_encode(_CHALLENGE))
    client.cookies.set(PENDING_COOKIE_NAME, pending)
    resp = await client.post(
        "/auth/webauthn/verify", json={"id": "x", "rawId": _b64url_encode(_CRED_ID), "response": {}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True and body["redirect"] == "/"
    # Session cookie minted; pending cleared.
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("whilly_session=" in v for v in set_cookies)
    assert any(PENDING_COOKIE_NAME in v and "Max-Age=0" in v for v in set_cookies)
    # Security anchors: from config + cookie + stored counter (gates #1/#2/#3).
    assert captured["expected_origin"] == _ORIGIN
    assert captured["expected_rp_id"] == _RP_ID
    assert captured["expected_challenge"] == _CHALLENGE
    assert captured["credential_current_sign_count"] == 2
    assert bump_mock.await_args.kwargs["new_sign_count"] == 9


@pytest.mark.asyncio
async def test_auth_verify_invalid_increments_attempts(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webauthn_repo, "get_credential_by_id", AsyncMock(return_value=_stored_cred()))

    def _raise(**kwargs: object) -> object:
        raise InvalidAuthenticationResponse("wrong origin / replay / sign-count regression")

    monkeypatch.setattr(webauthn, "verify_authentication_response", _raise)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, challenge=_b64url_encode(_CHALLENGE))
    client.cookies.set(PENDING_COOKIE_NAME, pending)
    resp = await client.post("/auth/webauthn/verify", json={"rawId": _b64url_encode(_CRED_ID), "response": {}})
    assert resp.status_code == 401
    assert resp.json()["remaining"] == PENDING_MAX_ATTEMPTS - 1
    assert any(PENDING_COOKIE_NAME in v for v in resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_auth_verify_lockout_after_max_attempts(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webauthn_repo, "get_credential_by_id", AsyncMock(return_value=_stored_cred()))

    def _raise(**kwargs: object) -> object:
        raise InvalidAuthenticationResponse("nope")

    monkeypatch.setattr(webauthn, "verify_authentication_response", _raise)
    pending = _mint_pending_cookie(
        _TEST_SECRET, username=_USERNAME, attempts=PENDING_MAX_ATTEMPTS - 1, challenge=_b64url_encode(_CHALLENGE)
    )
    client.cookies.set(PENDING_COOKIE_NAME, pending)
    resp = await client.post("/auth/webauthn/verify", json={"rawId": _b64url_encode(_CRED_ID), "response": {}})
    assert resp.status_code == 429
    assert any(PENDING_COOKIE_NAME in v and "Max-Age=0" in v for v in resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_auth_verify_unknown_credential_fails(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Credential not in the store → verify is never called, failure path runs.
    monkeypatch.setattr(webauthn_repo, "get_credential_by_id", AsyncMock(return_value=None))
    called = {"n": 0}

    def _verify(**kwargs: object) -> object:
        called["n"] += 1
        return types.SimpleNamespace(new_sign_count=1)

    monkeypatch.setattr(webauthn, "verify_authentication_response", _verify)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, challenge=_b64url_encode(_CHALLENGE))
    client.cookies.set(PENDING_COOKIE_NAME, pending)
    resp = await client.post("/auth/webauthn/verify", json={"rawId": _b64url_encode(b"unknown"), "response": {}})
    assert resp.status_code == 401
    assert called["n"] == 0


# ── chooser ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_choose_factor_without_pending_redirects_login(client: AsyncClient) -> None:
    resp = await client.get("/auth/2fa", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_choose_factor_renders_both_options(client: AsyncClient) -> None:
    client.cookies.set(PENDING_COOKIE_NAME, _mint_pending_cookie(_TEST_SECRET, username=_USERNAME))
    resp = await client.get("/auth/2fa")
    assert resp.status_code == 200
    assert "/auth/webauthn" in resp.text and "/auth/totp" in resp.text


# silence unused-import lint for the round-trip decode helper
_ = _b64url_decode
