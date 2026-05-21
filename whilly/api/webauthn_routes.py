"""WebAuthn / passkey registration + second-factor verification routes (E15).

PRD-post-auth-hardening §Epic E Item 15. Mounted only when
``WHILLY_WEBAUTHN_ENABLED=1`` (see :func:`webauthn_enabled`), so a deployment
that doesn't use passkeys never imports the optional ``webauthn`` package.

Surface
-------
Registration (requires a live **admin** session — gated by
:func:`whilly.api.admin_users_routes.require_admin_role`):

* ``GET  /me/webauthn``                — render the enrolment page (JS harness).
* ``POST /me/webauthn/register/begin`` — issue ``PublicKeyCredentialCreationOptions``
                                         for ``navigator.credentials.create()``.
* ``POST /me/webauthn/register/finish``— verify the attestation and store the
                                         credential.

Second-factor ceremony (redeems the shared pending cookie minted by
:func:`whilly.api.second_factor.maybe_intercept_for_second_factor`):

* ``GET  /auth/webauthn``        — render the assertion page (JS harness).
* ``POST /auth/webauthn/begin``  — issue ``PublicKeyCredentialRequestOptions`` and
                                   bind a fresh single-use challenge to the cookie.
* ``POST /auth/webauthn/verify`` — verify the assertion, reject sign-count
                                   regression, and mint the real session cookie.

Chooser:

* ``GET  /auth/2fa`` — when a user has *both* TOTP and a passkey enrolled, the
                       coordinator routes here so the user can pick.

Security posture (see .planning/E15-E17-auth-security-design.md §2):

* The challenge is server-generated (``os.urandom(32)``), single-use, carried
  inside the HMAC-signed pending/registration cookie, and expires with it.
* ``expected_origin`` / ``rp_id`` come from :class:`WebAuthnConfig` (resolved
  from ``WHILLY_PUBLIC_ORIGIN`` at router build time) — **never** from a request
  header. Building the router with the flag on but no origin raises (fail-closed).
* The authenticator sign-count is stored and the verify path rejects an
  assertion whose counter does not advance (cloned-credential detection).
* Enrolment requires an already-authenticated admin session, so you cannot
  enroll a key for someone else.
* User-verification policy is ``preferred`` (reviewer decision 2026-05-21).

``webauthn`` is imported lazily inside the handlers (the ``pyotp`` precedent),
so importing this module is safe even when the optional package is absent.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import os
import time
from typing import Final
from urllib.parse import urlsplit

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from whilly.api import rate_limit, sessions, users_repo, webauthn_repo
from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import (
    DEFAULT_SESSION_COOKIE_NAME,
    TEMPLATES_DIR,
    _append_event,
    _parse_bool_env,
    _set_session_cookie,
)
from whilly.api.auth_tokens import DEFAULT_SESSION_TTL_SECONDS, mint_session_cookie_value
from whilly.api.prod_mode import cookie_secure_default
from whilly.api.second_factor import (
    PENDING_COOKIE_NAME,
    PENDING_MAX_ATTEMPTS,
    _clear_pending_cookie,
    _mint_pending_cookie,
    _set_pending_cookie,
    _verify_pending_cookie,
)

logger = logging.getLogger(__name__)

WEBAUTHN_ENABLED_ENV: Final[str] = "WHILLY_WEBAUTHN_ENABLED"
PUBLIC_ORIGIN_ENV: Final[str] = "WHILLY_PUBLIC_ORIGIN"
RP_ID_ENV: Final[str] = "WHILLY_WEBAUTHN_RP_ID"
RP_NAME_ENV: Final[str] = "WHILLY_WEBAUTHN_RP_NAME"

WEBAUTHN_VERIFY_TEMPLATE: Final[str] = "webauthn_verify.html.j2"
WEBAUTHN_REGISTER_TEMPLATE: Final[str] = "webauthn_register.html.j2"
CHOOSE_FACTOR_TEMPLATE: Final[str] = "choose_factor.html.j2"

#: Separate signed cookie carrying the registration challenge. The auth-ceremony
#: pending cookie (PENDING_COOKIE_NAME) is for the post-password flow; enrolment
#: happens inside an already-authenticated admin session, so it gets its own.
REG_COOKIE_NAME: Final[str] = "whilly_webauthn_reg"

#: Number of random bytes in a server-generated challenge.
_CHALLENGE_BYTES: Final[int] = 32


def webauthn_enabled() -> bool:
    """Single source of truth for the WebAuthn feature flag."""
    raw = (os.environ.get(WEBAUTHN_ENABLED_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class WebAuthnConfig:
    """RP identity + expected origin, resolved once from server config.

    All three values are the anchor of WebAuthn's phishing resistance, so they
    are derived from ``WHILLY_PUBLIC_ORIGIN`` (not a request header). A wrong or
    empty origin silently disables that resistance, so :meth:`from_env` refuses
    to build when the flag is on but the origin is missing/invalid (fail-closed,
    mirroring the E17 empty-allowlist posture).
    """

    rp_id: str
    rp_name: str
    expected_origin: str

    @classmethod
    def from_env(cls) -> WebAuthnConfig:
        origin = (os.environ.get(PUBLIC_ORIGIN_ENV) or "").strip().rstrip("/")
        if not origin:
            raise RuntimeError(
                f"{WEBAUTHN_ENABLED_ENV}=1 requires {PUBLIC_ORIGIN_ENV} to be set to the public "
                "origin (e.g. https://whilly.example.com). Refusing to start: WebAuthn with an "
                "empty origin silently disables the phishing resistance that is its entire point."
            )
        parts = urlsplit(origin)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise RuntimeError(f"{PUBLIC_ORIGIN_ENV}={origin!r} is not a valid http(s) origin (need scheme://host).")
        rp_id = (os.environ.get(RP_ID_ENV) or "").strip() or parts.hostname
        rp_name = (os.environ.get(RP_NAME_ENV) or "").strip() or "Whilly"
        return cls(rp_id=rp_id, rp_name=rp_name, expected_origin=origin)


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding (the WebAuthn JSON wire format)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _transports_to_enum(transports: list[str] | None) -> list | None:
    """Map stored transport strings to ``AuthenticatorTransport``; drop unknowns."""
    if not transports:
        return None
    from webauthn.helpers.structs import AuthenticatorTransport

    valid = {t.value for t in AuthenticatorTransport}
    mapped = [AuthenticatorTransport(t) for t in transports if t in valid]
    return mapped or None


def build_webauthn_router(
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
    cookie_secure: bool | None = None,
) -> APIRouter:
    """Construct the WebAuthn router (registered only when the flag is on).

    Calling :meth:`WebAuthnConfig.from_env` here means a misconfigured
    deployment (flag on, ``WHILLY_PUBLIC_ORIGIN`` empty/invalid) fails at
    ``create_app`` time — loud and fail-closed — rather than per request.
    """
    config = WebAuthnConfig.from_env()
    if cookie_secure is None:
        cookie_secure = _parse_bool_env("WHILLY_SESSION_COOKIE_SECURE", default=cookie_secure_default())
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    router = APIRouter(tags=["auth"])
    admin_dep = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))

    # ── Registration (admin session required) ──────────────────────────────

    @router.get("/me/webauthn", response_class=HTMLResponse, include_in_schema=False)
    async def webauthn_register_form(request: Request, principal: dict = admin_dep) -> Response:
        existing = await webauthn_repo.get_credentials_by_username(pool, username=str(principal["username"]))
        return templates.TemplateResponse(
            request,
            WEBAUTHN_REGISTER_TEMPLATE,
            {"username": principal.get("username"), "credential_count": len(existing)},
        )

    @router.post("/me/webauthn/register/begin")
    async def webauthn_register_begin(request: Request, principal: dict = admin_dep) -> Response:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )

        username = str(principal["username"])
        existing = await webauthn_repo.get_credentials_by_username(pool, username=username)
        exclude = [
            PublicKeyCredentialDescriptor(id=c.credential_id, transports=_transports_to_enum(c.transports))
            for c in existing
        ]
        challenge = os.urandom(_CHALLENGE_BYTES)
        options = generate_registration_options(
            rp_id=config.rp_id,
            rp_name=config.rp_name,
            user_name=username,
            user_id=username.encode("utf-8"),
            challenge=challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.PREFERRED
            ),
            exclude_credentials=exclude,
        )
        response = Response(content=options_to_json(options), media_type="application/json")
        reg_cookie = _mint_pending_cookie(secret, username=username, challenge=_b64url_encode(challenge))
        _set_pending_cookie(response, value=reg_cookie, secure=cookie_secure, name=REG_COOKIE_NAME)
        return response

    @router.post("/me/webauthn/register/finish")
    async def webauthn_register_finish(request: Request, principal: dict = admin_dep) -> Response:
        from webauthn import verify_registration_response
        from webauthn.helpers.exceptions import InvalidRegistrationResponse

        username = str(principal["username"])
        reg = _verify_pending_cookie(secret, request.cookies.get(REG_COOKIE_NAME, ""))
        challenge_b64 = reg.get("c") if reg else None
        if not reg or not isinstance(challenge_b64, str) or str(reg.get("u")) != username:
            # Cookie missing/expired/forged, or bound to a different user than
            # the authenticated admin — refuse (cannot enroll a key for someone else).
            raise HTTPException(status_code=400, detail="registration session expired — start over")
        body = await request.json()
        try:
            verified = verify_registration_response(
                credential=body,
                expected_challenge=_b64url_decode(challenge_b64),
                expected_rp_id=config.rp_id,
                expected_origin=config.expected_origin,
                require_user_verification=False,
            )
        except (InvalidRegistrationResponse, ValueError, KeyError) as exc:
            logger.warning("webauthn.register: verification failed for %r: %s", username, exc)
            return JSONResponse({"verified": False, "error": "registration verification failed"}, status_code=400)

        transports = None
        if isinstance(body, dict):
            raw_transports = (body.get("response") or {}).get("transports")
            if isinstance(raw_transports, list):
                transports = [str(t) for t in raw_transports]
        try:
            await webauthn_repo.insert_credential(
                pool,
                username=username,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                transports=transports,
            )
        except asyncpg.UniqueViolationError:
            return JSONResponse({"verified": False, "error": "this passkey is already registered"}, status_code=409)
        response = JSONResponse({"verified": True})
        _clear_pending_cookie(response, name=REG_COOKIE_NAME)
        logger.info("webauthn.register: enrolled credential for %r", username)
        return response

    # ── Second-factor ceremony (post-password) ─────────────────────────────

    @router.get("/auth/webauthn", response_class=HTMLResponse, include_in_schema=False)
    async def webauthn_verify_form(request: Request) -> Response:
        pending = _verify_pending_cookie(secret, request.cookies.get(PENDING_COOKIE_NAME, ""))
        if pending is None:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request,
            WEBAUTHN_VERIFY_TEMPLATE,
            {"username": pending.get("u", ""), "has_totp_alt": False},
        )

    @router.post("/auth/webauthn/begin")
    async def webauthn_auth_begin(request: Request) -> Response:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

        client_ip = (request.client.host if request.client else None) or "unknown"
        if not rate_limit.allow(client_ip):
            raise HTTPException(status_code=429, detail="too many requests")
        pending = _verify_pending_cookie(secret, request.cookies.get(PENDING_COOKIE_NAME, ""))
        if pending is None:
            raise HTTPException(status_code=401, detail="login session expired — start over")
        username = str(pending.get("u", ""))
        attempts = int(pending.get("a", 0))
        creds = await webauthn_repo.get_credentials_by_username(pool, username=username)
        if not creds:
            raise HTTPException(status_code=400, detail="no passkey enrolled")
        allow = [
            PublicKeyCredentialDescriptor(id=c.credential_id, transports=_transports_to_enum(c.transports))
            for c in creds
        ]
        challenge = os.urandom(_CHALLENGE_BYTES)
        options = generate_authentication_options(
            rp_id=config.rp_id,
            challenge=challenge,
            allow_credentials=allow,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        response = Response(content=options_to_json(options), media_type="application/json")
        # Re-mint the pending cookie with the fresh challenge bound to it
        # (single-use, expires with the cookie — security gate #1), preserving
        # the failed-attempt counter across the begin→verify round-trip.
        new_pending = _mint_pending_cookie(
            secret, username=username, attempts=attempts, challenge=_b64url_encode(challenge)
        )
        _set_pending_cookie(response, value=new_pending, secure=cookie_secure)
        return response

    @router.post("/auth/webauthn/verify")
    async def webauthn_auth_verify(request: Request) -> Response:
        from webauthn import verify_authentication_response
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse

        client_ip = (request.client.host if request.client else None) or "unknown"
        if not rate_limit.allow(client_ip):
            raise HTTPException(status_code=429, detail="too many requests")
        pending = _verify_pending_cookie(secret, request.cookies.get(PENDING_COOKIE_NAME, ""))
        challenge_b64 = pending.get("c") if pending else None
        if not pending or not isinstance(challenge_b64, str):
            raise HTTPException(status_code=401, detail="login session expired — start over")
        username = str(pending.get("u", ""))
        attempts = int(pending.get("a", 0))

        # Respect the shared server-side lockout (a failed TOTP factor can set it).
        # WebAuthn assertions are not brute-forceable, so a failed assertion does
        # NOT itself bump the counter — that would let a fumbled passkey lock the
        # account (incl. password login) for no security gain.
        if await users_repo.is_account_locked(pool, username=username):
            return JSONResponse(
                {"verified": False, "error": "Account temporarily locked. Start over from /login.", "locked": True},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        body = await request.json()
        raw_id = body.get("rawId") or body.get("id") if isinstance(body, dict) else None
        stored = None
        if isinstance(raw_id, str):
            try:
                stored = await webauthn_repo.get_credential_by_id(pool, credential_id=_b64url_decode(raw_id))
            except (ValueError, TypeError):
                stored = None
        verified = None
        if stored is not None and stored.username == username:
            try:
                verified = verify_authentication_response(
                    credential=body,
                    expected_challenge=_b64url_decode(challenge_b64),
                    expected_rp_id=config.rp_id,
                    expected_origin=config.expected_origin,
                    credential_public_key=stored.public_key,
                    # Passing the stored counter makes py_webauthn reject any
                    # assertion whose counter fails to advance — cloned-credential
                    # detection (security gate #3).
                    credential_current_sign_count=stored.sign_count,
                    require_user_verification=False,
                )
            except (InvalidAuthenticationResponse, ValueError, KeyError) as exc:
                logger.warning("webauthn.verify: assertion failed for %r: %s", username, exc)
                verified = None

        if verified is None:
            return _handle_failed_assertion(secret, username=username, attempts=attempts, cookie_secure=cookie_secure)

        await webauthn_repo.bump_sign_count(
            pool, credential_id=stored.credential_id, new_sign_count=verified.new_sign_count
        )
        return await _mint_session_after_second_factor(
            pool,
            secret=secret,
            username=username,
            cookie_name=cookie_name,
            cookie_secure=cookie_secure,
            method="webauthn",
        )

    # ── Chooser (user has both TOTP and a passkey) ─────────────────────────

    @router.get("/auth/2fa", response_class=HTMLResponse, include_in_schema=False)
    async def choose_factor_form(request: Request) -> Response:
        pending = _verify_pending_cookie(secret, request.cookies.get(PENDING_COOKIE_NAME, ""))
        if pending is None:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request,
            CHOOSE_FACTOR_TEMPLATE,
            {"username": pending.get("u", "")},
        )

    return router


def _handle_failed_assertion(secret: bytes, *, username: str, attempts: int, cookie_secure: bool) -> Response:
    """Bump the per-cookie attempt counter; lock the pending cookie after N fails.

    Mirrors the TOTP brute-force lockout: 5 failed assertions invalidate the
    pending cookie and force the user back to /login. On a non-final failure the
    cookie is re-minted *without* a challenge so the client must call
    ``/auth/webauthn/begin`` again for a fresh one.
    """
    attempts += 1
    if attempts >= PENDING_MAX_ATTEMPTS:
        logger.warning("webauthn.verify: %d failed attempts for %r — locking pending cookie", attempts, username)
        response = JSONResponse(
            {"verified": False, "error": "Too many failed attempts. Start over from /login.", "locked": True},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        _clear_pending_cookie(response)
        return response
    response = JSONResponse(
        {"verified": False, "error": "Passkey verification failed.", "remaining": PENDING_MAX_ATTEMPTS - attempts},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )
    new_pending = _mint_pending_cookie(secret, username=username, attempts=attempts)
    _set_pending_cookie(response, value=new_pending, secure=cookie_secure)
    return response


async def _mint_session_after_second_factor(
    pool: asyncpg.Pool,
    *,
    secret: bytes,
    username: str,
    cookie_name: str,
    cookie_secure: bool,
    method: str,
) -> Response:
    """Complete the login: create the session, set the cookie, clear pending.

    Mirrors the TOTP success path (whilly/api/totp_routes.py) but returns JSON
    (the WebAuthn ceremony is fetch-driven) carrying a ``redirect`` the client
    JS follows after the browser applies the Set-Cookie header.
    """
    user = await users_repo.get_user_by_username(pool, username=username)
    if user is None:
        return JSONResponse({"verified": False, "error": "user no longer exists"}, status_code=401)
    principal_email = user.email or f"{user.username}@local"
    session = await sessions.create_session(pool, email=principal_email)
    ttl = int(session.expires_at.timestamp() - time.time()) or DEFAULT_SESSION_TTL_SECONDS
    cookie_value = mint_session_cookie_value(
        secret, session_id=session.session_id, email=session.email, ttl_seconds=ttl
    )
    await users_repo.update_last_login(pool, username=user.username)
    redirect_url = "/auth/change-password" if user.must_change_password else "/"
    response = JSONResponse({"verified": True, "redirect": redirect_url})
    _set_session_cookie(
        response,
        cookie_name=cookie_name,
        cookie_value=cookie_value,
        secure=cookie_secure,
        max_age_seconds=ttl,
    )
    _clear_pending_cookie(response)
    _append_event(
        {
            "event_type": "auth.session.created",
            "username": user.username,
            "email": session.email,
            "session_id": session.session_id,
            "method": method,
        }
    )
    logger.info("webauthn.verify: success for %r", username)
    return response


__all__ = [
    "PUBLIC_ORIGIN_ENV",
    "REG_COOKIE_NAME",
    "WEBAUTHN_ENABLED_ENV",
    "WebAuthnConfig",
    "build_webauthn_router",
    "webauthn_enabled",
]
