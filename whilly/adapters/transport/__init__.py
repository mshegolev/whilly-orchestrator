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

Re-exports below give callers a stable surface
(``from whilly.adapters.transport import RegisterRequest``) without needing
to know which submodule a schema lives in.
"""

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
from whilly.adapters.transport.server import (
    CLAIM_LONG_POLL_TIMEOUT_DEFAULT,
    CLAIM_PATH,
    CLAIM_POLL_INTERVAL_DEFAULT,
    HEALTH_PATH,
    REGISTER_PATH,
    create_app,
)

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
