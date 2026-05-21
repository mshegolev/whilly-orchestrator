"""Unit tests for the second-factor coordinator (E15, whilly.api.second_factor).

The coordinator is the dispatch point ``submit_login`` calls after the password
check. It is the highest-risk part of E15 because it changed the single TOTP
intercept into a multi-factor router. These tests pin the full decision table
WITHOUT a DB (the repos are mocked) and, crucially, that flipping
``WHILLY_WEBAUTHN_ENABLED`` off is byte-identical to the pre-E15 TOTP flow.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

import pytest

from whilly.api import second_factor, totp_repo, users_repo, webauthn_repo
from whilly.api.second_factor import maybe_intercept_for_second_factor

_TEST_SECRET: bytes = b"e15-test-secret-32-bytes-padxxxx"
_USERNAME: str = "alice"
_TOTP_ENABLED_ENV = "WHILLY_TOTP_ENABLED"
_WEBAUTHN_ENABLED_ENV = "WHILLY_WEBAUTHN_ENABLED"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_TOTP_ENABLED_ENV, raising=False)
    monkeypatch.delenv(_WEBAUTHN_ENABLED_ENV, raising=False)
    yield


def _user() -> users_repo.User:
    return users_repo.User(
        username=_USERNAME,
        email=None,
        role="admin",
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_login_at=None,
        must_change_password=False,
    )


def _totp_row(enabled: bool = True) -> totp_repo.UserTotpSecret:
    return totp_repo.UserTotpSecret(
        username=_USERNAME,
        secret="JBSWY3DPEHPK3PXP",
        enabled=enabled,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )


def _cred() -> webauthn_repo.WebAuthnCredential:
    return webauthn_repo.WebAuthnCredential(
        username=_USERNAME,
        credential_id=b"cred-1",
        public_key=b"pk",
        sign_count=0,
        transports=None,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        last_used_at=None,
    )


async def _call() -> object:
    # request is unused by the coordinator (and by maybe_intercept_for_totp),
    # so a placeholder is fine; pool is unused once the repos are mocked.
    return await maybe_intercept_for_second_factor(
        None,  # type: ignore[arg-type]
        pool=None,  # type: ignore[arg-type]
        secret=_TEST_SECRET,
        user=_user(),
        cookie_secure=False,
    )


def _set_totp(monkeypatch: pytest.MonkeyPatch, *, enabled_row: bool | None) -> None:
    async def _get(_pool: object, *, username: str) -> totp_repo.UserTotpSecret | None:
        return None if enabled_row is None else _totp_row(enabled=enabled_row)

    monkeypatch.setattr(totp_repo, "get_totp_secret", _get)


def _set_webauthn(monkeypatch: pytest.MonkeyPatch, *, creds: list) -> None:
    async def _get(_pool: object, *, username: str) -> list:
        return creds

    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", _get)


# ── flag OFF: delegate to the unchanged TOTP path (byte-equivalence) ───────


@pytest.mark.asyncio
async def test_webauthn_off_delegates_to_totp_when_enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_TOTP_ENABLED_ENV, "1")
    _set_totp(monkeypatch, enabled_row=True)
    # WebAuthn repo must NOT be consulted on the flag-off path.
    called = {"n": 0}

    async def _boom(_pool: object, *, username: str) -> list:
        called["n"] += 1
        return []

    monkeypatch.setattr(webauthn_repo, "get_credentials_by_username", _boom)
    resp = await _call()
    assert resp is not None
    assert resp.status_code == 303
    assert resp.headers["location"] == second_factor.TOTP_VERIFY_PATH
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_webauthn_off_and_no_totp_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag off + no TOTP enrolled → login completes normally (None).
    _set_totp(monkeypatch, enabled_row=None)
    assert await _call() is None


# ── flag ON: the multi-factor decision table ───────────────────────────────


@pytest.mark.asyncio
async def test_neither_factor_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WEBAUTHN_ENABLED_ENV, "1")
    _set_totp(monkeypatch, enabled_row=None)
    _set_webauthn(monkeypatch, creds=[])
    assert await _call() is None


@pytest.mark.asyncio
async def test_totp_only_redirects_to_totp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WEBAUTHN_ENABLED_ENV, "1")
    monkeypatch.setenv(_TOTP_ENABLED_ENV, "1")
    _set_totp(monkeypatch, enabled_row=True)
    _set_webauthn(monkeypatch, creds=[])
    resp = await _call()
    assert resp is not None and resp.headers["location"] == second_factor.TOTP_VERIFY_PATH


@pytest.mark.asyncio
async def test_webauthn_only_redirects_to_webauthn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WEBAUTHN_ENABLED_ENV, "1")
    # TOTP flag off → not available even if a row somehow existed.
    _set_totp(monkeypatch, enabled_row=True)
    _set_webauthn(monkeypatch, creds=[_cred()])
    resp = await _call()
    assert resp is not None
    assert resp.headers["location"] == second_factor.WEBAUTHN_VERIFY_PATH
    # And the pending cookie is set so the verify page can redeem it.
    set_cookies = [v for (k, v) in resp.raw_headers if k == b"set-cookie"]
    assert any(b"whilly_2fa_pending" in v for v in set_cookies)


@pytest.mark.asyncio
async def test_both_factors_redirects_to_chooser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WEBAUTHN_ENABLED_ENV, "1")
    monkeypatch.setenv(_TOTP_ENABLED_ENV, "1")
    _set_totp(monkeypatch, enabled_row=True)
    _set_webauthn(monkeypatch, creds=[_cred()])
    resp = await _call()
    assert resp is not None and resp.headers["location"] == second_factor.CHOOSE_FACTOR_PATH


@pytest.mark.asyncio
async def test_totp_row_disabled_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag on, TOTP enrolled but enabled=False (mid-setup), passkey present →
    # only WebAuthn is available, so go straight to it (no chooser).
    monkeypatch.setenv(_WEBAUTHN_ENABLED_ENV, "1")
    monkeypatch.setenv(_TOTP_ENABLED_ENV, "1")
    _set_totp(monkeypatch, enabled_row=False)
    _set_webauthn(monkeypatch, creds=[_cred()])
    resp = await _call()
    assert resp is not None and resp.headers["location"] == second_factor.WEBAUTHN_VERIFY_PATH
