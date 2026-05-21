"""TOTP enrolment + second-factor verification routes.

PRD-post-auth-hardening §Epic E Item 14b. Four routes behind the
``WHILLY_TOTP_ENABLED=1`` feature flag:

* ``GET  /me/totp/setup``  — show the user's enrolment URI + form to
                              confirm the first generated code.
* ``POST /me/totp/setup``  — verify the user's first code and flip
                              ``user_totp_secrets.enabled`` to TRUE.
* ``GET  /auth/totp``      — render the second-factor form mid-login.
* ``POST /auth/totp``      — verify the second-factor code and mint
                              the real session cookie.

Session state machine
---------------------
The flow when ``WHILLY_TOTP_ENABLED=1`` AND the user has an enabled
TOTP secret:

1. ``POST /auth/login`` validates password as usual.
2. Instead of minting the session cookie, it sets a short-lived
   ``whilly_totp_pending`` signed cookie carrying the username and
   redirects to ``/auth/totp``.
3. ``POST /auth/totp`` verifies the pending cookie + the TOTP code,
   then performs the rest of the original login (create_session +
   set the real session cookie).
4. The pending cookie is cleared on successful verification.

When the flag is OFF or the user has no TOTP secret, the login flow
is byte-equivalent to before this PR — the integration point is a
single conditional in ``submit_login``.

Hardening
---------
* Brute-force lock-out has TWO layers, because the per-cookie attempt
  counter alone is bypassable: it lives in the client-held signed pending
  cookie, so an attacker can reset it by replaying an older ``a=0`` cookie
  before each guess (HMAC stops *editing* the counter, not *reusing* an old
  signed value). And the password account-lockout does NOT backstop this —
  once the password is correct no further password failures accrue while the
  attacker brute-forces the code. So:
  1. **IP rate-limit** (``rate_limit.allow``) on ``POST /auth/totp`` — the
     same edge cap the password endpoint has, which the verify path lacked.
  2. **Server-side per-user lockout** (``users_repo.is_account_locked`` /
     ``register_failed_second_factor``) — shares the ``failed_attempts`` /
     ``locked_until`` columns with the password path, so a wrong code counts
     toward a 15-minute account lock that a cookie replay cannot reset. A
     successful verify clears it via ``users_repo.update_last_login``.
  The per-cookie counter is retained only for the "N attempts remaining" UX.
* ``pyotp`` is a lazy import: when the feature flag is off (default)
  the module never reaches for it, so deployments without the ``totp``
  extras don't crash on import.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
from typing import Final

import asyncpg
from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from whilly.api import auth_audit_repo, rate_limit, sessions, totp_repo, users_repo
from whilly.api.auth_routes import (
    DEFAULT_SESSION_COOKIE_NAME,
    TEMPLATES_DIR,
    _authenticate_session,
    _set_session_cookie,
)
from whilly.api.auth_tokens import (
    DEFAULT_SESSION_TTL_SECONDS,
    mint_session_cookie_value,
)
from whilly.api.second_factor import (
    PENDING_COOKIE_NAME,
    PENDING_COOKIE_TTL_SECONDS,
    PENDING_MAX_ATTEMPTS,
    _clear_pending_cookie,
    _mint_pending_cookie,
    _set_pending_cookie,
    _verify_pending_cookie,
)

logger = logging.getLogger(__name__)

TOTP_ENABLED_ENV: Final[str] = "WHILLY_TOTP_ENABLED"
TOTP_SETUP_TEMPLATE: Final[str] = "totp_setup.html.j2"
TOTP_VERIFY_TEMPLATE: Final[str] = "totp_verify.html.j2"

# The pending-cookie machinery (PENDING_COOKIE_NAME, _mint/_verify/_set/_clear)
# now lives in whilly.api.second_factor and is imported above so TOTP and
# WebAuthn share one implementation. The names are re-exported in __all__ for
# backward compatibility with callers/tests that import them from here.


def totp_enabled() -> bool:
    """Single source of truth for the feature flag."""
    raw = (os.environ.get(TOTP_ENABLED_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def maybe_intercept_for_totp(
    request: Request,
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    user: users_repo.User,
    cookie_secure: bool,
) -> Response | None:
    """Called from ``submit_login`` after password verification.

    Returns a 303 → /auth/totp response (with the pending cookie set)
    when the feature flag is on AND the user has TOTP enabled.
    Returns ``None`` to mean "no interception; complete the login
    normally" — that's the byte-equivalent-to-before path for users
    who don't have TOTP enrolled or for deployments without the flag.
    """
    if not totp_enabled():
        return None
    row = await totp_repo.get_totp_secret(pool, username=user.username)
    if row is None or not row.enabled:
        return None
    pending = _mint_pending_cookie(secret, username=user.username)
    response = RedirectResponse(url="/auth/totp", status_code=status.HTTP_303_SEE_OTHER)
    _set_pending_cookie(response, value=pending, secure=cookie_secure)
    return response


def _otpauth_uri(*, username: str, secret: str, issuer: str = "Whilly") -> str:
    """Build the standard ``otpauth://totp/...`` URI authenticator apps consume."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    params = urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": "6", "period": "30"}
    )
    return f"otpauth://totp/{label}?{params}"


def build_totp_router(
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
    cookie_secure: bool = False,
) -> APIRouter:
    """Construct the TOTP router (registered conditionally on the feature flag)."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    router = APIRouter(tags=["auth"])

    @router.get("/me/totp/setup", response_class=HTMLResponse, include_in_schema=False)
    async def totp_setup_form(request: Request) -> Response:
        """Show the otpauth URI + a code-confirm form. Requires an authenticated session."""
        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        username = _principal_username(principal)
        existing = await totp_repo.get_totp_secret(pool, username=username)
        # Generate a fresh secret on every visit so a half-enrolled user
        # who reloaded mid-setup gets a clean URI. The DB row is upserted
        # only when the confirm POST succeeds.
        import pyotp  # type: ignore[import-untyped]

        new_secret = pyotp.random_base32()
        if existing and existing.enabled:
            display_secret = existing.secret  # already enrolled — show what's stored
        else:
            display_secret = new_secret
        return templates.TemplateResponse(
            request,
            TOTP_SETUP_TEMPLATE,
            {
                "username": username,
                "secret": display_secret,
                "otpauth_uri": _otpauth_uri(username=username, secret=display_secret),
                "already_enrolled": bool(existing and existing.enabled),
                "form_error": None,
            },
        )

    @router.post("/me/totp/setup", response_class=HTMLResponse)
    async def totp_setup_confirm(
        request: Request,
        secret_b32: str = Form(..., min_length=16, max_length=128),
        code: str = Form(..., min_length=6, max_length=8),
    ) -> Response:
        """Validate the code against the secret; on success, persist enabled=TRUE."""
        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        username = _principal_username(principal)
        import pyotp

        totp = pyotp.TOTP(secret_b32)
        if not totp.verify(code, valid_window=1):
            return templates.TemplateResponse(
                request,
                TOTP_SETUP_TEMPLATE,
                {
                    "username": username,
                    "secret": secret_b32,
                    "otpauth_uri": _otpauth_uri(username=username, secret=secret_b32),
                    "already_enrolled": False,
                    "form_error": "Code didn't verify. Try the next one your authenticator shows.",
                },
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        await totp_repo.upsert_totp_secret(pool, username=username, secret=secret_b32, enabled=True)
        logger.info("totp.setup: enrolled username=%r", username)
        return RedirectResponse(url="/me/sessions", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/auth/totp", response_class=HTMLResponse, include_in_schema=False)
    async def totp_verify_form(request: Request) -> Response:
        """Render the second-factor form mid-login."""
        pending = _verify_pending_cookie(secret, request.cookies.get(PENDING_COOKIE_NAME, ""))
        if pending is None:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request,
            TOTP_VERIFY_TEMPLATE,
            {"username": pending.get("u", ""), "form_error": None},
        )

    @router.post(
        "/auth/totp",
        response_class=HTMLResponse,
        summary="Second-factor TOTP verification — mints real session cookie on success",
    )
    async def totp_verify_submit(
        request: Request,
        code: str = Form(..., min_length=6, max_length=8),
    ) -> Response:
        """Validate code + pending cookie → mint real session cookie."""
        pending = _verify_pending_cookie(secret, request.cookies.get(PENDING_COOKIE_NAME, ""))
        if pending is None:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        username = str(pending.get("u", ""))
        attempts = int(pending.get("a", 0))
        if not username:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

        # Layer 1 — IP rate-limit (the password endpoint has this; the verify
        # endpoint did not). Stop a flood before touching the DB.
        client_ip = (request.client.host if request.client else None) or "unknown"
        if not rate_limit.allow(client_ip):
            await auth_audit_repo.insert_attempt(
                pool,
                username=username[:64] or None,
                ip=client_ip,
                user_agent=(request.headers.get("user-agent") or "")[:512] or None,
                outcome="rate_limited",
            )
            return templates.TemplateResponse(
                request,
                TOTP_VERIFY_TEMPLATE,
                {"username": username, "form_error": "Too many attempts. Please wait a moment and try again."},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Layer 2 — server-side per-user lockout, which a replayed pending cookie
        # cannot reset (the cookie-side `a` counter can). See the module docstring.
        if await users_repo.is_account_locked(pool, username=username):
            logger.warning("totp.verify: blocked locked account username=%r", username)
            response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            _clear_pending_cookie(response)
            return response

        row = await totp_repo.get_totp_secret(pool, username=username)
        if row is None or not row.enabled:
            # User's TOTP was disabled between login and verify — bail.
            response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            _clear_pending_cookie(response)
            return response
        import pyotp

        totp = pyotp.TOTP(row.secret)
        if not totp.verify(code, valid_window=1):
            # Server-side budget (authoritative, cookie-replay-proof) + the
            # per-cookie counter (UX only).
            await users_repo.register_failed_second_factor(pool, username=username)
            attempts += 1
            if attempts >= PENDING_MAX_ATTEMPTS:
                logger.warning("totp.verify: %d failed attempts for %r — locking pending cookie", attempts, username)
                response = templates.TemplateResponse(
                    request,
                    TOTP_VERIFY_TEMPLATE,
                    {"username": username, "form_error": "Too many wrong codes. Start over from /login."},
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                )
                _clear_pending_cookie(response)
                return response
            # Re-issue the pending cookie with bumped attempt counter so
            # the budget survives the page reload.
            new_pending = _mint_pending_cookie(secret, username=username, attempts=attempts)
            response = templates.TemplateResponse(
                request,
                TOTP_VERIFY_TEMPLATE,
                {
                    "username": username,
                    "form_error": f"Wrong code. {PENDING_MAX_ATTEMPTS - attempts} attempts remaining.",
                },
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
            _set_pending_cookie(response, value=new_pending, secure=cookie_secure)
            return response

        # Success — mint the real session cookie + clear the pending cookie.
        user = await users_repo.get_user_by_username(pool, username=username)
        if user is None:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        principal_email = user.email or f"{user.username}@local"
        session = await sessions.create_session(pool, email=principal_email)
        cookie_value = mint_session_cookie_value(
            secret,
            session_id=session.session_id,
            email=session.email,
            ttl_seconds=int(session.expires_at.timestamp() - time.time()),
        )
        await users_repo.update_last_login(pool, username=user.username)
        redirect_url = "/auth/change-password" if user.must_change_password else "/"
        response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(
            response,
            cookie_name=cookie_name,
            cookie_value=cookie_value,
            secure=cookie_secure,
            max_age_seconds=int(session.expires_at.timestamp() - time.time()) or DEFAULT_SESSION_TTL_SECONDS,
        )
        _clear_pending_cookie(response)
        logger.info("totp.verify: success for %r", username)
        return response

    return router


def _principal_username(principal: dict) -> str:
    """Recover username from session.email (strip @local synthetic suffix)."""
    email = str(principal.get("email", ""))
    if email.endswith("@local"):
        return email.removesuffix("@local")
    return email


__all__ = [
    "PENDING_COOKIE_NAME",
    "PENDING_COOKIE_TTL_SECONDS",
    "PENDING_MAX_ATTEMPTS",
    "TOTP_ENABLED_ENV",
    "TOTP_SETUP_TEMPLATE",
    "TOTP_VERIFY_TEMPLATE",
    "build_totp_router",
    "maybe_intercept_for_totp",
    "totp_enabled",
]
