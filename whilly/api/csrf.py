"""CSRF middleware for cookie-authenticated state-mutating requests.

PRD-wui-multi-plan v2 §6.1 / §9 (risk row "CSRF on cookie-authenticated
mutations"). The default ``SameSite=Lax`` browser cookie protection does
not stop top-level cross-site ``POST`` from a form, and the v2 design adds
a long-lived ``whilly_session`` cookie. Without a CSRF gate, a malicious
page could POST to ``/api/v1/plans`` from the operator's logged-in browser.

Defence in three layers:

1. The cookie itself is minted with ``SameSite=Strict`` (set in
   :mod:`whilly.api.auth_routes`). Modern browsers refuse to attach it to
   any cross-site navigation, top-level or otherwise.
2. This middleware adds an explicit ``Origin`` allowlist check for every
   *state-mutating* request that authenticates via the session cookie
   (POST / PATCH / PUT / DELETE). Even if a future browser regression or
   misconfiguration relaxes SameSite, the Origin check is independent.
3. Worker-bearer / dashboard-JWT auth paths are exempt because they do
   not rely on ambient credentials — the attacker would need to know the
   token, which is the auth model already.

The allowlist is read from the env var ``WHILLY_CSRF_ORIGIN_ALLOWLIST``
(comma-separated). Default: ``http://127.0.0.1:8000,http://localhost:8000``.
Same-origin requests (``Origin`` matches the request scheme+host+port) are
always accepted regardless of allowlist contents.

Routes registered under ``/auth/`` are exempt from the Origin check on
``POST /auth/login`` — the operator's first POST comes from a no-cookie
state, so there is no session to protect. ``/auth/logout`` is gated; an
attacker forcing logout would be annoying but not data-exfiltrating.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

DEFAULT_CSRF_ALLOWLIST: Final[tuple[str, ...]] = (
    "http://127.0.0.1:8000",
    "http://localhost:8000",
)
"""Default Origin allowlist for local development."""

CSRF_ALLOWLIST_ENV: Final[str] = "WHILLY_CSRF_ORIGIN_ALLOWLIST"

STATE_MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PATCH", "PUT", "DELETE"})

#: Paths whose state-mutating verbs are NOT cookie-protected because they
#: are the entry points used before any session exists. ``/auth/login``
#: is the username+password submit, ``/auth/magic-login`` is the email
#: passwordless submit, ``/auth/magic`` is verified by its own token
#: signature so CSRF would be ineffective anyway.
CSRF_EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/auth/login", "/auth/magic-login", "/auth/magic"})

#: Cookie name the middleware looks for to decide "this request is
#: cookie-authenticated and therefore needs CSRF gating".
COOKIE_NAME: Final[str] = "whilly_session"


class WhillySessionCSRFMiddleware(BaseHTTPMiddleware):
    """Block cookie-authenticated state-mutating requests with a missing/bad Origin.

    Applied as ASGI middleware via :meth:`fastapi.FastAPI.add_middleware`.
    Order matters: this must be installed *before* the routers so it runs
    on every request, including ones whose handler would otherwise return
    early. See :mod:`whilly.adapters.transport.server` :func:`create_app`.
    """

    def __init__(self, app: ASGIApp, *, allowlist: Iterable[str] | None = None) -> None:
        super().__init__(app)
        if allowlist is None:
            raw = os.environ.get(CSRF_ALLOWLIST_ENV, "").strip()
            entries = [s.strip() for s in raw.split(",") if s.strip()] if raw else list(DEFAULT_CSRF_ALLOWLIST)
        else:
            entries = [s.strip() for s in allowlist if s.strip()]
        # Normalise: strip trailing slash so configured "http://host/" matches
        # incoming "http://host". Browsers never send the trailing slash on
        # Origin, but operators paste URLs with various levels of polish.
        self._allowlist: frozenset[str] = frozenset(s.rstrip("/") for s in entries)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _needs_csrf_check(request):
            return await call_next(request)

        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin and origin in self._allowlist:
            return await call_next(request)

        # Same-origin fallback: when the browser sends Origin matching the
        # request's own host+port, accept regardless of allowlist contents.
        # This lets reverse-proxied deployments work without explicit config
        # so long as the proxy preserves Host.
        host_header = request.headers.get("host") or ""
        scheme = request.url.scheme
        if origin and host_header and origin == f"{scheme}://{host_header}".rstrip("/"):
            return await call_next(request)

        # Reject. We use 403, not 401, to make this distinguishable from
        # "no credentials" — the request HAD a session cookie, we just
        # refused to trust the origin.
        return JSONResponse(
            status_code=403,
            content={
                "error": "csrf_origin_check_failed",
                "detail": f"Origin {origin!r} not in CSRF allowlist for cookie-authenticated request",
            },
        )


def _needs_csrf_check(request: Request) -> bool:
    """Return True iff this request is cookie-authenticated AND state-mutating AND not exempt."""
    method = request.method.upper()
    if method not in STATE_MUTATING_METHODS:
        return False
    path = request.url.path
    if path in CSRF_EXEMPT_PATHS:
        return False
    # No session cookie → request relies on bearer/JWT, which is not
    # auto-attached by the browser. Skip CSRF gate.
    if COOKIE_NAME not in request.cookies:
        return False
    return True


__all__ = [
    "COOKIE_NAME",
    "CSRF_ALLOWLIST_ENV",
    "CSRF_EXEMPT_PATHS",
    "DEFAULT_CSRF_ALLOWLIST",
    "STATE_MUTATING_METHODS",
    "WhillySessionCSRFMiddleware",
]


# Mark Response as intentionally re-exported so tests / callers can import
# starlette's Response from here if they want a single import root.
_ = Response
