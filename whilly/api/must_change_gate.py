"""Per-request ``must_change_password`` gate (PRD §Epic C, Item 6).

When a user signed in with ``must_change_password=True`` makes ANY request
other than the small whitelist below, the gate returns ``303 → /auth/change-
password``. This stops the "navigate-away-after-login" bypass: the password-
change page is rendered once after a successful login, but nothing prevents
the user from typing ``/`` into the URL bar before completing the change.
The middleware enforces the redirect on every request, not just the one
right after login.

Whitelist (request passes through unchanged):
    * ``/auth/change-password`` — the change-password form + POST itself.
    * ``/auth/logout``          — never trap a user who wants to leave.
    * ``/auth/login``, ``/auth/magic``, ``/auth/magic-login`` — entry
      points needed to *get* a session in the first place.
    * ``/health``               — liveness probes.
    * ``/static/*``             — CSS, JS, images.

Cookie-unauthenticated requests (worker bearer, dashboard JWT) carry no
``whilly_session`` cookie and pass through with no DB round-trip — the gate
has no opinion on machine-to-machine auth paths.

Caching
-------
The gate caches the ``must_change_password`` verdict per ``session_id``
with a 30 s TTL in a process-local dict. Without caching every authenticated
request would issue two DB round-trips (``verify_session`` +
``get_user_by_username``); with the cache the steady state is one in 30 s.

The PRD specifies the cache key as ``(session_id, password_version)``
to auto-invalidate on password change. The current ``users`` schema has
no ``password_version`` column, so the cache is keyed on ``session_id``
alone and invalidation is performed *explicitly* by the change-password
POST handler calling :func:`invalidate_session` right after a successful
``set_password``. A future schema migration that adds
``users.password_version`` could swap to the PRD's two-tuple key without
changing the public API of this module — :func:`invalidate_session` would
become a no-op and the cache TTL could grow.

Middleware ordering
-------------------
This middleware is registered BEFORE :class:`WhillySessionCSRFMiddleware`
in :func:`whilly.adapters.transport.server.create_app`. In Starlette,
``add_middleware`` is LIFO — the *last* middleware added is the
*outermost* on the request path. So CSRF (added last) runs first; a
bad-Origin POST is rejected with 403 before the gate ever queries the DB.
The gate then runs on already-CSRF-validated requests. This ordering is
verified by ``tests/unit/test_must_change_gate.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

import asyncpg
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp

from whilly.api import auth_tokens, sessions
from whilly.api.csrf import COOKIE_NAME, _HOST_PREFIX_COOKIE_NAME
from whilly.api.users_repo import get_user_by_session_email

logger = logging.getLogger(__name__)

#: Target of the redirect when the gate fires.
CHANGE_PASSWORD_PATH: Final[str] = "/auth/change-password"

#: Paths exempted from the gate by exact match. ``/auth/login`` etc. are
#: included so an already-logged-in-but-must-change user can still hit
#: them without infinite redirect loops on edge cases (e.g. opening
#: ``/auth/login`` in a second tab while the first tab is sitting on the
#: change-password form).
GATE_EXEMPT_EXACT: Final[frozenset[str]] = frozenset(
    {
        CHANGE_PASSWORD_PATH,
        "/auth/logout",
        "/auth/login",
        "/auth/magic",
        "/auth/magic-login",
        "/health",
    }
)

#: Path *prefixes* exempted from the gate. ``/static/`` covers the CSS,
#: JS and image bundle the login + change-password forms depend on.
GATE_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/static/",)

#: Cache TTL in seconds. PRD specifies 30 s as the maximum staleness
#: window between a password change and the cached verdict expiring.
#: Combined with explicit invalidation on password change, this is the
#: tail-latency bound for stale verdicts during failure scenarios (e.g.
#: when ``invalidate_session`` is somehow skipped).
CACHE_TTL_SECONDS: Final[float] = 30.0

#: Process-local cache: ``session_id`` → ``(must_change, expires_at_monotonic)``.
#: A bare dict is safe under the GIL for the single read + single write
#: pattern below; no async lock is required and adding one would only slow
#: down the cache-hit path the gate exists to optimise.
_cache: dict[str, tuple[bool, float]] = {}


def invalidate_session(session_id: str) -> None:
    """Drop the cached ``must_change_password`` verdict for ``session_id``.

    Called by the change-password POST handler immediately after a
    successful :func:`whilly.api.users_repo.set_password`. Without this
    the cached ``True`` verdict would persist for up to
    :data:`CACHE_TTL_SECONDS` and the user would loop on the change-
    password form even though the flag is already ``False`` in the DB.

    Idempotent — calling on a session that has no cache entry is a no-op,
    so the change-password handler does not need to know whether the gate
    has seen this session yet.
    """
    _cache.pop(session_id, None)


def _clear_cache() -> None:
    """Reset the entire cache. Test-only helper, not exported."""
    _cache.clear()


def _is_exempt_path(path: str) -> bool:
    """Return True iff ``path`` is on the gate's whitelist."""
    if path in GATE_EXEMPT_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in GATE_EXEMPT_PREFIXES)


def _extract_session_id(request: Request, *, secret: bytes, cookie_name: str) -> str | None:
    """Pull and verify the ``session_id`` from the request cookie.

    Returns ``None`` on any failure — missing cookie, bad signature,
    expired, malformed claims. The gate fail-opens in every failure mode:
    a request the gate cannot make sense of is left to the route handlers
    (which run their own authentication and will return 401 if needed).
    """
    cookie_raw = request.cookies.get(cookie_name) or request.cookies.get(_HOST_PREFIX_COOKIE_NAME)
    if not cookie_raw:
        return None
    try:
        claims = auth_tokens.verify_session_cookie_value(cookie_raw, secret)
    except auth_tokens.AuthTokenError:
        return None
    sid = claims.get("sid")
    if not isinstance(sid, str) or not sid:
        return None
    return sid


class MustChangePasswordGateMiddleware(BaseHTTPMiddleware):
    """Redirect every cookie-authenticated request to the change-password
    form while the signed-in user has ``must_change_password=True``.

    See module docstring for the design rationale, whitelist contents,
    caching strategy, and middleware ordering requirements.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        pool: asyncpg.Pool,
        secret: bytes,
        cookie_name: str = COOKIE_NAME,
    ) -> None:
        super().__init__(app)
        self._pool = pool
        self._secret = secret
        self._cookie_name = cookie_name

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Fast path 1: whitelisted path. No cookie inspection, no DB.
        if _is_exempt_path(request.url.path):
            return await call_next(request)

        # Fast path 2: no session cookie → request relies on bearer/JWT.
        # The gate has no opinion on machine-to-machine traffic.
        sid = _extract_session_id(request, secret=self._secret, cookie_name=self._cookie_name)
        if sid is None:
            return await call_next(request)

        # Slow path: cached or DB lookup for the must_change verdict.
        must_change = await self._must_change_for_session(sid)
        if must_change:
            return RedirectResponse(url=CHANGE_PASSWORD_PATH, status_code=303)
        return await call_next(request)

    async def _must_change_for_session(self, session_id: str) -> bool:
        """Return ``True`` iff the signed-in user has
        ``must_change_password=True``. 30 s TTL cache.
        """
        now = time.monotonic()
        cached = _cache.get(session_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        verdict = await self._lookup_must_change(session_id)
        _cache[session_id] = (verdict, now + CACHE_TTL_SECONDS)
        return verdict

    async def _lookup_must_change(self, session_id: str) -> bool:
        """One round-trip to ``sessions`` and one to ``users``.

        Fail-open on any exception or missing row — the gate never breaks
        the request lifecycle when the DB hiccups. The route handlers
        downstream will return 401 if the session is genuinely invalid.
        """
        try:
            session = await sessions.verify_session(self._pool, session_id=session_id)
        except Exception:  # noqa: BLE001 — gate must never crash the request
            logger.warning(
                "must_change_gate: verify_session raised for sid=%r — fail-open",
                session_id,
                exc_info=True,
            )
            return False
        if session is None:
            return False
        # Resolve the user via the single canonical resolver, which handles both
        # the synthetic ``<username>@local`` and a real email (e.g. the seeded
        # admin ``admin@whilly.local``, whose email does NOT round-trip to a
        # username). Resolving real emails is what stops the gate silently
        # bypassing must-change for that account; a magic-link user with no
        # ``users`` row still resolves to None → fail-open (correct).
        try:
            user = await get_user_by_session_email(self._pool, session_email=session.email)
        except Exception:  # noqa: BLE001 — gate must never crash the request
            logger.warning(
                "must_change_gate: user lookup raised for session email %r — fail-open",
                session.email,
                exc_info=True,
            )
            return False
        if user is None:
            # Magic-link user with no ``users`` row, ambiguous email, or stale
            # session whose user has been deleted. Pass through; downstream
            # handlers cope.
            return False
        return bool(user.must_change_password)


__all__ = [
    "CACHE_TTL_SECONDS",
    "CHANGE_PASSWORD_PATH",
    "GATE_EXEMPT_EXACT",
    "GATE_EXEMPT_PREFIXES",
    "MustChangePasswordGateMiddleware",
    "invalidate_session",
]
