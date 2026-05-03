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

from fastapi import Header, HTTPException, Request, status

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

# Public type alias for the *gate-keeping* dependency callables this module
# produces — :func:`make_bearer_auth` and :func:`make_bootstrap_auth`. Both
# receive only the ``Authorization`` header and return ``None`` on success.
# FastAPI dependencies are arbitrary callables; we keep the alias narrow so
# the call sites that don't need identity binding stay typed accordingly.
AuthDependency = Callable[[str | None], Awaitable[None]]

# Public type alias for the *identity-binding* dependency that
# :func:`make_db_bearer_auth` produces. The dep takes both
# :class:`fastapi.Request` (so it can stash the resolved
# ``worker_id`` on ``request.state.authenticated_worker_id`` for the
# route handler's :func:`_require_token_owner` check — see
# :mod:`whilly.adapters.transport.server`) and the ``Authorization``
# header. Returns ``None`` on success: the gate-keeping shape stays
# unchanged so existing handlers can keep declaring
# ``dependencies=[Depends(bearer_dep)]`` without wiring the dep result
# through a parameter.
#
# Why a separate alias?
#     Mixing the two signatures under a single type would force every
#     route that consumes :data:`AuthDependency` (e.g. tests that build
#     ad-hoc gate-keeping deps via :func:`make_bearer_auth`) to also
#     accept the wider :class:`Request` parameter. Splitting keeps each
#     factory's contract narrow and makes it obvious at a glance which
#     dependencies write to ``request.state`` and which don't.
IdentityBindingAuthDependency = Callable[[Request, str | None], Awaitable[None]]


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
    global _legacy_worker_token_warning_emitted, _legacy_bootstrap_token_warning_emitted
    _legacy_worker_token_warning_emitted = False
    _legacy_bootstrap_token_warning_emitted = False


_legacy_bootstrap_token_warning_emitted: bool = False


def _maybe_warn_legacy_bootstrap_token() -> None:
    """Emit the legacy ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` deprecation warning once per process.

    Called from :func:`make_db_bootstrap_auth` (and
    :func:`make_admin_auth`) when an incoming bearer matched the
    legacy shared bootstrap token (and not a per-operator
    ``bootstrap_tokens`` row). Mirrors
    :func:`_maybe_warn_legacy_worker_token` — once per process,
    suppressible via :data:`SUPPRESS_WORKER_TOKEN_WARNING_ENV` (kept
    intentionally shared with the worker-token suppression env so
    operators in transition only have to flip one switch).
    """
    global _legacy_bootstrap_token_warning_emitted
    if _legacy_bootstrap_token_warning_emitted:
        return
    if (os.environ.get(SUPPRESS_WORKER_TOKEN_WARNING_ENV) or "").strip() == "1":
        _legacy_bootstrap_token_warning_emitted = True
        return
    logger.warning(
        "WHILLY_WORKER_BOOTSTRAP_TOKEN env-var fallback is deprecated; "
        "mint per-operator bootstrap tokens via `whilly admin bootstrap mint`. "
        "Suppress: %s=1",
        SUPPRESS_WORKER_TOKEN_WARNING_ENV,
    )
    _legacy_bootstrap_token_warning_emitted = True


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
) -> IdentityBindingAuthDependency:
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

    Identity binding (TASK-101 scrutiny round-1 fix)
    ------------------------------------------------
    On a successful per-worker hash hit the dep stashes the resolved
    ``worker_id`` on ``request.state.authenticated_worker_id`` so the
    route handler's :func:`whilly.adapters.transport.server._require_token_owner`
    check can reject cross-worker calls (worker A's bearer used to act
    as worker B) with a 403 — VAL-AUTH-024. On the legacy
    ``WHILLY_WORKER_TOKEN`` fallback path, ``authenticated_worker_id`` is
    set to ``None`` (identity unknown — the shared cluster token cannot
    name a specific worker), and the route-level helper treats that as
    a no-op so the one-minor-version legacy compat window
    (VAL-AUTH-030/031/034) stays green. A dep raise (401) does not
    write to ``request.state`` — Starlette unwinds the request before
    a handler runs, so unauthenticated requests never observe a
    half-set state.

    Returns
    -------
    IdentityBindingAuthDependency
        An async callable suitable for ``Depends(...)`` that takes a
        :class:`fastapi.Request` and the ``Authorization`` header.
        Returns ``None`` on success (matching the
        :func:`make_bearer_auth` shape so handlers can continue to
        declare it as ``dependencies=[Depends(bearer_dep)]`` without
        consuming a return value); side-effect is the
        ``request.state.authenticated_worker_id`` write described
        above.
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

    async def db_bearer_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        token = _extract_bearer(authorization)
        token_hash = hash_bearer_token(token)
        # M2: single-round-trip identity lookup that also returns the
        # operator's ``owner_email`` so handlers (claim / heartbeat /
        # ...) can attribute audit events to the operator without an
        # extra SELECT. ``identity is None`` covers the same three
        # operationally distinct miss cases as the legacy
        # ``get_worker_id_by_token_hash`` (forged / revoked / row-
        # gone) and is treated as a 401 from the wire's perspective.
        # Fallback path: a fake repo (test) that only implements the
        # legacy ``get_worker_id_by_token_hash`` method still works,
        # with ``owner_email`` resolved as ``None``.
        identity_lookup = getattr(repo, "get_worker_identity_by_token_hash", None)
        if identity_lookup is not None:
            identity = await identity_lookup(token_hash)
        else:
            worker_id_only = await repo.get_worker_id_by_token_hash(token_hash)
            identity = (worker_id_only, None) if worker_id_only is not None else None
        if identity is not None:
            worker_id, owner_email = identity
            # Per-worker bearer takes precedence: even if ``token`` also
            # happens to equal ``legacy_token``, the deprecation warning
            # is NOT emitted on this path — VAL-AUTH-034 pins the
            # contract that "valid per-worker token never logs the
            # deprecation". Stash the DB-resolved identity on
            # ``request.state`` so the route handlers'
            # ``_require_token_owner`` helper can compare it against
            # the body / path identity and reject cross-worker calls
            # with a 403 (VAL-AUTH-024). ``owner_email`` is stashed
            # alongside (M2: VAL-M2-ADMIN-AUTH-011) so audit-event
            # payloads can attribute actions to the operator.
            request.state.authenticated_worker_id = worker_id
            request.state.authenticated_owner_email = owner_email
            return None
        if legacy_token_clean is not None and secrets.compare_digest(token, legacy_token_clean):
            # Legacy fallback hit. The shared ``WHILLY_WORKER_TOKEN``
            # cannot identify a specific worker, so we explicitly mark
            # the identity as unknown; the route-level
            # ``_require_token_owner`` helper treats ``None`` as a
            # no-op and lets the legacy bearer act as "any worker" for
            # one minor version (VAL-AUTH-030/031/034). Emit the
            # one-shot deprecation warning (suppressible).
            request.state.authenticated_worker_id = None
            request.state.authenticated_owner_email = None
            _maybe_warn_legacy_worker_token()
            return None
        raise _bearer_401("invalid token")

    return db_bearer_auth


def make_db_bootstrap_auth(
    repo: TaskRepository,
    *,
    legacy_token: str | None = None,
) -> IdentityBindingAuthDependency:
    """Build a per-operator bootstrap ``Depends`` callable backed by ``bootstrap_tokens``.

    M2 mission: replaces the single shared
    ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` env var with per-operator rows
    minted via ``whilly admin bootstrap mint`` (migration 009).
    Behaves like :func:`make_db_bearer_auth` for the bootstrap surface
    (``POST /workers/register``):

    1. Extract the bearer per RFC 6750 / RFC 7235.
    2. Hash the plaintext via :func:`hash_bootstrap_token` (same SHA-256
       digest the repo's mint path uses) and ask
       :meth:`TaskRepository.get_bootstrap_token_owner` to resolve it.
       A hit returns ``(owner_email, is_admin)`` — both stashed on
       ``request.state.bootstrap_owner_email`` /
       ``request.state.bootstrap_is_admin`` so the route handler
       (``register_worker``) can attribute the new ``workers`` row to
       the operator who minted the token.
    3. **Optional legacy fallback.** If ``legacy_token`` is set
       (operator left ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` defined for
       one-minor-version backwards compatibility), the dep
       :func:`secrets.compare_digest`-checks the presented bearer
       against ``legacy_token``. On match the dep accepts the request
       AND emits a one-shot deprecation warning via
       :func:`_maybe_warn_legacy_bootstrap_token`. On the legacy path
       ``request.state.bootstrap_owner_email`` is set to ``None`` and
       ``bootstrap_is_admin`` to ``False`` — the shared token cannot
       identify a specific operator and is never admin-scoped.
    4. Otherwise raise 401 ``invalid bootstrap token``.

    Why a separate factory rather than retrofitting
    :func:`make_bootstrap_auth`?
        Symmetry with the per-worker pair (``make_bearer_auth`` vs.
        ``make_db_bearer_auth``): the static-token form is preserved
        for tests and ad-hoc scripts that don't need the DB; the
        DB-backed form is the primary surface for ``create_app``.
        Re-using one entry point with both shapes would have to
        sniff arg types (string vs. repo), which obscures intent at
        the call site.

    Why no DB call on the legacy hit?
        Two SQL round-trips per failed auth would amplify a
        password-spray DoS, and the legacy fallback is itself a
        constant-time comparison against an in-process string —
        symmetric with :func:`make_db_bearer_auth`'s behaviour.

    Parameters
    ----------
    repo:
        :class:`TaskRepository` bound to the app's asyncpg pool.
        Captured by the closure. Only
        :meth:`get_bootstrap_token_owner` is called from this dep.
    legacy_token:
        Optional plaintext shared bootstrap secret kept for one
        minor version (PRD AC: "shared-bearer fallback через env
        var оставить на одну минорную версию"). When ``None``, the
        dep is purely DB-backed and any non-matching bearer
        returns 401.

    Returns
    -------
    IdentityBindingAuthDependency
        Async ``Depends`` callable taking ``Request`` + the
        ``Authorization`` header. Returns ``None`` on success;
        side-effect is the ``request.state`` writes documented above.
    """
    legacy_token_clean = legacy_token.strip() if legacy_token is not None else None
    if legacy_token_clean == "":
        raise RuntimeError(
            "make_db_bootstrap_auth: legacy_token must be non-empty when provided "
            "(use None to disable the legacy WHILLY_WORKER_BOOTSTRAP_TOKEN fallback)."
        )

    async def db_bootstrap_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        token = _extract_bearer(authorization)
        try:
            owner = await repo.get_bootstrap_token_owner(token)
        except Exception:
            logger.exception("make_db_bootstrap_auth: repo lookup raised")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="bootstrap auth backend unavailable",
            ) from None
        if owner is not None:
            owner_email, is_admin = owner
            request.state.bootstrap_owner_email = owner_email
            request.state.bootstrap_is_admin = is_admin
            return None
        if legacy_token_clean is not None and secrets.compare_digest(token, legacy_token_clean):
            request.state.bootstrap_owner_email = None
            request.state.bootstrap_is_admin = False
            _maybe_warn_legacy_bootstrap_token()
            return None
        raise _bearer_401("invalid bootstrap token")

    return db_bootstrap_auth


def make_admin_auth(
    repo: TaskRepository,
    *,
    legacy_token: str | None = None,
) -> IdentityBindingAuthDependency:
    """Build a FastAPI ``Depends`` that gates ``/api/v1/admin/*`` routes on an admin bootstrap token.

    M2 mission: ``whilly admin bootstrap mint --is-admin`` mints a
    bootstrap-token row with ``is_admin=true`` (migration 009). This
    factory wraps the same DB-backed lookup as
    :func:`make_db_bootstrap_auth` but tightens the verdict:

    * 401 on missing / malformed / wrong bearer (no DB lookup
      performed when the header is absent / non-Bearer / empty);
    * 403 on a known active *non-admin* bootstrap token (auth
      succeeded but the operator is not admin-scoped);
    * 200 (i.e. dep returns ``None``) on a known active admin
      bootstrap token; ``request.state.bootstrap_owner_email`` /
      ``bootstrap_is_admin=True`` are stashed for downstream
      handlers / audit log emission.

    Legacy ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` env-var fallback is
    intentionally NOT admin-scoped: the shared cluster bootstrap
    secret cannot identify an operator and therefore cannot be
    elevated. A request bearing the legacy token against an admin
    route returns 403 (auth succeeded as a non-admin operator). One-
    shot deprecation warning still fires on the legacy hit so the
    operator notices.

    Why 403 (not 401) for non-admin?
        RFC 7235 / RFC 6750 §3 : 401 is "I don't know who you are";
        403 is "I know you, you can't do this". A known-active token
        is authenticated; admin-scope is the *authorisation* gate.
        This split lets operator dashboards separate "wrong token"
        from "right token, wrong scope" cleanly. VAL-M2-ADMIN-AUTH-008
        / -902 pin this contract.

    Why no DB-existence leak through the response code?
        Per VAL-M2-ADMIN-AUTH-904: a revoked-but-formerly-admin
        token, an active non-admin token, and a fully-bogus token
        each yield well-defined codes that don't disclose existence
        beyond what the active-set lookup already discloses
        (revoked / expired / unknown all collapse to 401; active
        non-admin yields 403). The DB lookup runs only after the
        bearer header parse passes (VAL-M2-ADMIN-AUTH-010).
    """
    legacy_token_clean = legacy_token.strip() if legacy_token is not None else None
    if legacy_token_clean == "":
        raise RuntimeError(
            "make_admin_auth: legacy_token must be non-empty when provided "
            "(use None to disable the legacy WHILLY_WORKER_BOOTSTRAP_TOKEN fallback)."
        )

    async def admin_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        token = _extract_bearer(authorization)
        try:
            owner = await repo.get_bootstrap_token_owner(token)
        except Exception:
            logger.exception("make_admin_auth: repo lookup raised")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin auth backend unavailable",
            ) from None
        if owner is not None:
            owner_email, is_admin = owner
            if not is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"operator {owner_email!r} is not admin-scoped",
                )
            request.state.bootstrap_owner_email = owner_email
            request.state.bootstrap_is_admin = True
            return None
        if legacy_token_clean is not None and secrets.compare_digest(token, legacy_token_clean):
            _maybe_warn_legacy_bootstrap_token()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="legacy WHILLY_WORKER_BOOTSTRAP_TOKEN is not admin-scoped",
            )
        raise _bearer_401("invalid bootstrap token")

    return admin_auth


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
    "IdentityBindingAuthDependency",
    "bearer_auth",
    "bootstrap_auth",
    "hash_bearer_token",
    "make_admin_auth",
    "make_bearer_auth",
    "make_bootstrap_auth",
    "make_db_bearer_auth",
    "make_db_bootstrap_auth",
    "reset_lazy_dependencies",
    "reset_legacy_warning_state",
]
