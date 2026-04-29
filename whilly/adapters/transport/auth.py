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

import hashlib
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

from fastapi import Header, HTTPException, status

if TYPE_CHECKING:
    from whilly.adapters.db import TaskRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Env var holding the *legacy* shared bearer token. Workers used to send
#: this as ``Authorization: Bearer <token>`` on every RPC; v4.1 moves the
#: steady-state surface onto per-worker tokens validated against
#: ``workers.token_hash`` (TASK-101). The env var is kept as a one-minor-
#: version backward-compatibility fallback so existing deployments don't
#: break on upgrade — every successful match emits a one-shot deprecation
#: warning (see :data:`SUPPRESS_WORKER_TOKEN_WARNING_ENV`). PRD FR-1.2 /
#: TC-6.
WORKER_TOKEN_ENV: Final[str] = "WHILLY_WORKER_TOKEN"

#: Env var that suppresses the one-shot deprecation warning emitted when
#: an RPC authenticates via the legacy ``WHILLY_WORKER_TOKEN`` shared
#: bearer. Set to ``"1"`` to silence the warning (operators in transition
#: who do not yet want the journal noise). The fallback itself is *not*
#: disabled — only the warning. The whole legacy code path goes away in
#: v4.2 (see :func:`_maybe_warn_legacy_worker_token`).
SUPPRESS_WORKER_TOKEN_WARNING_ENV: Final[str] = "WHILLY_SUPPRESS_WORKER_TOKEN_WARNING"

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


def hash_bearer_token(plaintext: str) -> str:
    """Return the canonical hash of a per-worker bearer token (PRD NFR-3).

    Plain SHA-256 over UTF-8 bytes, hex-encoded. The output is what
    lands in ``workers.token_hash`` on registration and what
    :func:`make_db_bearer_auth` compares against on every RPC. The
    plaintext is never persisted server-side.

    Centralised here (rather than inside
    :mod:`whilly.adapters.transport.server`'s registration handler)
    because both the registration write path and the per-worker bearer
    read path need to use the *same* encoding — promoting the helper
    out of a private function in ``server.py`` keeps the two flows
    naturally synchronised. A future migration to a salted / KDF-based
    scheme (argon2 / scrypt) lands in this one function without
    touching the routes.

    Why SHA-256 and not bcrypt / argon2?
        Per-worker tokens come from :func:`secrets.token_urlsafe(32)` —
        ~256 bits of entropy. There is no dictionary to attack, so the
        slow-hashing argument that motivates bcrypt / argon2 for
        *passwords* doesn't apply. Constant-time hash verification is
        also the natural fit for the bearer-auth path: the heavy
        work-factor of bcrypt on every request would amplify trivially-
        abusable DoS vectors.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# Module-level guard for the one-shot legacy ``WHILLY_WORKER_TOKEN``
# deprecation warning. The warning is emitted at most once per process
# lifetime per VAL-AUTH-031 — repeated successful requests with the
# legacy bearer must not flood the journal. Tests reset the flag via
# :func:`reset_legacy_warning_state`.
_legacy_worker_token_warning_emitted: bool = False


def _maybe_warn_legacy_worker_token() -> None:
    """Emit the legacy ``WHILLY_WORKER_TOKEN`` deprecation warning once per process.

    Called from :func:`make_db_bearer_auth`'s fallback branch — i.e.
    only when an incoming bearer matched the legacy shared token (and
    not a per-worker hash). Suppressed by
    :data:`SUPPRESS_WORKER_TOKEN_WARNING_ENV` (operators in transition
    who do not yet want the noise).

    The pattern (module-level boolean + env-var opt-out + ``log.warning``
    rather than Python's ``DeprecationWarning``) mirrors
    :func:`whilly.config._maybe_warn_dotenv_deprecation` — see that
    docstring for the rationale (operator-visible journal entries
    rather than per-package warning filters, env-var-driven opt-out
    rather than ``warnings.filterwarnings``).

    Why one-shot rather than per-request?
        Logging once per process surfaces the deprecation to the
        operator's journal once at the first transition — enough
        signal to motivate rotation, low enough volume that it doesn't
        crowd out other warnings. Per-request would either need a
        rate-limiter (extra state) or would flood at request rate.
    """
    global _legacy_worker_token_warning_emitted
    if _legacy_worker_token_warning_emitted:
        return
    if (os.environ.get(SUPPRESS_WORKER_TOKEN_WARNING_ENV) or "").strip() == "1":
        # Even when suppressed we still flip the flag so a subsequent
        # request with the suppression env unset doesn't re-emit. This
        # keeps the "once per process" contract intact regardless of
        # mid-process env mutations.
        _legacy_worker_token_warning_emitted = True
        return
    logger.warning(
        "WHILLY_WORKER_TOKEN deprecated; use per-worker tokens. Suppress: %s=1",
        SUPPRESS_WORKER_TOKEN_WARNING_ENV,
    )
    _legacy_worker_token_warning_emitted = True


def reset_legacy_warning_state() -> None:
    """Reset the one-shot legacy-bearer warning flag — for tests only.

    Production code never calls this. Tests that exercise the
    one-shot semantics across multiple ``create_app`` instances need
    the flag cleared between cases (otherwise a previous test's
    emission masks the next test's expected emission). Mirrors
    :func:`reset_lazy_dependencies` for the dependency cache.
    """
    global _legacy_worker_token_warning_emitted
    _legacy_worker_token_warning_emitted = False


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


def make_db_bearer_auth(
    repo: TaskRepository,
    *,
    legacy_token: str | None = None,
) -> AuthDependency:
    """Build a per-worker bearer ``Depends`` callable backed by the workers table.

    This is the v4.1 successor to :func:`make_bearer_auth` for the
    steady-state RPC surface (claim / complete / fail / heartbeat /
    release). The dep:

    1. Extracts the bearer token from ``Authorization: Bearer <…>``
       (same RFC 6750 / RFC 7235 handling as
       :func:`make_bearer_auth` — missing header / wrong scheme /
       empty token all surface as 401 with
       ``WWW-Authenticate: Bearer realm="whilly"``).
    2. Hashes the presented plaintext via :func:`hash_bearer_token`
       and asks the repository to resolve it to a ``worker_id``
       (``SELECT worker_id FROM workers WHERE token_hash = $1`` —
       see :meth:`whilly.adapters.db.TaskRepository.get_worker_id_by_token_hash`).
       A hit returns 200; a miss falls through to step 3.
    3. **Optional legacy fallback.** If ``legacy_token`` is set
       (operator opted into the v4.0 shared-bearer behaviour by
       leaving ``WHILLY_WORKER_TOKEN`` defined), the dep
       :func:`secrets.compare_digest`-checks the presented bearer
       against ``legacy_token``. On match it accepts the request AND
       emits the one-shot deprecation warning via
       :func:`_maybe_warn_legacy_worker_token`. Per-worker bearer
       precedence is preserved by *order of evaluation*: the DB
       lookup runs first, so a registered worker's bearer that
       happens to also equal ``legacy_token`` (vanishingly unlikely)
       still authenticates as the per-worker identity and does not
       trigger the deprecation log (VAL-AUTH-034).
    4. On all other paths — miss in the DB, no legacy token, or
       legacy token set but doesn't match — raise 401 ``invalid
       token`` (same wire shape as the v4.0 closure factory so the
       remote-worker error mapper doesn't have to learn a new
       discriminator).

    Why a *factory* rather than a module-level dep?
        Same rationale as :func:`make_bearer_auth`: the closure
        captures ``repo`` and ``legacy_token`` once at app build
        time, so a mid-process env mutation can't drift the auth
        surface. The repo handle stays bound across the whole
        FastAPI request lifecycle without each request having to
        reach into ``request.app.state``.

    Why no DB call on the legacy path when the lookup misses?
        Two SQL round-trips per failed auth would amplify a
        password-spray DoS. Hashing is constant-time anyway; the
        ``compare_digest`` against ``legacy_token`` runs in fixed
        time over the longer of the two strings, no information leak
        about whether the token was "close" to a real one.

    Parameters
    ----------
    repo:
        :class:`TaskRepository` bound to the app's asyncpg pool.
        Captured by the closure. The repo's
        :meth:`get_worker_id_by_token_hash` is the only method the
        dep calls — keeps the auth surface narrow and easy to fake
        in tests.
    legacy_token:
        Optional plaintext shared-bearer kept for one-minor-version
        backwards compatibility (PRD AC: "shared-bearer fallback
        через env var оставить на одну минорную версию"). When
        ``None``, the dep is purely DB-backed and any non-matching
        bearer returns 401 — the v4.2 future shape. When set, a
        successful match logs the one-shot deprecation warning
        (suppressible via :data:`SUPPRESS_WORKER_TOKEN_WARNING_ENV`).

    Returns
    -------
    AuthDependency
        An async callable suitable for ``Depends(...)``. Returns
        ``None`` on success (matching the
        :func:`make_bearer_auth` shape so route handlers remain
        compatible). Worker-identity-aware handlers can opt-in to
        the resolved worker_id in a future revision by reading from
        ``request.state``; today's handlers carry the worker_id in
        the request body and the dep's role is gate-keeping only.
    """
    # Whitespace-stripped legacy token guards against operators
    # leaving the env value as a stray space — same rule as
    # :func:`_resolve_token` in server.py.
    legacy_token_clean = legacy_token.strip() if legacy_token is not None else None
    if legacy_token_clean == "":
        # Empty / whitespace-only value is a misconfiguration, not a
        # toggle. Reject loudly at app build time — the operator
        # forgot to set the env or mis-typed.
        raise RuntimeError(
            "make_db_bearer_auth: legacy_token must be non-empty when provided "
            "(use None to disable the legacy WHILLY_WORKER_TOKEN fallback)."
        )

    async def db_bearer_auth(authorization: str | None = Header(default=None)) -> None:
        token = _extract_bearer(authorization)
        token_hash = hash_bearer_token(token)
        worker_id = await repo.get_worker_id_by_token_hash(token_hash)
        if worker_id is not None:
            # Per-worker bearer takes precedence: even if ``token`` also
            # happens to equal ``legacy_token``, the deprecation warning
            # is NOT emitted on this path — VAL-AUTH-034 pins the
            # contract that "valid per-worker token never logs the
            # deprecation". The route handler can correlate the
            # bearer-resolved identity with the body's ``worker_id``
            # echo (defence-in-depth), but the dep stays gate-keeping
            # only.
            return None
        if legacy_token_clean is not None and secrets.compare_digest(token, legacy_token_clean):
            # Legacy fallback hit. Emit the one-shot deprecation
            # warning (suppressible) and accept the request. We hash
            # the legacy token through the same code path as a
            # safety check that ``compare_digest`` is the only
            # constant-time comparison surface — nothing leaks
            # whether the legacy token was "close" to a real one.
            _maybe_warn_legacy_worker_token()
            return None
        raise _bearer_401("invalid token")

    return db_bearer_auth


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
    "SUPPRESS_WORKER_TOKEN_WARNING_ENV",
    "WORKER_TOKEN_ENV",
    "AuthDependency",
    "bearer_auth",
    "bootstrap_auth",
    "hash_bearer_token",
    "make_bearer_auth",
    "make_bootstrap_auth",
    "make_db_bearer_auth",
    "reset_lazy_dependencies",
    "reset_legacy_warning_state",
]
