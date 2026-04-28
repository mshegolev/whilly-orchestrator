"""FastAPI bearer-auth dependencies for the worker ↔ control-plane HTTP API.

This module owns the *edge* of authentication: the FastAPI ``Depends``
callables that every worker-facing route hangs off. It deliberately stays
small and side-effect-free so the route layer in
:mod:`whilly.adapters.transport.server` (TASK-021a3) can compose auth
without ever touching the env directly.

Two surfaces, two env vars
--------------------------
=========================  ============================  =========================================
Dependency                 Env var                       Used by
=========================  ============================  =========================================
:func:`bearer_auth`        ``WHILLY_WORKER_TOKEN``       claim / complete / fail / heartbeat (TASK-021b/c)
:func:`bootstrap_auth`     ``WHILLY_WORKER_BOOTSTRAP_TOKEN``  ``POST /workers/register`` (TASK-021b)
=========================  ============================  =========================================

The split is intentional: a shared bootstrap secret is what lets a fresh
worker box join the cluster (it has no credentials of its own yet), while
the per-worker bearer token is what every steady-state RPC carries. Keeping
them in separate env vars means an operator can rotate the bootstrap secret
(e.g. after a compromise of the deploy artefact) without invalidating every
already-running worker's bearer — and vice versa.

Why ``secrets.compare_digest`` instead of ``==``
-------------------------------------------------
Plain string equality short-circuits on the first mismatched byte. An
attacker who can time the response can probe the token byte-by-byte to
recover it. :func:`secrets.compare_digest` runs in constant time over the
longer of the two inputs — the extra cycles are free relative to the HTTP
round-trip and the timing leak is closed off by construction. This matters
even for a "shared static token": treating bearer comparison as carefully as
password comparison is the cheap, obviously-correct default.

Why dependencies are factory functions, not module-level callables
------------------------------------------------------------------
``bearer_auth`` / ``bootstrap_auth`` could be plain functions that read the
env on every request. Instead they're returned by factories
(:func:`make_bearer_auth` / :func:`make_bootstrap_auth`) that read the env
*once* at app construction. Three reasons:

1. **Test isolation.** Tests can build a dependency bound to a specific
   token without mutating ``os.environ`` (and racing with other tests).
2. **Fast-fail at startup.** A missing token raises during
   :func:`whilly.adapters.transport.server.create_app` (TASK-021a3), not on
   the first 401 in production — config errors surface before traffic.
3. **No silent fallback.** Reading on every request invites
   "if env was set, accept; if not, accept all" patterns. A factory binds
   the value once and the fast path stops worrying about reconfiguration.

The module also re-exports module-level :data:`bearer_auth` /
:data:`bootstrap_auth` shims that lazy-initialise from the env on first use.
These are convenient for routes that haven't been wired through
:func:`create_app` yet (early prototyping, ad-hoc scripts), but production
code should always go through :func:`make_bearer_auth` /
:func:`make_bootstrap_auth` so the missing-env error surfaces at startup.

Why 401 (not 403)
-----------------
RFC 7235 / RFC 6750 §3: a missing or invalid bearer is a 401 with
``WWW-Authenticate: Bearer realm="whilly"``. 403 means "I know who you are,
you can't do this" — but we don't know who they are if the token is wrong.
FastAPI's :class:`HTTPException` doesn't set the header by default, so the
helper :func:`_bearer_401` builds it explicitly.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Header, HTTPException, status

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Env var holding the per-worker bearer token. Workers send it as
#: ``Authorization: Bearer <token>`` on every RPC after they've registered.
#: Validated by :func:`bearer_auth`. PRD FR-1.2 / TC-6.
WORKER_TOKEN_ENV: Final[str] = "WHILLY_WORKER_TOKEN"

#: Env var holding the cluster-join secret. Required to call
#: ``POST /workers/register`` (TASK-021b) — i.e. before a worker has its own
#: bearer. Validated by :func:`bootstrap_auth`. PRD FR-1.2 / TC-6.
BOOTSTRAP_TOKEN_ENV: Final[str] = "WHILLY_WORKER_BOOTSTRAP_TOKEN"

#: ``Authorization: Bearer <token>`` prefix per RFC 6750. Case-insensitive
#: per the spec, so :func:`_extract_bearer` does a lower-cased comparison
#: but preserves the suffix verbatim (tokens are case-sensitive).
_BEARER_PREFIX: Final[str] = "bearer "

#: Realm label included in ``WWW-Authenticate`` on 401 responses. Identifies
#: the protection space (RFC 7235 §2.2) — clients that cache credentials
#: per-realm see this as the namespace.
_BEARER_REALM: Final[str] = "whilly"

# Public type alias for the dependency callables this module produces. A
# FastAPI dependency is any callable; ours are async because they may grow
# DB lookups in TASK-021b (per-worker token-hash verification) without a
# breaking signature change.
AuthDependency = Callable[[str | None], Awaitable[None]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer_401(detail: str) -> HTTPException:
    """Build a 401 ``HTTPException`` with the RFC 6750 ``WWW-Authenticate`` header.

    FastAPI's default :class:`HTTPException` doesn't set
    ``WWW-Authenticate``; without it, well-behaved HTTP clients (httpx,
    curl ``--anyauth``) won't recognise the response as a bearer-protected
    resource and won't prompt for / retry with credentials. The header value
    follows RFC 6750 §3: ``Bearer realm="<realm>"``.

    ``detail`` flows into the JSON body's ``detail`` field — it's safe to
    return short, generic strings ("missing bearer token", "invalid
    token") because the client already knows *what* failed (it sent or
    didn't send a header) and we never want to leak whether the token was
    "close" to a real one.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": f'Bearer realm="{_BEARER_REALM}"'},
    )


def _extract_bearer(authorization: str | None) -> str:
    """Pull the raw token out of an ``Authorization: Bearer <token>`` header.

    Returns the token suffix on success; raises 401 on:

    * missing / empty header — there's nothing to authenticate with;
    * non-Bearer scheme (``Basic ...``, ``Digest ...``) — clients sending a
      different scheme are misconfigured and a clear 401 surfaces the
      mismatch faster than silently accepting / rejecting based on coincidence;
    * empty token after the prefix (``Authorization: Bearer ``) — the empty
      string would otherwise pass the constant-time comparison against
      another empty token, so we reject it before it reaches
      :func:`secrets.compare_digest`.

    The scheme check is case-insensitive (RFC 7235 §2.1) but the token is
    preserved verbatim — bearer tokens are opaque case-sensitive byte
    strings and folding case would corrupt them.
    """
    if authorization is None:
        raise _bearer_401("missing bearer token")
    # ``str.startswith`` is case-sensitive, but the scheme is case-insensitive
    # by spec. Lower-casing the *prefix slice* of the header (not the whole
    # value, so the token suffix survives) is the cheapest correct check.
    if not authorization[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
        raise _bearer_401("invalid authorization scheme")
    token = authorization[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise _bearer_401("empty bearer token")
    return token


def _read_required_env(name: str) -> str:
    """Read a required env var or raise ``RuntimeError`` at config time.

    Used by the dependency factories — the failure is a *configuration*
    error (operator forgot to set ``WHILLY_WORKER_TOKEN``), not a request
    error, so ``RuntimeError`` is correct: it surfaces during
    :func:`create_app` and aborts startup. Surfacing it as a 500 on the
    first request would be misleading (the server is healthy, the env
    isn't) and would let one mis-deployed control plane silently accept
    every request as anonymous before the operator notices.

    The env var must also be non-empty after stripping whitespace —
    ``WHILLY_WORKER_TOKEN=`` (set to empty) is a misconfiguration, not a
    deliberate "auth disabled" toggle. There is no way to turn auth off
    by design (PRD FR-1.2): if you want a single-tenant unauthenticated
    deployment, run the worker in-process via :mod:`whilly.cli.run`
    (TASK-019c) instead of going over HTTP.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"environment variable {name} is required for HTTP transport auth; "
            f"set it on the control-plane process (and matching client config) "
            f"before calling create_app(). See whilly/adapters/transport/auth.py "
            f"docstring for the bootstrap vs per-worker token split."
        )
    return value


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------


def make_bearer_auth(expected_token: str) -> AuthDependency:
    """Build a FastAPI ``Depends`` that gates routes on a per-worker token.

    The returned coroutine reads the ``Authorization`` header (via FastAPI's
    :func:`Header` injection — declaring the parameter is what makes
    FastAPI populate it) and constant-time-compares the bearer suffix
    against ``expected_token``. On mismatch / missing / malformed: 401
    with a proper ``WWW-Authenticate`` header.

    Wiring example (used by TASK-021b/c routes)::

        from fastapi import FastAPI, Depends
        bearer = make_bearer_auth(os.environ["WHILLY_WORKER_TOKEN"])

        @app.post("/tasks/claim", dependencies=[Depends(bearer)])
        async def claim(...): ...

    Why a factory (closure) instead of reading env in the dep itself: the
    expected token is captured *once* at app build time. Changing the env
    after startup has no effect; this is a feature, not a bug — auth
    config should be static for the lifetime of the process so we don't
    have to reason about half-rotated state.
    """
    if not expected_token:
        # Defensive: callers should already have validated via
        # _read_required_env, but accepting an empty string here would let
        # any client through (compare_digest("", "") is True).
        raise RuntimeError("make_bearer_auth: expected_token must be non-empty")

    async def bearer_auth(authorization: str | None = Header(default=None)) -> None:
        token = _extract_bearer(authorization)
        if not secrets.compare_digest(token, expected_token):
            raise _bearer_401("invalid token")

    return bearer_auth


def make_bootstrap_auth(expected_token: str) -> AuthDependency:
    """Build a FastAPI ``Depends`` that gates ``POST /workers/register``.

    Mechanically identical to :func:`make_bearer_auth` — same header
    parsing, same constant-time comparison, same 401 shape — but bound to
    the *bootstrap* secret (``WHILLY_WORKER_BOOTSTRAP_TOKEN``) rather than
    the per-worker token.

    They look the same on the wire on purpose: a fresh worker, before it
    has its own credentials, sends the bootstrap secret as a regular
    ``Authorization: Bearer <token>`` header. This keeps the worker's
    HTTP layer simple (one auth path on the wire) and the server's split
    is a route-level concern: ``/workers/register`` uses
    :func:`bootstrap_auth`, every other worker route uses
    :func:`bearer_auth`.

    A different secret (rather than reusing the per-worker token) means
    cluster-join is a separate capability: an operator can rotate the
    bootstrap secret to lock out new workers during an incident without
    revoking already-issued per-worker tokens — and vice versa.
    """
    if not expected_token:
        raise RuntimeError("make_bootstrap_auth: expected_token must be non-empty")

    async def bootstrap_auth(authorization: str | None = Header(default=None)) -> None:
        token = _extract_bearer(authorization)
        if not secrets.compare_digest(token, expected_token):
            raise _bearer_401("invalid bootstrap token")

    return bootstrap_auth


# ---------------------------------------------------------------------------
# Module-level lazy shims
# ---------------------------------------------------------------------------
#
# The factories above are the production path: TASK-021a3's ``create_app``
# will read the env once and bind a closure. But many call sites (early
# tests, ad-hoc scripts, ``import whilly.adapters.transport.auth as auth;
# Depends(auth.bearer_auth)`` in a one-off route) want a direct callable.
#
# These shims read the env on first use and cache the bound closure for
# the lifetime of the process. They behave exactly like the factory output
# at request time — the only difference is *when* the env is read. Tests
# that need to re-bind across env mutations should either:
#
# * use the explicit ``make_bearer_auth`` factory, or
# * call :func:`reset_lazy_dependencies` to clear the cache between cases.


_lazy_bearer: AuthDependency | None = None
_lazy_bootstrap: AuthDependency | None = None


async def bearer_auth(authorization: str | None = Header(default=None)) -> None:
    """Lazy module-level :func:`make_bearer_auth` bound to ``WHILLY_WORKER_TOKEN``.

    First call reads :data:`WORKER_TOKEN_ENV` and caches the closure.
    Subsequent calls hit the cached closure directly. Tests that need to
    re-bind should call :func:`reset_lazy_dependencies` first.
    """
    global _lazy_bearer
    if _lazy_bearer is None:
        _lazy_bearer = make_bearer_auth(_read_required_env(WORKER_TOKEN_ENV))
    await _lazy_bearer(authorization)


async def bootstrap_auth(authorization: str | None = Header(default=None)) -> None:
    """Lazy module-level :func:`make_bootstrap_auth` bound to ``WHILLY_WORKER_BOOTSTRAP_TOKEN``.

    Mirrors :func:`bearer_auth` for the bootstrap secret. Same lifecycle
    semantics: env read once, then cached.
    """
    global _lazy_bootstrap
    if _lazy_bootstrap is None:
        _lazy_bootstrap = make_bootstrap_auth(_read_required_env(BOOTSTRAP_TOKEN_ENV))
    await _lazy_bootstrap(authorization)


def reset_lazy_dependencies() -> None:
    """Clear cached lazy bindings — for tests that mutate auth env vars.

    Production code never calls this: the lazy shims are meant to be
    bound-once. The ``create_app`` path (TASK-021a3) doesn't go through
    them at all — it uses the factory functions directly.
    """
    global _lazy_bearer, _lazy_bootstrap
    _lazy_bearer = None
    _lazy_bootstrap = None


__all__ = [
    "BOOTSTRAP_TOKEN_ENV",
    "WORKER_TOKEN_ENV",
    "AuthDependency",
    "bearer_auth",
    "bootstrap_auth",
    "make_bearer_auth",
    "make_bootstrap_auth",
    "reset_lazy_dependencies",
]
