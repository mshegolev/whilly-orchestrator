"""FastAPI app factory for the worker ↔ control-plane HTTP API (TASK-021a3 / TASK-021b / TASK-021c1 / TASK-021c2, PRD FR-1.1 / FR-1.2 / FR-1.3 / FR-1.6 / TC-6).

This module is the *composition root* of the control-plane HTTP surface.
:func:`create_app` wires the asyncpg pool, the auth dependencies from
:mod:`whilly.adapters.transport.auth` and the wire schemas from
:mod:`whilly.adapters.transport.schemas` into a single FastAPI app. The
task-facing terminal endpoints (``/tasks/{id}/complete``,
``/tasks/{id}/fail``) live alongside ``/tasks/claim`` so route handlers
stay co-located with the lifespan / state / auth plumbing they depend on.

What lives here today (TASK-021a3 + TASK-021b + TASK-021c1 + TASK-021c2)
------------------------------------------------------------------------
* :func:`create_app(pool, *, worker_token, bootstrap_token)` — factory.
  Stores ``pool``, a :class:`TaskRepository` and the two pre-bound auth
  dependencies on ``app.state`` so handlers added in TASK-021c can reach
  them via ``request.app.state``. Tokens default to the values from the
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
* ``POST /workers/register`` — cluster-join RPC (PRD FR-1.1). Gated by
  the bootstrap-token dependency: a fresh worker has no per-worker
  credentials yet, so the only secret it can prove possession of is the
  cluster-wide bootstrap token. Server mints a fresh ``worker_id`` +
  per-worker bearer token, hashes the token via SHA-256, and inserts the
  ``workers`` row through :meth:`TaskRepository.register_worker`. The
  *plaintext* token is returned exactly once in the response — the
  server discards it after sending. PRD NFR-3 guarantees plaintext is
  never persisted server-side.
* ``POST /workers/{worker_id}/heartbeat`` — liveness ping (PRD FR-1.6).
  Gated by the per-worker bearer dependency — the cluster's shared
  ``WHILLY_WORKER_TOKEN`` proves the caller is a registered member.
  Calls :meth:`TaskRepository.update_heartbeat` and surfaces the bool
  return as ``{"ok": ...}``. A 200 with ``ok=false`` (worker no longer
  registered) is the documented recoverable state — the caller should
  re-register and resume rather than crashing.
* ``POST /tasks/claim`` — long-polled task acquisition (PRD FR-1.3).
  Gated by the per-worker bearer dependency. Wraps
  :meth:`TaskRepository.claim_task` in a *server-side* poll loop: the
  request is held open for up to ``claim_long_poll_timeout`` seconds
  (default 30s), with the repo polled every ``claim_poll_interval``
  seconds (default 1.5s) until either a row transitions PENDING →
  CLAIMED or the deadline expires. A successful claim returns 200 with
  :class:`ClaimResponse` carrying the post-claim :class:`TaskPayload`;
  the timeout returns 204 No Content (per AC). 204 (rather than 200
  with a null task) keeps the wire small on the timeout path and lets
  the remote worker (TASK-022b1) re-poll without redundant JSON
  decoding. ``plan`` is intentionally left ``None`` here — the AC scope
  is "Task | 204"; populating it is deferred to a future task that
  needs the prompt context server-side.
* ``POST /tasks/{task_id}/complete`` — terminal-state RPC (PRD FR-1.1
  / FR-2.4). Gated by the per-worker bearer dependency. Thin wrapper
  over :meth:`TaskRepository.complete_task`: the worker sends the
  ``version`` it last observed (from the claim response or its own
  heartbeat) and the server's UPDATE filter (``WHERE id = $1 AND
  version = $2 AND status = 'IN_PROGRESS'``) provides the optimistic
  lock. Success returns 200 + :class:`CompleteResponse` carrying the
  post-update :class:`TaskPayload` (status ``DONE``, version + 1).
  :class:`whilly.adapters.db.VersionConflictError` maps to 409 + an
  :class:`ErrorResponse` envelope carrying the full conflict tuple
  (``task_id``, ``expected_version``, ``actual_version``,
  ``actual_status``) — the remote worker (TASK-022a3 / 022b1) reads
  those fields directly to decide retry vs drop vs surface, instead
  of running its own follow-up SELECT. The 409 body shape is a
  contract: any change here must land in lock-step with TASK-022a3's
  client-side error mapper.
* ``POST /tasks/{task_id}/fail`` — symmetric terminal-state RPC.
  Same shape as ``complete`` but accepts a non-empty ``reason`` in the
  body — the value flows straight into ``events.payload`` so the
  dashboard (TASK-027) and post-mortem queries can surface a human-
  readable cause without re-scanning logs. Same 409 contract on
  conflict. ``fail_task``'s SQL accepts both ``CLAIMED`` and
  ``IN_PROGRESS`` source states (a worker can crash before
  :meth:`TaskRepository.start_task` has even fired) — the route
  surfaces this faithfully without filtering further.
* OpenAPI docs at ``/docs`` (Swagger UI) and the spec at
  ``/openapi.json`` — both wired by FastAPI's defaults; we don't move
  them off the default paths because operators expect them there.

Long-polling design (PRD FR-1.3)
--------------------------------
The repository call (:meth:`TaskRepository.claim_task`) is itself fast —
a single SQL round-trip with ``FOR UPDATE SKIP LOCKED``. The long-poll
budget exists because issuing the same query on a tight loop would
slam Postgres for an empty plan; sleeping ``claim_poll_interval``
between attempts caps the wasted query rate at ~67 qps per idle
worker (well under what the database can absorb) without sacrificing
latency on the warm path. ``asyncio.sleep`` is cancellation-friendly:
if the HTTP client disconnects mid-poll, Starlette propagates
:class:`asyncio.CancelledError` through the sleep and the handler
unwinds cleanly without occupying a pool connection any longer than
the in-flight ``claim_task`` round-trip itself.

Deadline-based (``time.monotonic()``) rather than count-based
(``range(int(timeout / interval))``) because :func:`asyncio.sleep` may
overshoot the requested duration under event-loop pressure; the
deadline guarantees the total wall-clock time never exceeds the
budget. We do one final ``claim_task`` *without* sleeping when the
loop falls through, so a task that lands in the very last interval
window is still picked up before we return 204.

Token hashing (PRD NFR-3)
-------------------------
Per-worker tokens are produced by :func:`secrets.token_urlsafe(32)` —
~256 bits of entropy. Plain SHA-256 is correct here: with that much
entropy there is no dictionary to attack, so the slow-hashing argument
that motivates bcrypt for *passwords* doesn't apply. Constant-time hash
verification is also the natural fit for the bearer-auth path — the
heavy work-factor of bcrypt on every request would amplify trivially-
abusable DoS vectors. We keep the hash format opaque (raw lowercase
hex) so a future migration to argon2 / a salted scheme can land in
:func:`_hash_token` without touching the routes.

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

import asyncio
import hashlib
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse

from whilly.adapters.db import TaskRepository, VersionConflictError
from whilly.adapters.transport.auth import (
    BOOTSTRAP_TOKEN_ENV,
    WORKER_TOKEN_ENV,
    AuthDependency,
    make_bearer_auth,
    make_bootstrap_auth,
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
    ReleaseRequest,
    ReleaseResponse,
    TaskPayload,
)

__all__ = [
    "CLAIM_LONG_POLL_TIMEOUT_DEFAULT",
    "CLAIM_PATH",
    "CLAIM_POLL_INTERVAL_DEFAULT",
    "HEALTH_PATH",
    "REGISTER_PATH",
    "create_app",
]

logger = logging.getLogger(__name__)

#: Path of the unauthenticated health probe. Exported so tests and
#: external probes (Kubernetes ``livenessProbe.httpGet.path``) reference
#: the same string and a typo here surfaces in CI immediately.
HEALTH_PATH: Final[str] = "/health"

#: Path of the cluster-join RPC. Exported for symmetry with
#: :data:`HEALTH_PATH` so tests and the httpx client (TASK-022a) point at
#: the same constant — a typo here would surface in CI rather than as a
#: silent 404 in production.
REGISTER_PATH: Final[str] = "/workers/register"

#: Path of the task-claim RPC. Exported alongside :data:`REGISTER_PATH` so
#: tests, the httpx client (TASK-022a) and any operator running ``curl``
#: against the API land on the same string — a typo here would surface
#: in CI as a 404 immediately rather than as silently-broken claims in
#: production.
CLAIM_PATH: Final[str] = "/tasks/claim"

#: Default ``claim_long_poll_timeout`` (seconds) — the upper bound on how
#: long ``POST /tasks/claim`` holds an idle request open before returning
#: 204. 30s is the PRD's TASK-021c1 budget: long enough that a worker
#: that just crashed and respawned doesn't issue 30 RPCs before the next
#: PENDING row arrives, short enough that proxies / load-balancers /
#: ``httpx`` don't hit their own connection-idle timeouts (typically
#: 60-120s). Tests override this via the ``claim_long_poll_timeout``
#: kwarg on :func:`create_app` so the suite stays fast.
CLAIM_LONG_POLL_TIMEOUT_DEFAULT: Final[float] = 30.0

#: Default ``claim_poll_interval`` (seconds) — how often the long-poll
#: loop re-issues ``claim_task`` against the database while waiting for
#: a PENDING row. 1.5s is a deliberate compromise: tighter (≤0.5s)
#: amplifies query pressure on idle plans without meaningfully reducing
#: latency on the warm path; looser (≥3s) leaves an unhappy-path tail
#: where a task lands but waits seconds for the next poll. ~67 qps per
#: idle worker is well under the database's per-connection cost.
CLAIM_POLL_INTERVAL_DEFAULT: Final[float] = 1.5

#: Number of bytes of entropy used by :func:`secrets.token_urlsafe` for the
#: per-worker bearer token. 32 bytes ≈ 256 bits — well above the threshold
#: where rainbow / dictionary attacks become irrelevant, so plain SHA-256
#: hashing of the result is sufficient (see module docstring).
_TOKEN_ENTROPY_BYTES: Final[int] = 32

#: Number of bytes of entropy used for the server-issued worker_id. 8 bytes
#: ≈ 64 bits, giving ~10^9 collisions only after ~4 billion registrations
#: per the birthday bound — orders of magnitude above any realistic cluster
#: size, so the unique-violation surface in :class:`TaskRepository` is a
#: defensive theoretical guard rather than a hot path.
_WORKER_ID_ENTROPY_BYTES: Final[int] = 8

#: Prefix for server-generated worker ids. ``w-`` keeps the IDs human-
#: scannable in logs / dashboards (TASK-027) without limiting the entropy
#: of the suffix.
_WORKER_ID_PREFIX: Final[str] = "w-"

#: API metadata exposed via ``/docs`` and ``/openapi.json``. ``version``
#: is intentionally ``4.0.0-dev`` rather than reading
#: :mod:`whilly.__version__` — the wire protocol version is independent
#: of the package version, and bumping it deliberately on every breaking
#: protocol change (TASK-022 onward) is easier to track than coupling it
#: to ``__version__``.
_API_TITLE: Final[str] = "Whilly Control Plane"
_API_VERSION: Final[str] = "4.0.0-dev"


def _hash_token(plaintext: str) -> str:
    """Return the canonical hash of a per-worker bearer token (PRD NFR-3).

    Plain SHA-256 over UTF-8 bytes, hex-encoded. The output is what lands
    in ``workers.token_hash``; the plaintext token is returned to the
    worker exactly once via :class:`RegisterResponse` and then discarded
    by the server. See the module docstring for why bcrypt isn't the
    right primitive for *random* bearer tokens.

    Centralising the encoding here means a future migration to a salted
    or KDF-based scheme (argon2 / scrypt) can change the hash format
    without touching the route handlers — they call :func:`_hash_token`
    and let this function decide what goes on disk.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _generate_worker_id() -> str:
    """Mint a fresh URL-safe ``worker_id`` for a newly-registered worker.

    Format is ``w-<urlsafe>`` — the prefix keeps logs scannable and the
    suffix carries the entropy. Uses :func:`secrets.token_urlsafe` so the
    bytes come from the OS CSPRNG; collisions are vanishingly unlikely
    across any plausible cluster size (see :data:`_WORKER_ID_ENTROPY_BYTES`).
    """
    return f"{_WORKER_ID_PREFIX}{secrets.token_urlsafe(_WORKER_ID_ENTROPY_BYTES)}"


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
    claim_long_poll_timeout: float = CLAIM_LONG_POLL_TIMEOUT_DEFAULT,
    claim_poll_interval: float = CLAIM_POLL_INTERVAL_DEFAULT,
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
    claim_long_poll_timeout:
        Total seconds the ``POST /tasks/claim`` handler holds the
        request open while polling for a PENDING task. Defaults to
        :data:`CLAIM_LONG_POLL_TIMEOUT_DEFAULT` (30s — the PRD budget).
        Tests pass a small value (e.g. 0.3s) so the suite isn't
        dominated by the long-poll wait time.
    claim_poll_interval:
        Seconds between ``claim_task`` retries inside the long-poll
        loop. Defaults to :data:`CLAIM_POLL_INTERVAL_DEFAULT` (1.5s).
        Must be strictly positive — a zero / negative interval would
        spin a tight loop against Postgres and is rejected.

    Returns
    -------
    FastAPI
        Configured app with ``/health``, ``/docs``, ``/openapi.json``,
        ``/workers/*`` and ``POST /tasks/claim`` wired. TASK-021c2 adds
        ``POST /tasks/{id}/complete`` and ``POST /tasks/{id}/fail`` onto
        this same app instance.

    Raises
    ------
    RuntimeError
        If a required token is missing both from kwargs and the
        environment. The error names the env var so operators don't
        have to grep the codebase to find what's missing.
    ValueError
        If ``claim_poll_interval`` is not strictly positive — a zero
        or negative interval would spin a tight loop against
        Postgres without yielding to the event loop.
    """
    if claim_poll_interval <= 0:
        # Catch the misconfiguration at construction time (loud) rather
        # than spinning a CPU-bound poll loop in production (silent and
        # disastrous). Negative or zero would also defeat ``asyncio.sleep``
        # as a cancellation point, since ``sleep(0)`` does *not* yield in
        # all event-loop implementations.
        raise ValueError(
            f"create_app: claim_poll_interval must be > 0, got {claim_poll_interval!r}; "
            f"a zero or negative interval would tight-loop the database."
        )
    if claim_long_poll_timeout < 0:
        raise ValueError(f"create_app: claim_long_poll_timeout must be >= 0, got {claim_long_poll_timeout!r}.")
    resolved_worker_token = _resolve_token(worker_token, WORKER_TOKEN_ENV)
    resolved_bootstrap_token = _resolve_token(bootstrap_token, BOOTSTRAP_TOKEN_ENV)
    # Bind the auth dependencies *now*, at app-construction time, so a
    # bad token surfaces during ``create_app`` rather than on the first
    # 401 in production. Both factories also reject empty tokens
    # internally, giving us defence in depth against
    # ``_resolve_token`` ever returning an empty string by accident.
    bearer_dep: AuthDependency = make_bearer_auth(resolved_worker_token)
    bootstrap_dep: AuthDependency = make_bootstrap_auth(resolved_bootstrap_token)
    # Construct the repository once at app build time. The repo is a
    # thin wrapper around the pool, so reusing one instance across all
    # requests is both correct and cheaper than instantiating per
    # request. Stashed on app.state below so handlers added in
    # TASK-021c reach the same instance via ``request.app.state``.
    repo = TaskRepository(pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Stash pool + repo + auth deps on app.state so handlers added
        # in TASK-021c can reach them without closing over module
        # globals. ``state`` is starlette's free-form attribute bag —
        # typed as ``Any`` so we can't lean on mypy here; the
        # integration tests in TASK-021b/c are what guarantee handlers
        # find what they need.
        app.state.pool = pool
        app.state.repo = repo
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
            app.state.repo = None
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

    @app.post(
        REGISTER_PATH,
        response_model=RegisterResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(bootstrap_dep)],
    )
    async def register_worker(payload: RegisterRequest) -> RegisterResponse:
        """Mint a fresh worker identity and return its bearer token (PRD FR-1.1).

        Gated by the *bootstrap* token (cluster-join secret) — the
        worker has no per-worker credentials yet, so the only secret it
        can present is the cluster-wide ``WHILLY_WORKER_BOOTSTRAP_TOKEN``.

        The handler:

        1. Generates a fresh ``worker_id`` (``w-<urlsafe>``) — server-
           side so two workers can't pick the same id.
        2. Generates a fresh per-worker bearer token via
           :func:`secrets.token_urlsafe(32)` — ~256 bits of entropy.
        3. Hashes the token via :func:`_hash_token` and stores only the
           hash in ``workers.token_hash`` (PRD NFR-3 — plaintext is
           never persisted).
        4. Returns the *plaintext* token in the response. The worker is
           expected to keep it in memory for the lifetime of the
           process; if it crashes it must re-register.

        201 (not 200) is the right status: a new resource (the worker
        row) is created, and operators reading access logs can grep
        ``201`` to count successful registrations. Returning ``RegisterResponse``
        as a model means FastAPI handles validation + serialisation +
        the OpenAPI spec automatically.
        """
        worker_id = _generate_worker_id()
        plaintext_token = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
        token_hash = _hash_token(plaintext_token)
        try:
            await repo.register_worker(worker_id, payload.hostname, token_hash)
        except asyncpg.UniqueViolationError:
            # Defensive: 64 bits of entropy makes this nearly impossible.
            # If it does fire we surface a 500 rather than retrying with
            # a fresh id — a collision is overwhelmingly likely to mean
            # something is wrong with the entropy source / clock and a
            # blind retry would just paper over it.
            logger.exception("register_worker: worker_id collision on %s", worker_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="worker_id collision; retry registration",
            ) from None
        return RegisterResponse(worker_id=worker_id, token=plaintext_token)

    @app.post(
        "/workers/{worker_id}/heartbeat",
        response_model=HeartbeatResponse,
        dependencies=[Depends(bearer_dep)],
    )
    async def heartbeat(worker_id: str, payload: HeartbeatRequest) -> HeartbeatResponse:
        """Refresh ``workers.last_heartbeat`` for ``worker_id`` (PRD FR-1.6).

        Gated by the per-worker bearer dependency — the cluster-shared
        ``WHILLY_WORKER_TOKEN`` is what proves the caller is a registered
        member of the cluster. The path parameter is the canonical
        identity; the body's ``worker_id`` is a defence-in-depth echo
        (per :class:`HeartbeatRequest`'s docstring) that we validate
        against the path to surface mis-routed clients early.

        ``ok=false`` with HTTP 200 is the documented recoverable state:
        the worker_id is not (or no longer) registered. The caller's
        right move is to re-register and continue, not to crash. Any
        unrelated database failure surfaces as a 500 with the asyncpg
        error in the body — see :class:`TaskRepository.update_heartbeat`.
        """
        if payload.worker_id != worker_id:
            # Mismatch between path and body indicates a misrouted /
            # mis-built client request. 400 is the right code: the
            # request itself is malformed (the body contradicts the
            # URL). 422 would be a pure schema violation; this is a
            # cross-field validation that FastAPI can't catch via
            # pydantic alone.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"worker_id in body does not match path: path={worker_id!r} body={payload.worker_id!r}"),
            )
        ok = await repo.update_heartbeat(worker_id)
        return HeartbeatResponse(ok=ok)

    @app.post(
        CLAIM_PATH,
        # The 200 path returns ClaimResponse; FastAPI will infer this
        # from the type annotation and respect the explicit ``Response``
        # we return on the 204 path. Declaring it explicitly here keeps
        # /docs accurate (Swagger shows the success body shape).
        response_model=ClaimResponse,
        # Both 200 and 204 are documented success responses. Without
        # this OpenAPI shows only the inferred 200, and a worker
        # writing against the schema would think 204 is unexpected.
        responses={
            status.HTTP_200_OK: {"model": ClaimResponse, "description": "Task claimed"},
            status.HTTP_204_NO_CONTENT: {
                "description": "Long-poll timeout expired with no PENDING tasks; the worker should re-issue the claim.",
            },
        },
        dependencies=[Depends(bearer_dep)],
    )
    async def claim(payload: ClaimRequest) -> Response | ClaimResponse:
        """Long-polled task acquisition (PRD FR-1.3).

        Wraps :meth:`TaskRepository.claim_task` in a server-side poll
        loop. The handler retries the claim every ``claim_poll_interval``
        seconds until either:

        * a row transitions PENDING → CLAIMED (return 200 + the post-
          update :class:`TaskPayload`), or
        * the cumulative wait time exceeds ``claim_long_poll_timeout``
          (return 204 No Content per AC).

        Why server-side long-polling rather than client-side retry?
            The remote worker (TASK-022b1) would otherwise have to
            implement its own back-off + reconnect ladder, multiplying
            both the client complexity and the request rate against
            the database. Holding a single connection open here lets
            the worker's outer loop stay trivial: ``while True: claim();
            run(); complete()``.

        Cancellation
            ``asyncio.sleep`` is cancellation-friendly: if the client
            disconnects mid-poll, Starlette propagates
            :class:`asyncio.CancelledError` through the sleep and the
            handler unwinds without holding the asyncpg connection
            longer than the in-flight ``claim_task`` round-trip itself.

        Why one final attempt past the deadline?
            ``asyncio.sleep`` overshoots under event-loop pressure;
            using a deadline-based loop guarantees the wall-clock
            budget but means the *last* sleep can put us past the
            deadline before we've actually polled. We do one
            unconditional final ``claim_task`` so a row that arrived
            in the trailing window is still returned rather than 204'd.
        """
        deadline = time.monotonic() + claim_long_poll_timeout
        while True:
            claimed = await repo.claim_task(payload.worker_id, payload.plan_id)
            if claimed is not None:
                # Hot path: claim succeeded. Wrap the domain Task in
                # the wire-format payload — ``plan`` is intentionally
                # left ``None``: the AC scope is "Task | 204", and a
                # plan-name lookup would expand the task footprint.
                # TASK-022b1 / future work can populate it if needed.
                logger.info(
                    "claim: worker=%s plan=%s task=%s",
                    payload.worker_id,
                    payload.plan_id,
                    claimed.id,
                )
                return ClaimResponse(task=TaskPayload.from_task(claimed))

            now = time.monotonic()
            if now >= deadline:
                # Long-poll budget exhausted. 204 No Content is the AC
                # contract: the worker should re-issue the claim
                # immediately (TASK-022b1's behaviour on 204).
                logger.debug(
                    "claim: timeout (no PENDING tasks) worker=%s plan=%s",
                    payload.worker_id,
                    payload.plan_id,
                )
                return Response(status_code=status.HTTP_204_NO_CONTENT)

            # Cap the sleep at the time remaining to the deadline so
            # the *total* wait time never exceeds ``claim_long_poll_timeout``
            # — even when the interval doesn't divide the budget evenly.
            await asyncio.sleep(min(claim_poll_interval, deadline - now))

    @app.post(
        "/tasks/{task_id}/complete",
        response_model=CompleteResponse,
        responses={
            status.HTTP_200_OK: {
                "model": CompleteResponse,
                "description": "Task transitioned IN_PROGRESS → DONE.",
            },
            status.HTTP_409_CONFLICT: {
                "model": ErrorResponse,
                "description": (
                    "Optimistic-locking conflict: another writer advanced the "
                    "version, the row's status disallows the transition, or the "
                    "task no longer exists."
                ),
            },
        },
        dependencies=[Depends(bearer_dep)],
    )
    async def complete_task(task_id: str, payload: CompleteRequest) -> CompleteResponse | JSONResponse:
        """Terminal-state RPC: IN_PROGRESS → DONE (PRD FR-1.1, FR-2.4).

        Thin wrapper over :meth:`TaskRepository.complete_task`. The
        ``task_id`` lives on the URL (it identifies the resource being
        mutated, so it belongs in the path); ``version`` and
        ``worker_id`` come from the body. The ``worker_id`` echo is
        defence-in-depth — the bearer token already authenticates the
        worker, but logging the claimed identity alongside the actual
        repo call lets operators correlate a 409 with the *worker*
        that hit it, not just the request id.

        409 mapping
            :class:`VersionConflictError` carries ``task_id``,
            ``expected_version``, ``actual_version``, and
            ``actual_status``. We project them into the
            :class:`ErrorResponse` envelope so a remote worker
            (TASK-022a3) can branch on the actual conflict cause
            without an extra SELECT round-trip:

            * ``actual_status is None`` and ``actual_version is None`` →
              the row is gone (FK cascade in tests, mis-routed worker);
            * ``actual_version != expected_version`` → another writer
              advanced the counter first (lost-update / re-claim);
            * ``actual_version == expected_version`` and ``actual_status``
              is ``DONE`` / ``FAILED`` / ``SKIPPED`` → idempotent retry,
              the worker can treat it as success and move on.

            The error code string is ``"version_conflict"`` — a stable
            machine-readable identifier the client maps onto its own
            retry policy, mirrored in TASK-022a3's mapper.

        Why a JSONResponse for 409 instead of HTTPException?
            ``HTTPException(detail=...)`` only fills the ``detail``
            field of FastAPI's default error envelope; it cannot
            populate the structured fields (``task_id``,
            ``expected_version``, etc.) that :class:`ErrorResponse`
            promises. Returning a typed :class:`JSONResponse` lets us
            ship the full envelope while still honouring the
            ``responses`` map declared on the route, so /docs shows
            the correct shape on the conflict path.
        """
        try:
            updated = await repo.complete_task(task_id, payload.version)
        except VersionConflictError as exc:
            logger.info(
                "complete_task conflict: worker=%s task=%s expected_version=%d actual_version=%s actual_status=%s",
                payload.worker_id,
                exc.task_id,
                exc.expected_version,
                exc.actual_version,
                exc.actual_status.value if exc.actual_status else None,
            )
            return _conflict_response(exc)
        logger.info(
            "complete_task: worker=%s task=%s version=%d → DONE",
            payload.worker_id,
            updated.id,
            updated.version,
        )
        return CompleteResponse(task=TaskPayload.from_task(updated))

    @app.post(
        "/tasks/{task_id}/fail",
        response_model=FailResponse,
        responses={
            status.HTTP_200_OK: {
                "model": FailResponse,
                "description": "Task transitioned CLAIMED|IN_PROGRESS → FAILED.",
            },
            status.HTTP_409_CONFLICT: {
                "model": ErrorResponse,
                "description": (
                    "Optimistic-locking conflict — see the matching /tasks/{task_id}/complete description."
                ),
            },
        },
        dependencies=[Depends(bearer_dep)],
    )
    async def fail_task(task_id: str, payload: FailRequest) -> FailResponse | JSONResponse:
        """Terminal-state RPC: CLAIMED | IN_PROGRESS → FAILED (PRD FR-1.1, FR-2.4).

        Mirrors :func:`complete_task` exactly except for the extra
        ``reason`` field, which lands in the ``events.payload`` audit
        row alongside the post-update version. ``reason`` is required
        and non-empty (enforced by :class:`FailRequest`'s
        :data:`NonEmptyReason` constraint) — a blank reason would
        defeat the dashboard's whole point.

        ``fail_task``'s repository SQL accepts both ``CLAIMED`` *and*
        ``IN_PROGRESS`` as valid source states (a worker may crash
        before :meth:`TaskRepository.start_task` has fired), so this
        route does not pre-filter on the source status — the repo
        owns that policy and the 409 envelope surfaces the actual
        status when the transition is rejected.
        """
        try:
            updated = await repo.fail_task(task_id, payload.version, payload.reason)
        except VersionConflictError as exc:
            logger.info(
                "fail_task conflict: worker=%s task=%s expected_version=%d actual_version=%s actual_status=%s",
                payload.worker_id,
                exc.task_id,
                exc.expected_version,
                exc.actual_version,
                exc.actual_status.value if exc.actual_status else None,
            )
            return _conflict_response(exc)
        logger.info(
            "fail_task: worker=%s task=%s version=%d reason=%r → FAILED",
            payload.worker_id,
            updated.id,
            updated.version,
            payload.reason,
        )
        return FailResponse(task=TaskPayload.from_task(updated))

    @app.post(
        "/tasks/{task_id}/release",
        response_model=ReleaseResponse,
        responses={
            status.HTTP_200_OK: {
                "model": ReleaseResponse,
                "description": "Task transitioned CLAIMED|IN_PROGRESS → PENDING.",
            },
            status.HTTP_409_CONFLICT: {
                "model": ErrorResponse,
                "description": (
                    "Optimistic-locking conflict — see the matching /tasks/{task_id}/complete description."
                ),
            },
        },
        dependencies=[Depends(bearer_dep)],
    )
    async def release_task(task_id: str, payload: ReleaseRequest) -> ReleaseResponse | JSONResponse:
        """Worker-driven release: CLAIMED | IN_PROGRESS → PENDING (TASK-022b3, PRD FR-1.6, NFR-1).

        HTTP analogue of :meth:`TaskRepository.release_task` — wraps the
        same SQL primitive the local worker calls directly on
        SIGTERM / SIGINT (TASK-019b2). Mirrors :func:`fail_task`'s
        shape because the request bodies are identical (worker_id +
        version + non-empty reason); the only differences are the
        terminal status (``PENDING`` rather than ``FAILED``) and the
        event_type (``RELEASE`` rather than ``FAIL``).

        Why a dedicated endpoint rather than reusing /fail with a
        special reason?
            ``fail_task`` flips the row to ``FAILED``, which would
            require the visibility-timeout sweep to re-PENDING it
            *and* would surface in the dashboard's "failed task"
            counter for what is actually a clean cooperative
            shutdown. A dedicated route keeps the audit log honest
            and lets a peer worker re-claim the row within one poll
            cycle instead of waiting up to 15 minutes for the sweep.

        Same 409 contract as :func:`fail_task` — both routes share
        :func:`_conflict_response`, so the wire envelope a remote
        worker (TASK-022b3) reads on a lost race is identical
        regardless of which terminal-state RPC the conflict surfaced
        on. The repository's classification logic distinguishes the
        three cases (lost-update / wrong-status / row-gone) so the
        worker can branch cleanly:

        * ``actual_status == PENDING`` — the visibility-timeout sweep
          (or another worker's release) beat us to it; treat as
          idempotent-success and exit.
        * ``actual_status`` in (``DONE``, ``FAILED``, ``SKIPPED``) —
          the worker actually finished the task before the signal
          handler reached this RPC (extremely narrow race); the
          terminal status wins.
        """
        try:
            updated = await repo.release_task(task_id, payload.version, payload.reason)
        except VersionConflictError as exc:
            logger.info(
                "release_task conflict: worker=%s task=%s expected_version=%d actual_version=%s actual_status=%s",
                payload.worker_id,
                exc.task_id,
                exc.expected_version,
                exc.actual_version,
                exc.actual_status.value if exc.actual_status else None,
            )
            return _conflict_response(exc)
        logger.info(
            "release_task: worker=%s task=%s version=%d reason=%r → PENDING",
            payload.worker_id,
            updated.id,
            updated.version,
            payload.reason,
        )
        return ReleaseResponse(task=TaskPayload.from_task(updated))

    return app


def _conflict_response(exc: VersionConflictError) -> JSONResponse:
    """Project a :class:`VersionConflictError` onto a 409 :class:`ErrorResponse`.

    Centralised because both ``complete_task`` and ``fail_task`` map
    the same exception with the same shape — and a future
    ``release_task`` HTTP endpoint will reuse it. Keeping the
    projection in one place means the wire contract for
    ``"version_conflict"`` lives in exactly one location, so a future
    schema change (extra fields, alternate error codes) lands without
    touching the routes.

    The ``error`` code is the stable machine-readable token; the
    ``detail`` is :func:`str(exc)`'s human-readable message — the same
    text that appears in server logs, which keeps debugging cheap when
    a remote worker reports a 409 from production.
    """
    body = ErrorResponse(
        error="version_conflict",
        detail=str(exc),
        task_id=exc.task_id,
        expected_version=exc.expected_version,
        actual_version=exc.actual_version,
        actual_status=exc.actual_status,
    )
    # ``mode="json"`` so enums (e.g. ``actual_status``) are serialised
    # as their string values rather than the bare Enum instance, which
    # the default JSON encoder would reject. ``exclude_none=False`` is
    # the default but stated for emphasis: ``ErrorResponse`` clients
    # rely on the ``None`` markers to distinguish "field not
    # applicable to this error" from "field absent because the server
    # forgot to populate it".
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status.HTTP_409_CONFLICT,
    )
