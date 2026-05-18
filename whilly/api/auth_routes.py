"""FastAPI routes for magic-link login + sessions.

PRD-wui-multi-plan v2 Epic A. Five endpoints:

* ``GET /login`` — email entry form. Pre-fills ``email`` from query string
  so the "Wrong address? Send again" path (Frontend F1) round-trips
  without retyping.
* ``POST /auth/login`` — accept email, create-or-reuse a magic link via
  :mod:`whilly.api.sessions`, write an ``auth.magic_link.issued`` event to
  ``whilly_events.jsonl`` (only on fresh mint — reuse is silent), render
  the "check inbox" confirmation page. **No raw link rendered to the
  browser** (Frontend F7); operators get the link from the log file.
* ``GET /auth/magic`` — verify token signature + DB consume; on success
  set the session cookie and 302-redirect to ``next`` (default ``/``);
  on failure (consumed, expired, forged) render the "link used" page
  (Frontend F2).
* ``GET /me`` — return session principal as JSON. Session-only auth.
* ``POST /auth/logout`` — revoke session, clear cookie, 204.

The cookie is set with ``SameSite=Strict; Path=/; HttpOnly`` and ``Secure``
flag governed by ``WHILLY_SESSION_COOKIE_SECURE`` (default ``false`` for
loopback dev). All endpoints write to ``whilly_events.jsonl`` via the
existing logger pattern from :mod:`whilly.api.dashboard` so dev-mode
audit trail stays in one file.

This module imports FastAPI but NOT asyncpg directly — DB access is
delegated to :mod:`whilly.api.sessions`. See Architect F9 module split.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from whilly.api import auth_tokens, rate_limit, sessions
from whilly.api.csrf import COOKIE_NAME
from whilly.api.prod_mode import cookie_secure_default, is_prod_mode

logger = logging.getLogger(__name__)

TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"
LOGIN_TEMPLATE: Final[str] = "login.html.j2"
LOGIN_MAGIC_TEMPLATE: Final[str] = "login_magic.html.j2"
LOGIN_CHECK_INBOX_TEMPLATE: Final[str] = "login_check_inbox.html.j2"
LOGIN_CONSUMED_TEMPLATE: Final[str] = "login_consumed.html.j2"

DEFAULT_SESSION_COOKIE_NAME: Final[str] = COOKIE_NAME
"""Single canonical cookie name; the CSRF middleware reads the same constant."""

_HOST_PREFIX_COOKIE_NAME: Final[str] = "__Host-whilly_session"

CHANGE_PASSWORD_TEMPLATE: Final[str] = "password_change.html.j2"
ME_PASSWORD_TEMPLATE: Final[str] = "me_password.html.j2"
ME_SESSIONS_TEMPLATE: Final[str] = "me_sessions.html.j2"
_MIN_PASSWORD_LENGTH: Final[int] = 12
"""__Host- prefixed name used in prod+secure mode for strongest browser binding."""


def session_cookie_name(*, secure: bool | None = None) -> str:
    """Return the canonical session cookie name for the current environment.

    When prod mode is active AND the Secure flag is on, the ``__Host-``
    prefix is used so browsers enforce ``Secure; Path=/; no Domain``.
    In all other cases the plain ``whilly_session`` name is returned for
    backward compatibility with dev setups and proxied deployments that
    strip TLS before the app.

    The ``secure`` parameter lets the caller pass the already-resolved
    value (so we don't re-read the env twice); when omitted, it is derived
    from :func:`~whilly.api.prod_mode.cookie_secure_default` and the
    ``WHILLY_SESSION_COOKIE_SECURE`` override.
    """
    if secure is None:
        raw = (os.environ.get("WHILLY_SESSION_COOKIE_SECURE") or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            secure = True
        elif raw in {"0", "false", "no", "off"}:
            secure = False
        else:
            secure = cookie_secure_default()
    if is_prod_mode() and secure:
        return _HOST_PREFIX_COOKIE_NAME
    return DEFAULT_SESSION_COOKIE_NAME


EVENT_LOG_PATH_ENV: Final[str] = "WHILLY_EVENT_LOG_PATH"
DEFAULT_EVENT_LOG_PATH: Final[str] = "whilly_logs/whilly_events.jsonl"

#: Type alias for the secret-getter dependency.
SecretGetter = Callable[[], bytes]
#: Type alias for the pool-getter dependency.
PoolGetter = Callable[[], asyncpg.Pool]


def build_auth_router(
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
    cookie_secure: bool | None = None,
) -> APIRouter:
    """Construct the auth router bound to a connection pool and HMAC secret.

    Pattern mirrors :func:`whilly.adapters.transport.server.create_app`:
    factory wiring (not a module-level ``APIRouter()`` singleton) so unit
    tests can inject testcontainer pools and per-test secrets.
    """
    if cookie_secure is None:
        cookie_secure = _parse_bool_env("WHILLY_SESSION_COOKIE_SECURE", default=cookie_secure_default())

    # Resolve the canonical cookie name once at router-build time so all
    # handlers in this closure use the same value without re-reading env.
    resolved_cookie_name = session_cookie_name(secure=cookie_secure)
    if resolved_cookie_name != cookie_name:
        # The caller passed an explicit cookie_name that differs from what
        # the prod-mode logic would select.  Honour the caller (the caller
        # is usually a test fixture) but log a debug note.
        logger.debug(
            "build_auth_router: caller provided cookie_name=%r; prod-mode resolved name=%r. "
            "Using caller-provided value.",
            cookie_name,
            resolved_cookie_name,
        )
    else:
        cookie_name = resolved_cookie_name

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    router = APIRouter(tags=["auth"])

    @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(
        request: Request,
        username: str | None = Query(default=None, max_length=64),
    ) -> Response:
        """Username + password sign-in form. Pre-fills ``username`` from query."""
        return templates.TemplateResponse(
            request,
            LOGIN_TEMPLATE,
            {"username_prefill": (username or "").strip(), "login_error": None},
        )

    @router.get("/login/magic", response_class=HTMLResponse, include_in_schema=False)
    async def login_magic_form(
        request: Request,
        email: str | None = Query(default=None, max_length=320),
    ) -> Response:
        """Alternative passwordless magic-link form. Reachable from /login footer."""
        return templates.TemplateResponse(
            request,
            LOGIN_MAGIC_TEMPLATE,
            {"email_prefill": (email or "").strip()},
        )

    @router.post(
        "/auth/login",
        response_class=HTMLResponse,
        include_in_schema=True,
        summary="Submit username+password, mint a session cookie on success",
    )
    async def submit_login(
        request: Request,
        username: str = Form(..., min_length=1, max_length=64),
        password: str = Form(..., min_length=1, max_length=512),
    ) -> Response:
        from whilly.api import auth_audit_repo, users_repo

        # PRD-post-auth-hardening §Epic D Item 11 instrumentation (D10b).
        # Pull client metadata once at the top so every audit branch records
        # the same shape. user_agent is best-effort and truncated at 512.
        client_ip = (request.client.host if request.client else None) or "unknown"
        user_agent = (request.headers.get("user-agent") or "")[:512] or None

        # P1.2: IP rate limit — checked before touching the DB so a flood of
        # requests is stopped at the edge without creating DB load.
        if not rate_limit.allow(client_ip):
            await auth_audit_repo.insert_attempt(
                pool,
                username=username.strip()[:64] or None,
                ip=client_ip,
                user_agent=user_agent,
                outcome="rate_limited",
            )
            raise HTTPException(status_code=429, detail="too many requests")

        user = await users_repo.verify_credentials(pool, username=username, password=password)

        # PRD-post-auth-hardening §Epic E Item 14b — TOTP second-factor
        # integration point. When WHILLY_TOTP_ENABLED=1 AND the user has
        # totp.enabled=TRUE, this returns a 303 → /auth/totp with a signed
        # pending cookie carrying the verified username; otherwise None
        # and the login completes as before. By design, the integration
        # is a single conditional so flipping WHILLY_TOTP_ENABLED off is
        # an instant rollback to the byte-equivalent pre-E14b flow.
        if user is not None:
            from whilly.api.totp_routes import maybe_intercept_for_totp

            intercept = await maybe_intercept_for_totp(
                request, pool=pool, secret=secret, user=user, cookie_secure=cookie_secure
            )
            if intercept is not None:
                return intercept

        if user is None:
            logger.info("auth.login: credential rejection for username=%r", username[:64])
            # NOTE: verify_credentials returns None for THREE distinct failures
            # (bad_password / locked / missing_user) — the contract deliberately
            # collapses them at the route layer to avoid an enumeration leak.
            # We record outcome='bad_password' as the catch-all; distinguishing
            # the three would require refactoring verify_credentials to return
            # the reason out-of-band, which is intentionally out of D10b's
            # narrow scope. Audit consumers should read this as "auth failed
            # for one of the three reasons" not "wrong password specifically".
            await auth_audit_repo.insert_attempt(
                pool,
                username=username.strip()[:64] or None,
                ip=client_ip,
                user_agent=user_agent,
                outcome="bad_password",
            )
            return templates.TemplateResponse(
                request,
                LOGIN_TEMPLATE,
                {
                    "username_prefill": username.strip()[:64],
                    "login_error": "Invalid username or password.",
                },
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        # Establish session + cookie. Email defaults to ``<username>@local``
        # so the existing sessions table (keyed on email) keeps its shape.
        principal_email = user.email or f"{user.username}@local"
        session = await sessions.create_session(pool, email=principal_email)
        cookie_value = auth_tokens.mint_session_cookie_value(
            secret,
            session_id=session.session_id,
            email=session.email,
            ttl_seconds=int((session.expires_at.timestamp() - time.time())),
        )
        await users_repo.update_last_login(pool, username=user.username)
        # Audit: successful login — session_id links back to the row in `sessions`.
        try:
            import uuid as _uuid

            audit_session_id = _uuid.UUID(session.session_id) if session.session_id else None
        except (TypeError, ValueError):
            # Session IDs that aren't valid UUIDs (legacy / synthetic) skip the
            # session_id column rather than failing the audit insert. Logged
            # only at DEBUG because this is expected for some session-id schemes.
            audit_session_id = None
        await auth_audit_repo.insert_attempt(
            pool,
            username=user.username,
            ip=client_ip,
            user_agent=user_agent,
            outcome="ok",
            session_id=audit_session_id,
        )
        _append_event(
            {
                "event_type": "auth.session.created",
                "username": user.username,
                "email": session.email,
                "session_id": session.session_id,
                "method": "password",
            },
        )
        # P1.1: when must_change_password is set, redirect to the change-password
        # form immediately after session creation.  The session is valid; access to
        # other pages is gated by the per-request DB check in
        # :func:`_check_must_change_password`.
        redirect_url = "/auth/change-password" if user.must_change_password else "/"
        redirect = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(
            redirect,
            cookie_name=cookie_name,
            cookie_value=cookie_value,
            secure=cookie_secure,
            max_age_seconds=int((session.expires_at.timestamp() - time.time())),
        )
        return redirect

    @router.post(
        "/auth/magic-login",
        response_class=HTMLResponse,
        include_in_schema=True,
        summary="Submit email, mint magic link (passwordless fallback)",
    )
    async def submit_magic_login(
        request: Request,
        email: str = Form(..., min_length=3, max_length=320),
    ) -> Response:
        # P1.2: IP rate limit on magic-login the same as on password login.
        client_ip = (request.client.host if request.client else None) or "unknown"
        if not rate_limit.allow(client_ip):
            raise HTTPException(status_code=429, detail="too many requests")

        normalised = email.strip().lower()
        if not _looks_like_email(normalised):
            # Render the same "check inbox" page on bad email to avoid
            # leaking validation hints to enumeration attackers. Operators
            # see the typo themselves when no link arrives; the [Send again]
            # affordance handles the loop.
            logger.warning("auth.magic-login: rejected non-email input shape (length=%d)", len(normalised))
            return templates.TemplateResponse(
                request,
                LOGIN_CHECK_INBOX_TEMPLATE,
                {"email": normalised},
            )

        link = await sessions.create_magic_link(pool, email=normalised, secret=secret)
        if link.raw_token is not None:
            # Fresh mint — emit issued event (audit) AND hand off to the Mailer
            # for delivery. The Mailer auto-falls-back to writing an
            # auth.magic_link.sent event when SMTP isn't configured, so
            # dev / loopback keeps the v2 behaviour and prod gets real email
            # without changes to this call site (PRD-post-auth-hardening §Epic
            # C Item 12). On reuse, raw_token is None and we deliberately do
            # NOT mint a duplicate event or re-send the email within the
            # reuse window — SC-2.3.
            from whilly.api.mailer import Mailer

            magic_url = _build_magic_url(request, link.raw_token)
            expires_at_unix = int(link.expires_at.timestamp())
            _append_event(
                {
                    "event_type": "auth.magic_link.issued",
                    "email": normalised,
                    "expires_at_unix": expires_at_unix,
                    "magic_link_url": magic_url,
                },
            )
            transport = await Mailer().send_magic_link(
                email=normalised,
                magic_url=magic_url,
                expires_at_unix=expires_at_unix,
            )
            logger.info("auth.magic-login: link delivered to %s via %s", normalised, transport)
        else:
            logger.info("auth.magic-login: reused existing unconsumed magic link for %s", normalised)

        return templates.TemplateResponse(
            request,
            LOGIN_CHECK_INBOX_TEMPLATE,
            {"email": normalised},
        )

    @router.get(
        "/auth/magic", response_class=HTMLResponse, include_in_schema=True, summary="Verify magic link, mint session"
    )
    async def consume_magic(
        request: Request,
        token: str = Query(..., min_length=10, max_length=4096),
        next_path: str = Query(default="/", alias="next", max_length=1024),
    ) -> Response:
        # 1. Verify signature + typ + expiry. Failure here means forged or
        # expired before consume — same UX as "already used" (Frontend F2).
        try:
            auth_tokens.verify_magic_link_token(token, secret)
        except auth_tokens.AuthTokenError as exc:
            logger.info("auth.magic: verify failed: %s", exc)
            return templates.TemplateResponse(request, LOGIN_CONSUMED_TEMPLATE, {})

        # 2. Hash and consume. consume_magic_link returns None on already-
        # consumed / expired / not-found, all of which render the same page.
        token_hash = auth_tokens.hash_token(token)
        consumed = await sessions.consume_magic_link(pool, token_hash=token_hash)
        if consumed is None:
            return templates.TemplateResponse(request, LOGIN_CONSUMED_TEMPLATE, {})

        # 3. Create session row + cookie value.
        session = await sessions.create_session(pool, email=consumed.email)
        cookie_value = auth_tokens.mint_session_cookie_value(
            secret,
            session_id=session.session_id,
            email=session.email,
            ttl_seconds=int((session.expires_at.timestamp() - time.time())),
        )

        # 4. Audit event for the successful sign-in.
        _append_event(
            {
                "event_type": "auth.session.created",
                "email": session.email,
                "session_id": session.session_id,
            },
        )

        # 5. Redirect. We restrict next_path to local paths to avoid
        # open-redirect-via-login attacks.
        safe_next = _sanitise_next_path(next_path)
        redirect = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(
            redirect,
            cookie_name=cookie_name,
            cookie_value=cookie_value,
            secure=cookie_secure,
            max_age_seconds=int((session.expires_at.timestamp() - time.time())),
        )
        return redirect

    @router.get("/me", include_in_schema=True, summary="Return current session principal")
    async def me(request: Request) -> JSONResponse:
        principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        return JSONResponse(
            {
                "email": principal["email"],
                "session_id": principal["session_id"],
                "expires_at_unix": principal["expires_at_unix"],
            }
        )

    @router.post("/auth/logout", include_in_schema=True, summary="Revoke session, clear cookie")
    async def logout(request: Request) -> Response:
        cookie_raw = request.cookies.get(cookie_name)
        session_id: str | None = None
        if cookie_raw:
            try:
                claims = auth_tokens.verify_session_cookie_value(cookie_raw, secret)
                session_id = claims.get("sid")
            except auth_tokens.AuthTokenError:
                session_id = None
        if session_id:
            revoked = await sessions.revoke_session(pool, session_id=session_id)
            if revoked:
                _append_event({"event_type": "auth.session.revoked", "session_id": session_id})
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        # Clear the cookie regardless of whether we found a valid session —
        # the browser may be carrying a stale cookie tied to a deleted row.
        response.delete_cookie(cookie_name, path="/")
        return response

    # P1.1 — change-password routes ───────────────────────────────────────────

    @router.get(
        "/auth/change-password",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def change_password_form(request: Request) -> Response:
        """Render the change-password form.

        Does not require an authenticated session — the browser reaches this
        page immediately after login when ``must_change_password`` is True.
        """
        return templates.TemplateResponse(
            request,
            CHANGE_PASSWORD_TEMPLATE,
            {"form_error": None},
        )

    @router.post(
        "/auth/change-password",
        response_class=HTMLResponse,
        include_in_schema=True,
        summary="Set a new password; clears must_change_password flag",
    )
    async def submit_change_password(
        request: Request,
        new_password: str = Form(..., min_length=1, max_length=512),
        confirm_new_password: str = Form(..., min_length=1, max_length=512),
    ) -> Response:
        """Validate + store new password, then redirect to dashboard.

        CSRF-protected by the existing :class:`~whilly.api.csrf.WhillySessionCSRFMiddleware`
        (POST with a session cookie present — not on the exempt list).
        Requires an authenticated session so a stale cookie cannot be used
        to reset someone else's password.
        """
        from whilly.api import users_repo

        # Require an authenticated session to reach this endpoint.
        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

        def _render_error(msg: str) -> Response:
            return templates.TemplateResponse(
                request,
                CHANGE_PASSWORD_TEMPLATE,
                {"form_error": msg},
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if new_password != confirm_new_password:
            return _render_error("Passwords do not match.")
        if len(new_password) < _MIN_PASSWORD_LENGTH:
            return _render_error(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")

        # Extract username from the session email.  The email stored in the
        # sessions table is either the real email or the ``<username>@local``
        # synthetic address produced by submit_login.
        session_email: str = str(principal.get("email", ""))
        username = session_email.removesuffix("@local") if session_email.endswith("@local") else session_email
        if not username:
            logger.warning("auth.change-password: could not resolve username from session email %r", session_email)
            return _render_error("Session error — please log out and log in again.")

        try:
            await users_repo.set_password(pool, username=username, new_password=new_password)
        except (ValueError, LookupError) as exc:
            logger.warning("auth.change-password: set_password failed for %r: %s", username, exc)
            return _render_error("Could not update password. Please try again.")

        # PRD-post-auth-hardening §Epic C Item 6: drop the gate's cached
        # must_change verdict for this session so the very next request
        # (the 303 redirect to /) is not bounced back to the change-password
        # form by a stale True verdict.
        session_id_raw = principal.get("session_id")
        if isinstance(session_id_raw, str) and session_id_raw:
            from whilly.api.must_change_gate import invalidate_session

            invalidate_session(session_id_raw)

        _append_event(
            {
                "event_type": "auth.password.changed",
                "username": username,
                "email": session_email,
                "session_id": principal.get("session_id"),
            }
        )
        logger.info("auth.change-password: password changed for username=%r", username)
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # PRD-post-auth-hardening §Epic D, Item 9 — voluntary self-service
    # password change ─────────────────────────────────────────────────────────
    #
    # Differs from /auth/change-password in two ways:
    #   1. Requires the user's *current* password (validated via
    #      users_repo.verify_credentials) so a stolen session cookie cannot
    #      rotate the password without knowing the original.
    #   2. Always requires an authenticated session — there is no
    #      must_change_password "forced flow" entry path here.
    #
    # set_password() clears the must_change_password flag as a side effect, so
    # if the user happened to be in the forced flow this endpoint also
    # satisfies it.

    @router.get(
        "/me/password",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def me_password_form(request: Request) -> Response:
        """Render the voluntary self-service password-change form.

        Unauthenticated requests are redirected to ``/login`` so an attacker
        cannot probe whether an account exists by hitting this URL directly.
        """
        try:
            await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request,
            ME_PASSWORD_TEMPLATE,
            {"form_error": None},
        )

    @router.post(
        "/me/password",
        response_class=HTMLResponse,
        include_in_schema=True,
        summary="Voluntary self-service password change (requires current password)",
    )
    async def submit_me_password(
        request: Request,
        current_password: str = Form(..., min_length=1, max_length=512),
        new_password: str = Form(..., min_length=1, max_length=512),
        confirm_new_password: str = Form(..., min_length=1, max_length=512),
    ) -> Response:
        """Validate current password, store new password, redirect to dashboard.

        CSRF-protected by :class:`~whilly.api.csrf.WhillySessionCSRFMiddleware`
        (POST with a session cookie present, not on the CSRF exempt list).
        """
        from whilly.api import users_repo

        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

        def _render_error(msg: str) -> Response:
            return templates.TemplateResponse(
                request,
                ME_PASSWORD_TEMPLATE,
                {"form_error": msg},
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Resolve the username from the session — same @local-strip logic as
        # the forced-flow endpoint above.
        session_email: str = str(principal.get("email", ""))
        username = session_email.removesuffix("@local") if session_email.endswith("@local") else session_email
        if not username:
            logger.warning(
                "me.password: could not resolve username from session email %r",
                session_email,
            )
            return _render_error("Session error — please log out and log in again.")

        # Validate the *current* password first. This is the key difference
        # from the forced /auth/change-password endpoint. verify_credentials
        # also drives the failed-attempts counter and account lockout, so
        # spraying current_password guesses costs the attacker access after
        # five wrong tries.
        verified_user = await users_repo.verify_credentials(pool, username=username, password=current_password)
        if verified_user is None:
            return _render_error("Current password is incorrect.")

        # Now the same new/confirm + length checks as the forced flow.
        if new_password != confirm_new_password:
            return _render_error("New passwords do not match.")
        if len(new_password) < _MIN_PASSWORD_LENGTH:
            return _render_error(f"New password must be at least {_MIN_PASSWORD_LENGTH} characters.")

        try:
            await users_repo.set_password(pool, username=username, new_password=new_password)
        except (ValueError, LookupError) as exc:
            logger.warning("me.password: set_password failed for %r: %s", username, exc)
            return _render_error("Could not update password. Please try again.")

        # PRD-post-auth-hardening §Epic C Item 6: drop the gate's cached
        # must_change verdict for this session. Voluntary changes can happen
        # while must_change_password=True (a returning user noticed the flag
        # and pre-emptively cleared it), so the next request after this one
        # must not be bounced back to /auth/change-password by a stale True.
        session_id_raw = principal.get("session_id")
        if isinstance(session_id_raw, str) and session_id_raw:
            from whilly.api.must_change_gate import invalidate_session

            invalidate_session(session_id_raw)

        _append_event(
            {
                "event_type": "auth.password.changed",
                "username": username,
                "email": session_email,
                "session_id": principal.get("session_id"),
                "source": "self_service",
            }
        )
        logger.info("me.password: password changed for username=%r (self-service)", username)
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # PRD-post-auth-hardening §Epic E, Item 16 — active-sessions UI ──────────
    #
    # GET /me/sessions  → list non-revoked sessions for the principal's email
    # POST /me/sessions/{session_id}/revoke → drop the row; if it was the
    #   current session, also clear the cookie and 303 to /login.
    #
    # The sessions schema (migration 018) does NOT carry user_agent or IP
    # columns — PRD prose mentions them but they don't exist. The template
    # shows session_id prefix + created_at + last_seen_at + expires_at,
    # which is what is actually queryable today. Adding ip/user_agent is
    # a future migration.

    @router.get("/me/sessions", response_class=HTMLResponse, include_in_schema=False)
    async def list_me_sessions(request: Request) -> Response:
        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        principal_email = str(principal.get("email", ""))
        active = await sessions.list_active_sessions_for_email(pool, email=principal_email)
        return templates.TemplateResponse(
            request,
            ME_SESSIONS_TEMPLATE,
            {
                "sessions": active,
                "current_session_id": principal.get("session_id"),
                "principal_email": principal_email,
                "flash": None,
            },
        )

    @router.post(
        "/me/sessions/{session_id}/revoke",
        response_class=HTMLResponse,
        include_in_schema=True,
        summary="Revoke a session (logs out the device); revoking the current session redirects to /login",
    )
    async def revoke_me_session(request: Request, session_id: str) -> Response:
        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        principal_email = str(principal.get("email", ""))
        # Guard: the target session must belong to the principal — otherwise
        # an authenticated user could revoke someone else's session by
        # guessing the session_id. Look it up via verify_session (which
        # returns None for expired/revoked/missing) and check the email.
        target = await sessions.verify_session(pool, session_id=session_id)
        if target is None or target.email != principal_email:
            logger.warning(
                "me.sessions.revoke: %r tried to revoke session %r they don't own",
                principal_email,
                session_id[:8] if session_id else "?",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
        await sessions.revoke_session(pool, session_id=session_id)
        is_self_revoke = session_id == principal.get("session_id")
        if is_self_revoke:
            # Revoking the current session — clear the cookie and bounce
            # to /login. The cookie clear matches the /auth/logout pattern.
            response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie(cookie_name, path="/")
            return response
        # Other-device revoke — re-render the list with a flash. The
        # underlying GET handler runs in the same closure, so just call
        # the list query again here.
        active = await sessions.list_active_sessions_for_email(pool, email=principal_email)
        return templates.TemplateResponse(
            request,
            ME_SESSIONS_TEMPLATE,
            {
                "sessions": active,
                "current_session_id": principal.get("session_id"),
                "principal_email": principal_email,
                "flash": f"revoked session {session_id[:8]}…",
            },
        )

    return router


# ─── Authentication helper exposed for other routers (plans/tasks CRUD) ──────


async def authenticate_session_request(
    request: Request,
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> dict[str, object]:
    """Validate the session cookie and return the principal.

    Raises ``HTTPException(401)`` when the cookie is missing, malformed,
    expired, or refers to a revoked/non-existent session. The CRUD routers
    in :mod:`whilly.api.plans_api` and :mod:`whilly.api.tasks_api_crud` call
    this to enforce SC-5.1 (session-only on new CRUD).
    """
    return await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)


async def _authenticate_session(
    request: Request,
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    cookie_name: str,
) -> dict[str, object]:
    cookie_raw = request.cookies.get(cookie_name)
    if not cookie_raw:
        raise HTTPException(status_code=401, detail="no session cookie")
    try:
        claims = auth_tokens.verify_session_cookie_value(cookie_raw, secret)
    except auth_tokens.ExpiredAuthTokenError:
        raise HTTPException(status_code=401, detail="session expired") from None
    except auth_tokens.AuthTokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid session cookie: {exc}") from None
    sid = claims.get("sid")
    if not isinstance(sid, str):
        raise HTTPException(status_code=401, detail="malformed session claims")
    session = await sessions.verify_session(pool, session_id=sid)
    if session is None:
        raise HTTPException(status_code=401, detail="session not found or revoked")
    return {
        "email": session.email,
        "session_id": session.session_id,
        "expires_at_unix": int(session.expires_at.timestamp()),
    }


# ─── Internal helpers ────────────────────────────────────────────────────────


def _set_session_cookie(
    response: Response,
    *,
    cookie_name: str,
    cookie_value: str,
    secure: bool,
    max_age_seconds: int,
) -> None:
    """Set the session cookie with SameSite=Strict; HttpOnly; Path=/."""
    response.set_cookie(
        key=cookie_name,
        value=cookie_value,
        max_age=max(1, int(max_age_seconds)),
        path="/",
        httponly=True,
        secure=bool(secure),
        samesite="strict",
    )


def _build_magic_url(request: Request, token: str) -> str:
    """Construct the absolute /auth/magic URL emitted to the event log."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/magic?token={urllib.parse.quote(token, safe='')}"


def _sanitise_next_path(raw: str) -> str:
    """Constrain ``?next=`` to local paths so /auth/magic cannot be an open redirect."""
    if not raw or not isinstance(raw, str):
        return "/"
    if raw.startswith("//") or raw.startswith("\\\\"):
        return "/"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not parsed.path.startswith("/"):
        return "/"
    # Reassemble path+query+fragment so legitimate deep-links like
    # /plans/foo?fragment=tasks survive the trip.
    out = parsed.path
    if parsed.query:
        out += "?" + parsed.query
    if parsed.fragment:
        out += "#" + parsed.fragment
    return out


def _looks_like_email(s: str) -> bool:
    """Cheap sanity check, NOT a validator. RFC 5322 is famously hard; we accept anything
    with one '@' surrounded by non-empty user and domain segments containing a dot."""
    if not isinstance(s, str) or "@" not in s:
        return False
    user, _, domain = s.partition("@")
    return bool(user) and "." in domain


def _append_event(event: dict[str, object]) -> None:
    """Append a single JSON line to whilly_events.jsonl.

    Best-effort: on filesystem errors we log a warning but do not raise —
    the user-visible auth flow must not fail because the event log is full
    or read-only.
    """
    event = dict(event)
    event.setdefault("ts", _isoformat_now())
    log_path = Path(os.environ.get(EVENT_LOG_PATH_ENV, DEFAULT_EVENT_LOG_PATH))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning("auth: event-log append failed (%s): %s", log_path, exc)


def _isoformat_now() -> str:
    """Return current UTC time in ISO-8601 format without microseconds."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_bool_env(name: str, *, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


__all__ = [
    "CHANGE_PASSWORD_TEMPLATE",
    "DEFAULT_EVENT_LOG_PATH",
    "DEFAULT_SESSION_COOKIE_NAME",
    "EVENT_LOG_PATH_ENV",
    "ME_PASSWORD_TEMPLATE",
    "ME_SESSIONS_TEMPLATE",
    "_HOST_PREFIX_COOKIE_NAME",
    "authenticate_session_request",
    "build_auth_router",
    "session_cookie_name",
]


# Mark Depends as intentionally exported (the test layer may want to depend
# on the auth-router factory's PoolGetter/SecretGetter aliases).
_ = (Depends, Awaitable)
