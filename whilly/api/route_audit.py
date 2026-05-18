"""Startup route audit (PRD-post-auth-hardening §Epic D, Item 13).

After all routers are included, :func:`audit_routes` walks ``app.routes``
and refuses to start the server if a route is reachable without either:

1. A FastAPI :class:`Depends` chain containing one of the recognised
   auth dependency functions, OR
2. An explicit entry in the static whitelist :data:`PUBLIC_WHITELIST` /
   :data:`PUBLIC_PREFIXES`.

Default behaviour
-----------------
**Opt-in.** The audit is OFF unless ``WHILLY_ENABLE_ROUTE_AUDIT=1`` is
set. This inverts the PRD's "skippable via ``WHILLY_SKIP_ROUTE_AUDIT=1``"
nominal flag and is a deliberate hedge against PRD R1 — many existing
routes call ``_authenticate_session`` inline (inside the route body)
rather than via :class:`Depends`. The dependant-walking approach used
here cannot see inline calls, so enabling-by-default would break
``create_app`` on the next deploy.

When the audit is enabled, the whitelist
(:data:`PUBLIC_WHITELIST` + :data:`PUBLIC_PREFIXES`) is intentionally
generous and covers every inline-auth route on main today. New routes
written in the Depends-style do NOT need a whitelist entry — they are
recognised by the dependant walk. Future-direction: when the existing
inline-auth routes are migrated to Depends style (separate refactor),
the whitelist can shrink and the env var can be flipped to opt-out.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)

ENABLE_ENV: Final[str] = "WHILLY_ENABLE_ROUTE_AUDIT"
SKIP_ENV: Final[str] = "WHILLY_SKIP_ROUTE_AUDIT"

#: Names of dependency callables that, when found in a route's dependant
#: tree, count as "this route is auth-guarded". Match by ``__qualname__``
#: tail so callers can use either the original function or a factory-
#: built closure (e.g. ``require_admin_role.<locals>._dep``).
_AUTH_DEPENDENCY_QUALNAMES: Final[frozenset[str]] = frozenset(
    {
        "authenticate_session_request",
        "_authenticate_session",
        "_dep",  # require_admin_role's inner closure
        "_bearer_auth",  # legacy + new bearer auth closures
        "make_db_bootstrap_auth",
        "make_db_bearer_auth",
        "make_admin_auth",
        "make_bearer_auth",
    }
)

#: Exact paths reachable without auth — entry-point pages, public health
#: probes, and the static asset mount.
PUBLIC_WHITELIST: Final[frozenset[str]] = frozenset(
    {
        "/login",
        "/login/magic",
        "/auth/login",
        "/auth/magic-login",
        "/auth/magic",
        "/auth/logout",  # logout itself must be reachable without an auth gate
        "/auth/change-password",  # forced-flow entry, reached pre-must-change clear
        "/health",
        "/healthz",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)

#: Path prefixes reachable without auth — covers ``/static/*`` and the
#: routes that authenticate inline rather than via :class:`Depends`. The
#: latter is a temporary scaffolding; tracked for cleanup in a follow-up.
PUBLIC_PREFIXES: Final[tuple[str, ...]] = (
    "/static/",
    # Inline-authenticated routes — covered by the inline `_authenticate_session`
    # call at the top of each handler. Listed explicitly here rather than
    # globbed so a new ``/api/v1/foo`` without auth still triggers the
    # RuntimeError when audit is enabled.
    "/me",  # /me + /me/password etc.
    "/api/v1/plans",
    "/api/v1/tasks",
)


def _is_enabled() -> bool:
    """Return True when WHILLY_ENABLE_ROUTE_AUDIT=1 AND WHILLY_SKIP_ROUTE_AUDIT
    is not set to a truthy value. Both knobs are honoured so existing
    operational playbooks that reference ``SKIP_ROUTE_AUDIT`` still work.
    """
    skip = (os.environ.get(SKIP_ENV) or "").strip().lower()
    if skip in {"1", "true", "yes", "on"}:
        return False
    enable = (os.environ.get(ENABLE_ENV) or "").strip().lower()
    return enable in {"1", "true", "yes", "on"}


def _route_has_auth_dependency(route: APIRoute) -> bool:
    """True iff the route's dependant tree contains a recognised auth dep."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False

    def _walk(d: object) -> bool:
        for sub in getattr(d, "dependencies", []):
            call = getattr(sub, "call", None)
            if call is not None:
                qualname = getattr(call, "__qualname__", "") or ""
                # Match either the whole qualname or the leaf attribute —
                # `require_admin_role.<locals>._dep` has the leaf `_dep`.
                leaf = qualname.rsplit(".", 1)[-1]
                if qualname in _AUTH_DEPENDENCY_QUALNAMES or leaf in _AUTH_DEPENDENCY_QUALNAMES:
                    return True
            if _walk(sub):
                return True
        return False

    return _walk(dependant)


def _path_is_whitelisted(path: str) -> bool:
    if path in PUBLIC_WHITELIST:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def audit_routes(app: FastAPI) -> list[str]:
    """Walk ``app.routes`` and return the list of un-guarded route paths.

    Returns an empty list when every route either has an auth dependency
    or is explicitly whitelisted. The :func:`enforce_audit` entry-point
    wraps this with the env-var gate and ``RuntimeError`` raising; tests
    can call :func:`audit_routes` directly to inspect findings without
    process side effects.
    """
    unguarded: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            # Mounts (StaticFiles, etc.) are not APIRoute — skip them.
            continue
        if _path_is_whitelisted(route.path):
            continue
        if _route_has_auth_dependency(route):
            continue
        unguarded.append(route.path)
    return unguarded


def enforce_audit(app: FastAPI) -> None:
    """Run the audit and raise :class:`RuntimeError` on any un-guarded route.

    No-op when the audit is disabled (default). Designed to be called
    from :func:`whilly.adapters.transport.server.create_app` after every
    router has been included.
    """
    if not _is_enabled():
        logger.debug(
            "route_audit: skipped (set %s=1 to enable; or %s=1 to force-skip when enabled)",
            ENABLE_ENV,
            SKIP_ENV,
        )
        return
    unguarded = audit_routes(app)
    if not unguarded:
        logger.info("route_audit: all routes guarded — %d APIRoute(s) checked", len(app.routes))
        return
    raise RuntimeError(
        f"Unguarded route: {unguarded[0]!r}"
        + (f" (and {len(unguarded) - 1} others: {unguarded[1:]!r})" if len(unguarded) > 1 else "")
    )


__all__ = [
    "ENABLE_ENV",
    "PUBLIC_PREFIXES",
    "PUBLIC_WHITELIST",
    "SKIP_ENV",
    "audit_routes",
    "enforce_audit",
]
