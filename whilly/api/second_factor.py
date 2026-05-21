"""Shared second-factor state machine: the pending cookie + the login coordinator.

PRD-post-auth-hardening §Epic E. This module is the one canonical place for the
two-phase login machinery that TOTP (Item 14b) and WebAuthn (Item 15) both reuse:

* the short-lived HMAC-signed *pending* cookie that carries the
  "credentials verified, second factor pending" intermediate state, and
* :func:`maybe_intercept_for_second_factor`, the single dispatch point called
  from ``submit_login`` after the password check.

Why a coordinator (not two sibling intercepts)
----------------------------------------------
A user may have *both* a TOTP secret and a passkey enrolled. The reviewer
decision for E15 is "user picks at login", which two independent sequential
intercepts cannot express (whichever runs first always wins). So the dispatch
is centralised here:

    WebAuthn flag OFF  → delegate to the unchanged ``maybe_intercept_for_totp``
                         (byte-identical to the pre-E15 flow — instant rollback).
    WebAuthn flag ON   → look at what the user actually has enrolled:
        neither         → return None  (login completes with no second factor)
        TOTP only       → 303 /auth/totp
        WebAuthn only   → 303 /auth/webauthn
        both            → 303 /auth/2fa  (the chooser)

The pending cookie is factor-agnostic — it only states "password verified for
user X, awaiting a second factor" — so every verify route (TOTP or WebAuthn)
redeems the same cookie. The WebAuthn ceremony additionally re-mints it carrying
a server-side ``challenge_id`` (the random handle for the single-use challenge
row in ``webauthn_challenges`` — migration 027); TOTP omits that field. The
challenge itself is never in the cookie.

The pending-cookie helpers used to live in :mod:`whilly.api.totp_routes`;
they were moved here unchanged (apart from the rename below) so there is a
single implementation for a reviewer to audit. ``totp_routes`` now imports
them from here.
"""

from __future__ import annotations

import hmac
import json
import time
import urllib.parse
from typing import TYPE_CHECKING, Final

import asyncpg
from fastapi import Request, Response, status
from fastapi.responses import RedirectResponse

if TYPE_CHECKING:
    from whilly.api import users_repo

#: Short-lived signed cookie that carries the "credentials verified, awaiting
#: a second factor" intermediate state. HMAC-signed with the same secret used
#: for session cookies so a tampered value can't bypass the gate. Renamed from
#: the original ``whilly_totp_pending`` now that WebAuthn shares it.
PENDING_COOKIE_NAME: Final[str] = "whilly_2fa_pending"
PENDING_COOKIE_TTL_SECONDS: Final[int] = 5 * 60  # 5 minutes
PENDING_MAX_ATTEMPTS: Final[int] = 5

#: Redirect targets the coordinator dispatches to. Kept as constants so the
#: routes and the coordinator agree on the URLs without a string literal drift.
TOTP_VERIFY_PATH: Final[str] = "/auth/totp"
WEBAUTHN_VERIFY_PATH: Final[str] = "/auth/webauthn"
CHOOSE_FACTOR_PATH: Final[str] = "/auth/2fa"


def _mint_pending_cookie(
    secret: bytes,
    *,
    username: str,
    attempts: int = 0,
    challenge: str | None = None,
) -> str:
    """Sign ``{username, exp, attempts[, challenge]}`` with HMAC-SHA256.

    Returns ``payload.sig``. ``challenge`` here is the WebAuthn ceremony's
    server-side ``challenge_id`` (the random handle for the single-use row in
    ``webauthn_challenges``), carried so it survives the begin→verify round-trip
    bound to this cookie; the challenge bytes themselves stay server-side. TOTP
    never sets it.
    """
    payload: dict[str, object] = {
        "u": username,
        "exp": int(time.time()) + PENDING_COOKIE_TTL_SECONDS,
        "a": attempts,
    }
    if challenge is not None:
        payload["c"] = challenge
    body = urllib.parse.quote(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    sig = hmac.new(secret, body.encode("utf-8"), "sha256").hexdigest()
    return f"{body}.{sig}"


def _verify_pending_cookie(secret: bytes, raw: str) -> dict | None:
    """Return the decoded payload or ``None`` on any failure (bad sig, expired, malformed)."""
    if not raw or "." not in raw:
        return None
    body, _, sig = raw.rpartition(".")
    expected = hmac.new(secret, body.encode("utf-8"), "sha256").hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(urllib.parse.unquote(body))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    return payload


def _set_pending_cookie(
    response: Response,
    *,
    value: str,
    secure: bool,
    name: str = PENDING_COOKIE_NAME,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=PENDING_COOKIE_TTL_SECONDS,
        path="/",
        httponly=True,
        secure=secure,
        samesite="strict",
    )


def _clear_pending_cookie(response: Response, *, name: str = PENDING_COOKIE_NAME) -> None:
    response.delete_cookie(name, path="/")


async def maybe_intercept_for_second_factor(
    request: Request,
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    user: users_repo.User,
    cookie_secure: bool,
) -> Response | None:
    """Dispatch the post-password login to the right second factor.

    Returns a 303 redirect (with the pending cookie set) when the user has a
    second factor to satisfy, or ``None`` to mean "complete the login normally"
    — the byte-equivalent-to-before path for users with no enrolled factor.

    See the module docstring for the dispatch table. Imports of the route /
    repo modules are deferred so (a) the optional ``webauthn`` package is never
    reached when the flag is off and (b) there is no import cycle with
    ``totp_routes`` (which imports the cookie helpers from this module).
    """
    from whilly.api import totp_repo, totp_routes, webauthn_repo, webauthn_routes

    if not webauthn_routes.webauthn_enabled():
        # WebAuthn off ⇒ behaviour is byte-identical to the pre-E15 TOTP-only
        # flow. Delegating to the original intercept (rather than re-deriving
        # it here) is what makes flipping WHILLY_WEBAUTHN_ENABLED off an instant,
        # provable rollback.
        return await totp_routes.maybe_intercept_for_totp(
            request, pool=pool, secret=secret, user=user, cookie_secure=cookie_secure
        )

    totp_avail = False
    if totp_routes.totp_enabled():
        totp_row = await totp_repo.get_totp_secret(pool, username=user.username)
        totp_avail = bool(totp_row and totp_row.enabled)
    creds = await webauthn_repo.get_credentials_by_username(pool, username=user.username)
    webauthn_avail = bool(creds)

    if not totp_avail and not webauthn_avail:
        return None

    if totp_avail and webauthn_avail:
        target = CHOOSE_FACTOR_PATH
    elif webauthn_avail:
        target = WEBAUTHN_VERIFY_PATH
    else:
        target = TOTP_VERIFY_PATH

    pending = _mint_pending_cookie(secret, username=user.username)
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    _set_pending_cookie(response, value=pending, secure=cookie_secure)
    return response


__all__ = [
    "CHOOSE_FACTOR_PATH",
    "PENDING_COOKIE_NAME",
    "PENDING_COOKIE_TTL_SECONDS",
    "PENDING_MAX_ATTEMPTS",
    "TOTP_VERIFY_PATH",
    "WEBAUTHN_VERIFY_PATH",
    "maybe_intercept_for_second_factor",
]
