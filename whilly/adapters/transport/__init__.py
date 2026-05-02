"""HTTP transport adapter for Whilly v4.0 (PRD FR-1.2, TC-6).

This package owns everything that travels between a remote worker and the
control plane: the FastAPI app, the auth dependency, the httpx-based client
— and, crucially, the **wire schemas** that define what either side may
serialise. Modules are split so the server (FastAPI) and the client (httpx)
can share the same pydantic contract without dragging FastAPI into a worker
process that doesn't need it.

Layout
------
* :mod:`whilly.adapters.transport.schemas` — pure pydantic request/response
  models for ``register`` / ``claim`` / ``complete`` / ``fail`` /
  ``heartbeat`` plus the shared :class:`ErrorResponse`. No FastAPI, no
  asyncpg, no httpx — see TASK-021a1 / PRD TC-6.
* :mod:`whilly.adapters.transport.auth` (TASK-021a2) — FastAPI bearer-auth
  dependency.
* :mod:`whilly.adapters.transport.server` (TASK-021a3) — FastAPI app
  factory ``create_app(pool)`` and the unauthenticated ``/health`` probe.
  Route handlers for workers / tasks land here in TASK-021b/c.
* :mod:`whilly.adapters.transport.client` (TASK-022a1) — httpx-based
  :class:`RemoteWorkerClient` with retry / fail-fast wire handling and the
  typed exception hierarchy (:class:`HTTPClientError`,
  :class:`AuthError`, :class:`VersionConflictError`,
  :class:`ServerError`). High-level RPC methods land in TASK-022a2 / a3.

Worker-import-purity discipline (PRD SC-6, fix-m1-whilly-worker-fastapi-leak)
-----------------------------------------------------------------------------
Importing a *submodule* of a package implicitly executes the parent
package's ``__init__.py``. The remote-worker entry
(:mod:`whilly.cli.worker`) only needs ``client`` (httpx + pydantic +
``whilly.core``) and ``schemas`` (pure pydantic) from this package, but
the eager re-export below used to drag ``auth`` (which imports
``fastapi``) and ``server`` (which imports ``asyncpg`` + ``fastapi``)
into the worker's process every time, which broke ``pip install
whilly-orchestrator[worker] && whilly-worker --help`` with
``ModuleNotFoundError: No module named 'fastapi'``.

The fix is a module-level ``__getattr__`` that defers loading of
``auth`` / ``server`` until the first attribute access on the package.
Eager-loaded names (``schemas`` and ``client``) keep their explicit
``from ... import ...`` statements at the top of this module so any
existing code paths that read those constants on first touch continue
to work. Server-only names listed in :data:`__all__` (``create_app``,
``bearer_auth``, ``hash_bearer_token``, …) resolve through
``__getattr__`` on first access — control-plane code that touches them
sees no behaviour change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from whilly.adapters.transport.client import (
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_TIMEOUT_SECONDS,
    AuthError,
    HTTPClientError,
    RemoteWorkerClient,
    ServerError,
    VersionConflictError,
)
from whilly.adapters.transport.schemas import (
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    CompleteResponse,
    ErrorResponse,
    FailRequest,
    FailResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    TaskPayload,
)

if TYPE_CHECKING:
    from whilly.adapters.transport.auth import (
        BOOTSTRAP_TOKEN_ENV,
        SUPPRESS_WORKER_TOKEN_WARNING_ENV,
        WORKER_TOKEN_ENV,
        AuthDependency,
        bearer_auth,
        bootstrap_auth,
        hash_bearer_token,
        make_bearer_auth,
        make_bootstrap_auth,
        make_db_bearer_auth,
        reset_lazy_dependencies,
        reset_legacy_warning_state,
    )
    from whilly.adapters.transport.server import (
        CLAIM_LONG_POLL_TIMEOUT_DEFAULT,
        CLAIM_PATH,
        CLAIM_POLL_INTERVAL_DEFAULT,
        HEALTH_PATH,
        REGISTER_PATH,
        create_app,
    )


# Names sourced from :mod:`whilly.adapters.transport.auth`. Resolving any
# of them through :func:`__getattr__` triggers a single import of that
# submodule (which pulls fastapi); subsequent lookups bind directly.
_AUTH_NAMES: frozenset[str] = frozenset(
    {
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
    }
)

# Names sourced from :mod:`whilly.adapters.transport.server`. Resolving
# any of them imports the FastAPI app factory + asyncpg surface.
_SERVER_NAMES: frozenset[str] = frozenset(
    {
        "CLAIM_LONG_POLL_TIMEOUT_DEFAULT",
        "CLAIM_PATH",
        "CLAIM_POLL_INTERVAL_DEFAULT",
        "HEALTH_PATH",
        "REGISTER_PATH",
        "create_app",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily load ``auth`` / ``server`` symbols on first attribute access.

    PEP 562 module-level ``__getattr__`` is the standard pattern for
    deferring submodule imports. Once the attribute is resolved we
    cache it on the package's globals so subsequent reads bypass this
    function entirely (no per-call import cost).
    """
    if name in _AUTH_NAMES:
        from whilly.adapters.transport import auth as _auth

        value = getattr(_auth, name)
    elif name in _SERVER_NAMES:
        from whilly.adapters.transport import server as _server

        value = getattr(_server, name)
    else:
        raise AttributeError(f"module 'whilly.adapters.transport' has no attribute {name!r}")
    globals()[name] = value
    return value


__all__ = [
    "BOOTSTRAP_TOKEN_ENV",
    "CLAIM_LONG_POLL_TIMEOUT_DEFAULT",
    "CLAIM_PATH",
    "CLAIM_POLL_INTERVAL_DEFAULT",
    "DEFAULT_BACKOFF_SCHEDULE",
    "DEFAULT_TIMEOUT_SECONDS",
    "HEALTH_PATH",
    "REGISTER_PATH",
    "SUPPRESS_WORKER_TOKEN_WARNING_ENV",
    "WORKER_TOKEN_ENV",
    "AuthDependency",
    "AuthError",
    "ClaimRequest",
    "ClaimResponse",
    "CompleteRequest",
    "CompleteResponse",
    "ErrorResponse",
    "FailRequest",
    "FailResponse",
    "HTTPClientError",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "RegisterRequest",
    "RegisterResponse",
    "RemoteWorkerClient",
    "ServerError",
    "TaskPayload",
    "VersionConflictError",
    "bearer_auth",
    "bootstrap_auth",
    "create_app",
    "hash_bearer_token",
    "make_bearer_auth",
    "make_bootstrap_auth",
    "make_db_bearer_auth",
    "reset_lazy_dependencies",
    "reset_legacy_warning_state",
]
