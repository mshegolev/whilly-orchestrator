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

from whilly.api import (  # noqa: E402
    auth_tokens,
    rate_limit,
    sessions,
    users_repo,
    webauthn_challenge_repo,
    webauthn_repo,
)
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
    # The auth ceremony now IP-rate-limits begin/verify and honours the shared
    # server-side lockout; neutralise both by default (tests override per case).
    monkeypatch.setattr(rate_limit, "allow", lambda key: True)
    monkeypatch.setattr(users_repo, "is_account_locked", AsyncMock(return_value=False))
    # The challenge now lives server-side (migration 027): begin mints an id,
    # verify/finish consume the challenge bytes. Default the store so the
    # ceremony tests stay deterministic; single-use tests override consume.
    monkeypatch.setattr(
        webauthn_challenge_repo, "create_challenge", AsyncMock(return_value="11111111-1111-1111-1111-111111111111")
    )
    monkeypatch.setattr(webauthn_challenge_repo, "consume_challenge", AsyncMock(return_value=_CHALLENGE))
    # Opaque per-user handle (Finding 3); default it so register_begin tests run.
    monkeypatch.setattr(webauthn_repo, "get_or_create_user_handle", AsyncMock(return_value=b"\x07" * 32))
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
async def test_auth_begin_rate_limited_429(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 1: begin is IP-rate-limited (caps a flood before the DB lookup)."""
    monkeypatch.setattr(rate_limit, "allow", lambda key: False)
    creds = AsyncMock(return_value=[_stored_cred()])
    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", creds)
    client.cookies.set(PENDING_COOKIE_NAME, _mint_pending_cookie(_TEST_SECRET, username=_USERNAME))
    resp = await client.post("/auth/webauthn/begin")
    assert resp.status_code == 429
    assert creds.await_count == 0


@pytest.mark.asyncio
async def test_auth_verify_blocked_when_account_locked(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify honours the shared server-side lockout (e.g. tripped by failed TOTP)
    even with a valid pending cookie + challenge — before any assertion check."""
    monkeypatch.setattr(users_repo, "is_account_locked", AsyncMock(return_value=True))
    get_cred = AsyncMock(return_value=_stored_cred())
    monkeypatch.setattr(webauthn_repo, "get_credential_by_id", get_cred)
    create = AsyncMock(return_value=_session())
    monkeypatch.setattr(sessions, "create_session", create)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, challenge=_b64url_encode(_CHALLENGE))
    client.cookies.set(PENDING_COOKIE_NAME, pending)
    resp = await client.post("/auth/webauthn/verify", json={"rawId": _b64url_encode(_CRED_ID), "response": {}})
    assert resp.status_code == 429
    assert resp.json()["locked"] is True
    # Never reached the credential lookup or session mint.
    assert get_cred.await_count == 0
    assert create.await_count == 0


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


# ── server-side single-use challenge (Finding 2) ────────────────────────────


@pytest.mark.asyncio
async def test_auth_begin_persists_challenge_server_side(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """begin stores the challenge in the DB and the cookie carries only its id —
    never the challenge bytes."""
    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", AsyncMock(return_value=[_stored_cred()]))
    create = AsyncMock(return_value="abcdef00-0000-4000-8000-000000000000")
    monkeypatch.setattr(webauthn_challenge_repo, "create_challenge", create)
    client.cookies.set(PENDING_COOKIE_NAME, _mint_pending_cookie(_TEST_SECRET, username=_USERNAME))
    resp = await client.post("/auth/webauthn/begin")
    assert resp.status_code == 200
    assert create.await_args.kwargs["purpose"] == "authenticate"
    # The re-minted pending cookie carries the challenge_id, not a challenge.
    from whilly.api.second_factor import _verify_pending_cookie

    new_pending = next(
        v.split(f"{PENDING_COOKIE_NAME}=")[1].split(";")[0]
        for v in resp.headers.get_list("set-cookie")
        if PENDING_COOKIE_NAME in v
    )
    payload = _verify_pending_cookie(_TEST_SECRET, new_pending)
    assert payload is not None and payload.get("c") == "abcdef00-0000-4000-8000-000000000000"


@pytest.mark.asyncio
async def test_auth_verify_replayed_challenge_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-use proof: when consume returns None (already redeemed / expired
    / replayed), verify is rejected before any assertion check or session mint."""
    monkeypatch.setattr(webauthn_challenge_repo, "consume_challenge", AsyncMock(return_value=None))
    get_cred = AsyncMock(return_value=_stored_cred())
    monkeypatch.setattr(webauthn_repo, "get_credential_by_id", get_cred)
    create_sess = AsyncMock(return_value=_session())
    monkeypatch.setattr(sessions, "create_session", create_sess)
    pending = _mint_pending_cookie(_TEST_SECRET, username=_USERNAME, challenge="dead0000-0000-4000-8000-000000000000")
    client.cookies.set(PENDING_COOKIE_NAME, pending)
    resp = await client.post("/auth/webauthn/verify", json={"rawId": _b64url_encode(_CRED_ID), "response": {}})
    assert resp.status_code == 401  # _handle_failed_assertion (attempts < max)
    # Never looked the credential up, never minted a session.
    assert get_cred.await_count == 0
    assert create_sess.await_count == 0


@pytest.mark.asyncio
async def test_register_finish_consumed_challenge_400(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registration finish whose challenge was already consumed/expired is 400."""
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    monkeypatch.setattr(webauthn_challenge_repo, "consume_challenge", AsyncMock(return_value=None))
    insert = AsyncMock(return_value=None)
    monkeypatch.setattr(webauthn_repo, "insert_credential", insert)
    client.cookies.set(COOKIE_NAME, _session_cookie())
    reg_cookie = _mint_pending_cookie(
        _TEST_SECRET, username=_USERNAME, challenge="beef0000-0000-4000-8000-000000000000"
    )
    client.cookies.set(REG_COOKIE_NAME, reg_cookie)
    resp = await client.post("/me/webauthn/register/finish", json={"id": "x", "response": {}})
    assert resp.status_code == 400
    assert insert.await_count == 0


# ── opaque user handle (Finding 3) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_begin_uses_opaque_user_handle(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """register options carry the random server-side handle as user.id — never
    the (PII) username."""
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=_session()))
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=_user()))
    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", AsyncMock(return_value=[]))
    handle = b"\x42" * 32
    monkeypatch.setattr(webauthn_repo, "get_or_create_user_handle", AsyncMock(return_value=handle))
    client.cookies.set(COOKIE_NAME, _session_cookie())
    resp = await client.post("/me/webauthn/register/begin")
    assert resp.status_code == 200
    user_id_b64 = resp.json()["user"]["id"]
    assert _b64url_decode(user_id_b64) == handle
    assert _b64url_decode(user_id_b64) != _USERNAME.encode("utf-8")


# silence unused-import lint for the round-trip decode helper
_ = _b64url_decode
