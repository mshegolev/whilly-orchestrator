"""FastAPI app factory for the worker ↔ control-plane HTTP API (TASK-021a3, PRD FR-1.2 / TC-6).

This module is the *composition root* of the control-plane HTTP surface.
:func:`create_app` wires the asyncpg pool, the auth dependencies from
:mod:`whilly.adapters.transport.auth` and the wire schemas from
:mod:`whilly.adapters.transport.schemas` into a single FastAPI app. The
worker- and task-facing endpoints arrive in TASK-021b and TASK-021c —
they will be added in this same module so route handlers stay co-located
with the lifespan / state / auth plumbing they depend on.

What lives here today (TASK-021a3)
----------------------------------
* :func:`create_app(pool, *, worker_token, bootstrap_token)` — factory.
  Stores ``pool`` and the two pre-bound auth dependencies on ``app.state``
  so handlers added in TASK-021b/c can reach them via
  ``request.app.state``. Tokens default to the values from the
  environment so production callers can ``create_app(pool)`` with no
  kwargs and still get a fail-fast error if the env is misconfigured.
  Tests pass tokens explicitly to avoid touching ``os.environ``.
* ``GET /health`` — unauthenticated liveness/readiness probe. Pings the
  pool with ``SELECT 1`` and returns ``{"status": "ok"}`` on success, 503
  with ``{"status": "unavailable", "detail": ...}`` on database failure.
  A bare 200 would lie when the Postgres link has died; the round-trip
  cost is one already-warmed connection (see
  :func:`whilly.adapters.db.create_pool`) and the operational win — early
  detection by Kubernetes liveness or an external uptime probe — is
  large.
* OpenAPI docs at ``/docs`` (Swagger UI) and the spec at
  ``/openapi.json`` — both wired by FastAPI's defaults; we don't move
  them off the default paths because operators expect them there.

Why a factory, not a module-level ``app``
-----------------------------------------
A module-level ``app = FastAPI()`` would force the pool / tokens to be
read from globals (env, module state) at import time. That breaks:

* **Tests.** Pytest can't construct multiple apps with different tokens
  or fake pools without monkey-patching the import.
* **The CLI run command (TASK-024 / TASK-025a).** The supervisor wants
  to open the pool, hand it to the app, and tear them down in a defined
  order; if the app opened the pool itself we'd have two lifecycles to
  reconcile.
* **Mid-flight reconfiguration.** A factory makes it explicit that
  rebuilding the app is the way to change config — no surprising
  late-binding of ``app.state.pool``.

This mirrors :func:`whilly.cli.run.run_run_command`'s composition shape
exactly: open pool → build object that needs pool → run → close pool.

Pool ownership
--------------
:func:`create_app` does **not** open or close the pool. Callers pass an
already-opened :class:`asyncpg.Pool` and are responsible for closing it
when the app shuts down (typically via
:func:`whilly.adapters.db.close_pool` in their own supervisor scope).
This is symmetric with the local-worker entry point in TASK-019c —
keeping pool ownership in the caller means we never accidentally
double-close on hot reloads or in tests that share a pool across
requests.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import asyncpg
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from whilly.adapters.transport.auth import (
    BOOTSTRAP_TOKEN_ENV,
    WORKER_TOKEN_ENV,
    AuthDependency,
    make_bearer_auth,
    make_bootstrap_auth,
)

__all__ = [
    "HEALTH_PATH",
    "create_app",
]

logger = logging.getLogger(__name__)

#: Path of the unauthenticated health probe. Exported so tests and
#: external probes (Kubernetes ``livenessProbe.httpGet.path``) reference
#: the same string and a typo here surfaces in CI immediately.
HEALTH_PATH: Final[str] = "/health"

#: API metadata exposed via ``/docs`` and ``/openapi.json``. ``version``
#: is intentionally ``4.0.0-dev`` rather than reading
#: :mod:`whilly.__version__` — the wire protocol version is independent
#: of the package version, and bumping it deliberately on every breaking
#: protocol change (TASK-022 onward) is easier to track than coupling it
#: to ``__version__``.
_API_TITLE: Final[str] = "Whilly Control Plane"
_API_VERSION: Final[str] = "4.0.0-dev"


def _resolve_token(arg: str | None, env_name: str) -> str:
    """Resolve a token from an explicit kwarg or fall back to the environment.

    Returning the resolved string (rather than ``arg or os.environ[...]``
    inline at the call site) keeps the missing-config error message in
    one place — and lets us normalise whitespace from ``.env`` files
    without cluttering :func:`create_app`. Whitespace is treated as empty
    on purpose: ``WHILLY_WORKER_TOKEN= `` is far more likely to be a
    misconfiguration than a deliberate "auth disabled" toggle, and
    :func:`whilly.adapters.transport.auth.make_bearer_auth` would only
    surface the empty string later as a less-helpful "must be non-empty"
    runtime error.

    Priority order (caller's value wins) is what makes the test seam
    work: a unit test passes ``worker_token="t"`` to
    :func:`create_app` and never has to mutate ``os.environ`` (which
    would race with parallel tests).
    """
    if arg is not None:
        if not arg.strip():
            raise RuntimeError(
                f"create_app: explicit token for {env_name} must be non-empty; "
                f"pass a real bearer string or omit the kwarg to read from the environment."
            )
        return arg
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        raise RuntimeError(
            f"environment variable {env_name} is required for HTTP transport auth; "
            f"set it on the control-plane process or pass the value to create_app() "
            f"explicitly. See whilly/adapters/transport/auth.py docstring for the "
            f"bootstrap vs per-worker token split."
        )
    return raw


def create_app(
    pool: asyncpg.Pool,
    *,
    worker_token: str | None = None,
    bootstrap_token: str | None = None,
) -> FastAPI:
    """Build a FastAPI control-plane app bound to ``pool`` and the configured tokens.

    Parameters
    ----------
    pool:
        Already-opened asyncpg pool. ``create_app`` does not own its
        lifecycle — the caller closes it.
    worker_token:
        Per-worker bearer token (PRD FR-1.2). If ``None``, read from
        :data:`whilly.adapters.transport.auth.WORKER_TOKEN_ENV` (i.e.
        ``WHILLY_WORKER_TOKEN``). Tests pass an explicit value to avoid
        env mutation.
    bootstrap_token:
        Cluster-join secret for ``POST /workers/register``. If ``None``,
        read from
        :data:`whilly.adapters.transport.auth.BOOTSTRAP_TOKEN_ENV`
        (i.e. ``WHILLY_WORKER_BOOTSTRAP_TOKEN``).

    Returns
    -------
    FastAPI
        Configured app with ``/health``, ``/docs``, ``/openapi.json``
        already wired. TASK-021b/c add ``/workers/*`` and ``/tasks/*``
        routes onto this same app instance.

    Raises
    ------
    RuntimeError
        If a required token is missing both from kwargs and the
        environment. The error names the env var so operators don't
        have to grep the codebase to find what's missing.
    """
    resolved_worker_token = _resolve_token(worker_token, WORKER_TOKEN_ENV)
    resolved_bootstrap_token = _resolve_token(bootstrap_token, BOOTSTRAP_TOKEN_ENV)
    # Bind the auth dependencies *now*, at app-construction time, so a
    # bad token surfaces during ``create_app`` rather than on the first
    # 401 in production. Both factories also reject empty tokens
    # internally, giving us defence in depth against
    # ``_resolve_token`` ever returning an empty string by accident.
    bearer_dep: AuthDependency = make_bearer_auth(resolved_worker_token)
    bootstrap_dep: AuthDependency = make_bootstrap_auth(resolved_bootstrap_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Stash pool + auth deps on app.state so handlers added in
        # TASK-021b/c can reach them without closing over module globals.
        # ``state`` is starlette's free-form attribute bag — typed as
        # ``Any`` so we can't lean on mypy here; the integration tests
        # in TASK-021b/c are what guarantee handlers find what they need.
        app.state.pool = pool
        app.state.bearer_dep = bearer_dep
        app.state.bootstrap_dep = bootstrap_dep
        logger.info("Whilly control-plane app started")
        try:
            yield
        finally:
            # Pool ownership is the caller's; we just drop our reference
            # so handlers don't keep a dangling pointer if the caller
            # closes the pool but reuses the app object (e.g. test
            # harnesses that share a pool across multiple sub-apps).
            app.state.pool = None
            logger.info("Whilly control-plane app stopped")

    app = FastAPI(
        title=_API_TITLE,
        version=_API_VERSION,
        lifespan=lifespan,
        # /docs (Swagger UI) and /openapi.json on FastAPI defaults —
        # operators expect them there, no reason to relocate.
    )

    @app.get(
        HEALTH_PATH,
        # Hidden from /docs because operators reach it from kube probes,
        # not from the API surface — keeps the OpenAPI schema focused
        # on worker-facing endpoints (TASK-021b/c).
        include_in_schema=False,
    )
    async def health() -> JSONResponse:
        """Liveness/readiness probe — pings the asyncpg pool with ``SELECT 1``.

        Returns 200 with ``{"status": "ok"}`` when the pool is reachable
        and Postgres responds to ``SELECT 1``; 503 with
        ``{"status": "unavailable", "detail": ...}`` on any
        :class:`Exception` raised by ``acquire()`` / ``fetchval()``.

        We catch :class:`Exception` (not :class:`BaseException`) so
        cancellation / KeyboardInterrupt still propagates — health
        endpoints should not swallow process-level signals.
        """
        try:
            async with pool.acquire() as conn:
                result: Any = await conn.fetchval("SELECT 1")
        except Exception as exc:
            # ``logger.warning`` rather than ``error`` because a single
            # failed health-check is the *signal* operators want to see;
            # noisy ``error`` lines pollute the alert path when the
            # outage is already obvious from the 503.
            logger.warning("Health check failed: %s", exc)
            return JSONResponse(
                {"status": "unavailable", "detail": str(exc)},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if result != 1:
            # Defensive: SELECT 1 always returns 1 against a live
            # Postgres. If we somehow get something else (proxy
            # rewriting queries, mocked pool returning the wrong type)
            # we surface it as 503 rather than pretending the system is
            # healthy.
            return JSONResponse(
                {
                    "status": "unavailable",
                    "detail": f"unexpected SELECT 1 result: {result!r}",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse({"status": "ok"})

    return app
