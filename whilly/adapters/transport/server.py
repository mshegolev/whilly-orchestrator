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
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from whilly.adapters.db import TaskRepository, VersionConflictError
from whilly.api.event_flusher import (
    DEFAULT_BATCH_LIMIT as EVENT_FLUSHER_DEFAULT_BATCH_LIMIT,
)
from whilly.api.event_flusher import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS as EVENT_FLUSHER_DEFAULT_DRAIN_TIMEOUT_SECONDS,
)
from whilly.api.event_flusher import (
    DEFAULT_FLUSH_INTERVAL_SECONDS as EVENT_FLUSHER_DEFAULT_FLUSH_INTERVAL_SECONDS,
)
from whilly.api.event_flusher import (
    EVENT_FLUSHER_TASK_NAME,
    EventFlusher,
)
from whilly.adapters.transport.auth import (
    BOOTSTRAP_TOKEN_ENV,
    WORKER_TOKEN_ENV,
    AuthDependency,
    IdentityBindingAuthDependency,
    hash_bearer_token,
    make_bootstrap_auth,
    make_db_bearer_auth,
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
    "HEARTBEAT_TIMEOUT_DEFAULT_SECONDS",
    "OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS",
    "REGISTER_PATH",
    "SWEEP_INTERVAL_DEFAULT_SECONDS",
    "VISIBILITY_TIMEOUT_DEFAULT_SECONDS",
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

#: Default visibility-timeout for the periodic stale-claim sweep (PRD
#: FR-1.4, TASK-025a). 15 minutes is the PRD's reference value: long
#: enough that a slow agent on a healthy worker doesn't spuriously lose
#: its claim while the runner is mid-execution, short enough that a
#: hard-killed worker's tasks come back into the queue inside one user-
#: noticeable window. Tests override this via the
#: ``visibility_timeout_seconds`` kwarg on :func:`create_app` so a
#: stale CLAIMED row can be aged-out in milliseconds.
VISIBILITY_TIMEOUT_DEFAULT_SECONDS: Final[int] = 15 * 60

#: Default cadence (seconds) for the periodic visibility-timeout sweep
#: (PRD FR-1.4, TASK-025a). 60s is the PRD's reference cadence: well
#: under the timeout itself (so a stale row is reclaimed within at most
#: timeout + interval seconds of going stale), and well above the
#: per-tick query cost of :meth:`TaskRepository.release_stale_tasks`
#: (one CTE round-trip — negligible). Tests pass a small value (e.g.
#: 0.1s) so the sweep fires inside a sub-second test budget.
SWEEP_INTERVAL_DEFAULT_SECONDS: Final[float] = 60.0

#: Default heartbeat-staleness threshold for the offline-worker sweep
#: (PRD FR-1.4, NFR-1, SC-2, TASK-025b). 120s = 2 min: long enough that a
#: brief network blip or a busy event-loop doesn't spuriously demote a
#: live worker, short enough that SC-2's "kill -9 a worker, peer takes
#: over within seconds, not minutes" lands without forcing the
#: visibility-timeout sweep down to a value that would risk releasing
#: live work. The two sweeps are layered: heartbeat staleness is the
#: primary fast signal; visibility timeout is the slower fallback for
#: cases where the heartbeat is somehow live but the claim is stuck.
HEARTBEAT_TIMEOUT_DEFAULT_SECONDS: Final[int] = 2 * 60

#: Default cadence (seconds) for the offline-worker sweep itself
#: (TASK-025b). 30s is the PRD's reference cadence — half the heartbeat
#: interval (workers tick at ~30s; see TASK-019b1 / TASK-022b2), so a
#: missed heartbeat is detected within at most ``threshold + interval``
#: seconds of going stale. Tests override this to ~0.1s so the suite
#: stays fast.
OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS: Final[float] = 30.0

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


async def _visibility_sweep_loop(
    repo: TaskRepository,
    *,
    sweep_interval: float,
    visibility_timeout: int,
    stop: asyncio.Event,
) -> None:
    """Periodic visibility-timeout sweep coroutine (PRD FR-1.4, TASK-025a).

    Runs under :class:`asyncio.TaskGroup` inside :func:`create_app`'s
    lifespan. Each iteration sleeps up to ``sweep_interval`` seconds; if
    the sleep returns by *timeout* (rather than ``stop`` firing) we call
    :meth:`TaskRepository.release_stale_tasks` once with the configured
    ``visibility_timeout`` — that's the single SQL round-trip that flips
    every aged-out ``CLAIMED`` / ``IN_PROGRESS`` row back to ``PENDING``
    and writes one ``RELEASE`` event per row with ``payload['reason'] =
    'visibility_timeout'`` (audit drift impossible — see the SQL in
    :data:`whilly.adapters.db.repository._RELEASE_STALE_SQL`).

    Why an :class:`asyncio.Event` instead of cancellation?
        Mirrors :func:`whilly.worker.main.run_heartbeat_loop` /
        :func:`whilly.worker.main.run_worker`: lifespan teardown sets
        ``stop`` and the loop drops out of its current
        :func:`asyncio.wait_for` cleanly without raising
        :class:`asyncio.CancelledError`. This means the enclosing
        :class:`asyncio.TaskGroup` exits without an exception group at
        all on the happy shutdown path — fewer corner cases for the
        FastAPI lifespan to catch and re-surface.

    Why catch ``Exception`` per tick?
        A transient asyncpg disconnect, a Postgres pgbouncer restart, or
        a (pathological) FK violation on the audit insert must not kill
        the sweep. The sweep is the *only* mechanism that recovers
        claims orphaned by a hard-killed worker (PRD SC-2 fault
        tolerance); silently turning it off because of one bad SQL
        round-trip would defeat its whole purpose. We log via
        ``logger.exception`` so the failure is visible, then sleep one
        more interval and try again. ``BaseException`` (KeyboardInterrupt,
        SystemExit) is *not* caught — those are signals to unwind the
        process, not the sweep.

    Args:
        repo: The :class:`TaskRepository` bound to the app's pool.
        sweep_interval: Seconds between sweep ticks. Must be > 0.
        visibility_timeout: Age threshold (seconds) passed to
            :meth:`TaskRepository.release_stale_tasks`.
        stop: Lifespan-owned :class:`asyncio.Event`. Set by the lifespan
            teardown to request a clean exit.
    """
    logger.info(
        "visibility_timeout sweep started: interval=%.1fs, timeout=%ds",
        sweep_interval,
        visibility_timeout,
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=sweep_interval)
            # ``stop`` fired during the wait: shutdown path, exit cleanly.
            break
        except TimeoutError:
            # Interval elapsed without a stop — run the sweep tick.
            pass
        try:
            released = await repo.release_stale_tasks(visibility_timeout)
            if released:
                logger.info(
                    "visibility_timeout sweep released %d stale claim(s) (timeout=%ds)",
                    released,
                    visibility_timeout,
                )
        except Exception:
            # Defensive: a single failed sweep tick must not take the
            # loop down for the lifetime of the server. Log and retry
            # on the next interval. See module docstring for the SC-2
            # rationale.
            logger.exception(
                "visibility_timeout sweep tick failed; will retry in %.1fs",
                sweep_interval,
            )
    logger.info("visibility_timeout sweep stopped")


async def _offline_worker_sweep_loop(
    repo: TaskRepository,
    *,
    sweep_interval: float,
    heartbeat_timeout: int,
    stop: asyncio.Event,
) -> None:
    """Periodic offline-worker sweep coroutine (PRD FR-1.4 / NFR-1 / SC-2, TASK-025b).

    Runs as a *second* coroutine inside the same lifespan
    :class:`asyncio.TaskGroup` that supervises the visibility-timeout
    sweep. Each iteration sleeps up to ``sweep_interval`` seconds; if the
    sleep returns by *timeout* (rather than ``stop`` firing) we call
    :meth:`TaskRepository.release_offline_workers` once with the
    configured ``heartbeat_timeout`` — that's the single SQL round-trip
    that flips every still-``online`` worker whose ``last_heartbeat``
    predates the threshold to ``offline``, releases all their CLAIMED /
    IN_PROGRESS tasks back to ``PENDING``, and writes one ``RELEASE``
    event per released task with ``payload['reason'] = 'worker_offline'``.

    Why a second loop instead of folding both sweeps into one?
        The two sweeps observe different signals on different timing
        budgets (see :meth:`TaskRepository.release_offline_workers`'s
        docstring). Coupling them into a single coroutine would force
        the slower ``visibility_timeout`` cadence on the offline sweep
        and make SC-2's fast-recovery target hard to reach without
        also tightening the visibility timeout (which risks spuriously
        cancelling live work). Two coroutines on independent intervals
        keep the policies decoupled. The shared :class:`asyncio.TaskGroup`
        and :class:`asyncio.Event` keep the supervision boundary single.

    Why the same ``stop`` event as the visibility sweep?
        Lifespan teardown is "stop *all* background work" — splitting
        the event would force the lifespan to set both with no behaviour
        difference. One event is simpler and there's no scenario where
        we'd want to stop one sweep without the other.

    ``Exception`` is caught per tick for the same reason as the
    visibility sweep: a transient asyncpg disconnect / pgbouncer restart
    must not silently disable the only mechanism that recovers tasks
    from a hard-killed worker. We log via ``logger.exception`` and try
    again on the next interval. ``BaseException`` propagates.

    Args:
        repo: The :class:`TaskRepository` bound to the app's pool.
        sweep_interval: Seconds between sweep ticks. Must be > 0.
        heartbeat_timeout: Age threshold (seconds) passed to
            :meth:`TaskRepository.release_offline_workers`.
        stop: Lifespan-owned :class:`asyncio.Event`. Set by the lifespan
            teardown to request a clean exit.
    """
    logger.info(
        "offline_worker sweep started: interval=%.1fs, heartbeat_timeout=%ds",
        sweep_interval,
        heartbeat_timeout,
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=sweep_interval)
            break
        except TimeoutError:
            pass
        try:
            released = await repo.release_offline_workers(heartbeat_timeout)
            if released:
                logger.info(
                    "offline_worker sweep released %d task(s) from offline workers (heartbeat_timeout=%ds)",
                    released,
                    heartbeat_timeout,
                )
        except Exception:
            logger.exception(
                "offline_worker sweep tick failed; will retry in %.1fs",
                sweep_interval,
            )
    logger.info("offline_worker sweep stopped")


# Bearer-token hashing has moved to :mod:`whilly.adapters.transport.auth`
# (TASK-101) — both the *write* path (registration) and the *read* path
# (per-worker bearer dep, :func:`make_db_bearer_auth`) need the same
# encoding, and centralising it next to the auth dep guarantees they
# stay synchronised across future scheme changes (argon2 / scrypt). The
# legacy module-private name is preserved as an alias so any direct
# importer ``from whilly.adapters.transport.server import _hash_token``
# keeps working without an import-graph churn.
_hash_token = hash_bearer_token


def _generate_worker_id() -> str:
    """Mint a fresh URL-safe ``worker_id`` for a newly-registered worker.

    Format is ``w-<urlsafe>`` — the prefix keeps logs scannable and the
    suffix carries the entropy. Uses :func:`secrets.token_urlsafe` so the
    bytes come from the OS CSPRNG; collisions are vanishingly unlikely
    across any plausible cluster size (see :data:`_WORKER_ID_ENTROPY_BYTES`).
    """
    return f"{_WORKER_ID_PREFIX}{secrets.token_urlsafe(_WORKER_ID_ENTROPY_BYTES)}"


def _resolve_optional_token(arg: str | None, env_name: str) -> str | None:
    """Resolve an *optional* token from an explicit kwarg or the environment.

    Sister of :func:`_resolve_token` for tokens whose absence is a
    legitimate operational state — specifically the legacy
    ``WHILLY_WORKER_TOKEN`` shared bearer (TASK-101). Returns ``None``
    when neither the kwarg nor the env var is set, signalling to
    :func:`make_db_bearer_auth` that the legacy fallback should be
    disabled. Whitespace-only env values are normalised to ``None`` —
    same rule as :func:`_resolve_token`'s loud rejection — so an
    operator leaving ``WHILLY_WORKER_TOKEN= `` in ``.env`` doesn't
    silently re-enable the legacy path.

    Why a separate helper rather than catching :class:`RuntimeError`
    from :func:`_resolve_token`?
        Exception flow for an *optional* config value is misleading —
        the missing-env case here is the v4.2 future shape, not a
        misconfiguration. A dedicated path makes the contract
        legible and keeps :func:`_resolve_token` strict for the
        bootstrap token (which is genuinely required).
    """
    if arg is not None:
        # An explicit empty kwarg is still a misconfiguration —
        # callers should pass ``None`` to disable, not ``""``.
        if not arg.strip():
            raise RuntimeError(
                f"create_app: explicit token for {env_name} must be non-empty when provided; "
                f"pass None (or omit the kwarg) to disable the legacy fallback."
            )
        return arg
    raw = (os.environ.get(env_name) or "").strip()
    return raw or None


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
    visibility_timeout_seconds: int = VISIBILITY_TIMEOUT_DEFAULT_SECONDS,
    sweep_interval_seconds: float = SWEEP_INTERVAL_DEFAULT_SECONDS,
    heartbeat_timeout_seconds: int = HEARTBEAT_TIMEOUT_DEFAULT_SECONDS,
    offline_worker_sweep_interval_seconds: float = OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS,
    event_flush_interval_seconds: float = EVENT_FLUSHER_DEFAULT_FLUSH_INTERVAL_SECONDS,
    event_batch_limit: int = EVENT_FLUSHER_DEFAULT_BATCH_LIMIT,
    event_drain_timeout_seconds: float = EVENT_FLUSHER_DEFAULT_DRAIN_TIMEOUT_SECONDS,
    event_checkpoint_dir: str | None = None,
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
    visibility_timeout_seconds:
        Age threshold for the periodic stale-claim sweep (PRD FR-1.4,
        TASK-025a). A ``CLAIMED`` / ``IN_PROGRESS`` row whose
        ``claimed_at`` predates ``NOW() - this`` is flipped back to
        ``PENDING`` by the next sweep tick. Defaults to
        :data:`VISIBILITY_TIMEOUT_DEFAULT_SECONDS` (15 minutes — the
        PRD's reference value). Tests override this with a small value
        (e.g. ``1``) so they can age out a CLAIMED row in milliseconds.
        Must be ``>= 0``; ``0`` releases every active claim on every
        tick (only useful in tests with fully-controlled clocks).
    sweep_interval_seconds:
        Cadence of the visibility-timeout sweep loop (PRD FR-1.4,
        TASK-025a). Defaults to :data:`SWEEP_INTERVAL_DEFAULT_SECONDS`
        (60s — the PRD's reference cadence). Must be strictly positive
        — a zero or negative interval would tight-loop the database
        and defeat :func:`asyncio.wait_for` as a cancellation point.
    heartbeat_timeout_seconds:
        Heartbeat-staleness threshold for the offline-worker sweep
        (PRD FR-1.4 / NFR-1 / SC-2, TASK-025b). A worker whose
        ``last_heartbeat`` predates ``NOW() - this`` and is still
        ``status = 'online'`` is flipped to ``offline`` by the next
        sweep tick, and all its CLAIMED / IN_PROGRESS tasks are
        released. Defaults to
        :data:`HEARTBEAT_TIMEOUT_DEFAULT_SECONDS` (2 min — well under
        the visibility timeout, so the heartbeat sweep is the primary
        fault-tolerance signal). Must be ``>= 0``; ``0`` flips every
        online worker on every tick (only useful in tests).
    offline_worker_sweep_interval_seconds:
        Cadence of the offline-worker sweep loop (TASK-025b). Defaults
        to :data:`OFFLINE_WORKER_SWEEP_INTERVAL_DEFAULT_SECONDS` (30s
        — half the heartbeat interval, so a missed heartbeat is
        detected within at most ``threshold + interval`` seconds).
        Must be strictly positive for the same reason as
        ``sweep_interval_seconds``.

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
    if sweep_interval_seconds <= 0:
        # Same rationale as ``claim_poll_interval``: zero / negative would
        # tight-loop on Postgres and defeat ``asyncio.wait_for`` as a
        # shutdown rendezvous.
        raise ValueError(
            f"create_app: sweep_interval_seconds must be > 0, got {sweep_interval_seconds!r}; "
            f"a zero or negative interval would tight-loop the database."
        )
    if visibility_timeout_seconds < 0:
        # ``release_stale_tasks`` itself accepts ``0`` (release every
        # active claim, useful in tests), so we only reject genuinely
        # negative values — those would surface as a Postgres error
        # later, but catching at construction time gives a better
        # message.
        raise ValueError(f"create_app: visibility_timeout_seconds must be >= 0, got {visibility_timeout_seconds!r}.")
    if offline_worker_sweep_interval_seconds <= 0:
        # Same rationale as ``sweep_interval_seconds``: zero / negative
        # would tight-loop on Postgres and defeat ``asyncio.wait_for``
        # as the shutdown rendezvous.
        raise ValueError(
            f"create_app: offline_worker_sweep_interval_seconds must be > 0, "
            f"got {offline_worker_sweep_interval_seconds!r}; a zero or negative "
            f"interval would tight-loop the database."
        )
    if heartbeat_timeout_seconds < 0:
        # Same asymmetry as ``visibility_timeout_seconds``: ``0`` is a
        # legal test override (flips every online worker every tick);
        # only genuinely negative values are rejected.
        raise ValueError(f"create_app: heartbeat_timeout_seconds must be >= 0, got {heartbeat_timeout_seconds!r}.")
    # ``WHILLY_WORKER_TOKEN`` is *optional* since TASK-101: the
    # steady-state RPC surface validates per-worker tokens against
    # ``workers.token_hash`` first, and only falls back to the legacy
    # shared bearer when the env var is set (with a one-shot
    # deprecation warning). The bootstrap token remains mandatory —
    # ``POST /workers/register`` is the only thing that can mint
    # per-worker tokens, so without it the cluster cannot grow.
    legacy_worker_token = _resolve_optional_token(worker_token, WORKER_TOKEN_ENV)
    resolved_bootstrap_token = _resolve_token(bootstrap_token, BOOTSTRAP_TOKEN_ENV)
    # Construct the repository once at app build time. The repo is a
    # thin wrapper around the pool, so reusing one instance across all
    # requests is both correct and cheaper than instantiating per
    # request. Stashed on app.state below so handlers added in
    # TASK-021c reach the same instance via ``request.app.state``.
    repo = TaskRepository(pool)
    # Bind the auth dependencies *now*, at app-construction time, so a
    # bad token surfaces during ``create_app`` rather than on the first
    # 401 in production. ``make_db_bearer_auth`` is the per-worker
    # bearer surface (TASK-101); legacy ``WHILLY_WORKER_TOKEN`` rides
    # along as the optional one-minor-version fallback.
    bearer_dep: IdentityBindingAuthDependency = make_db_bearer_auth(repo, legacy_token=legacy_worker_token)
    bootstrap_dep: AuthDependency = make_bootstrap_auth(resolved_bootstrap_token)

    # Event flusher (TASK-106). Construction is cheap and non-async — it
    # just allocates an :class:`asyncio.Queue` and stashes config. The
    # actual coroutine is spawned inside the lifespan TaskGroup below.
    flusher_checkpoint_dir = (
        event_checkpoint_dir if event_checkpoint_dir is not None else os.environ.get("WHILLY_EVENT_FLUSHER_STATE_DIR")
    )

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
        # Background-task rendezvous (PRD FR-1.4, TASK-025a). The sweep
        # loop checks this event between sleeps; lifespan teardown sets
        # it to request a clean exit. Stashed on app.state so tests can
        # observe it (e.g. assert it was set on shutdown) without
        # reflective inspection.
        sweep_stop = asyncio.Event()
        app.state.sweep_stop = sweep_stop
        # Construct the flusher inside the lifespan so its
        # :class:`asyncio.Queue` is bound to the running event loop
        # (constructing it at module import time would bind the queue
        # to whatever loop happened to be current then — fine in tests
        # but a foot-gun in multi-loop deployments).
        flusher = EventFlusher(
            pool,
            batch_limit=event_batch_limit,
            flush_interval=event_flush_interval_seconds,
            drain_timeout=event_drain_timeout_seconds,
            checkpoint_dir=flusher_checkpoint_dir,
        )
        app.state.event_flusher = flusher
        app.state.event_queue = flusher.queue
        # ``event_flusher_idle_polls`` lives on the flusher instance
        # itself (see :class:`EventFlusher.idle_polls`); validators read
        # it via ``app.state.event_flusher.idle_polls`` (VAL-OBS-013).
        # Late-bind the flusher onto the repo so the per-task TRIZ FAIL
        # hook routes ``triz.contradiction`` / ``triz.error`` rows
        # through the bulk-INSERT batcher (VAL-CROSS-021 contract pin:
        # the cross-area assertion explicitly names the lifespan
        # flusher as the canonical carrier). The repo was constructed
        # before lifespan entry — we cannot pass the flusher in via
        # :class:`TaskRepository.__init__` because the flusher's
        # :class:`asyncio.Queue` must bind to the running loop, which
        # only exists inside the lifespan. The setter is idempotent
        # and lifespan teardown re-clears it via ``None`` so subsequent
        # lifespan cycles (test harnesses re-entering the same app)
        # don't accumulate stale references.
        repo.attach_event_flusher(flusher)
        try:
            # ``asyncio.TaskGroup`` owns the periodic background sweeps
            # for the duration of the app's lifespan. Tasks are created
            # on enter; on exit we set ``sweep_stop`` (shared by both
            # sweeps) and the TaskGroup awaits each loop to drain its
            # current tick. Neither loop raises CancelledError on the
            # happy path, so the TaskGroup exits without an exception
            # group at all — the only way out is the ``stop`` event
            # firing.
            #
            # Two coroutines, one supervision boundary (TASK-025a +
            # TASK-025b):
            #
            # * ``whilly-visibility-sweep`` — visibility-timeout sweep
            #   on ``claimed_at`` (PRD FR-1.4, slow fallback for tasks
            #   stuck without a heartbeat signal).
            # * ``whilly-offline-worker-sweep`` — offline-worker sweep
            #   on ``last_heartbeat`` (PRD NFR-1 / SC-2, fast primary
            #   fault-tolerance path).
            #
            # See PRD R-1 / SC-2 for the layered-recovery rationale.
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    _visibility_sweep_loop(
                        repo,
                        sweep_interval=sweep_interval_seconds,
                        visibility_timeout=visibility_timeout_seconds,
                        stop=sweep_stop,
                    ),
                    name="whilly-visibility-sweep",
                )
                tg.create_task(
                    _offline_worker_sweep_loop(
                        repo,
                        sweep_interval=offline_worker_sweep_interval_seconds,
                        heartbeat_timeout=heartbeat_timeout_seconds,
                        stop=sweep_stop,
                    ),
                    name="whilly-offline-worker-sweep",
                )
                # Lifespan event flusher (TASK-106, VAL-OBS-001..017).
                # Owned by the same TaskGroup so a hard crash in any of
                # the three coroutines unwinds the whole supervision
                # boundary; ``stop`` is shared so a single signal stops
                # all three at once.
                event_flusher_task = tg.create_task(
                    flusher.run(sweep_stop),
                    name=EVENT_FLUSHER_TASK_NAME,
                )
                app.state.event_flusher_task = event_flusher_task
                try:
                    yield
                finally:
                    # Signal both loops to stop. The TaskGroup's
                    # __aexit__ then awaits each draining sweep
                    # coroutine — if either is mid-SQL we wait for that
                    # single round-trip to complete (no half-flushed
                    # audit events). Setting the event is idempotent;
                    # safe under normal-path and crash-path exits alike.
                    sweep_stop.set()
                    # Drain the flusher queue *before* the TaskGroup
                    # awaits the run coroutine — events enqueued just
                    # before SIGTERM must reach the DB before the app
                    # exits (VAL-OBS-007 / VAL-OBS-015). This call is
                    # bounded by ``drain_timeout`` so a wedged Postgres
                    # cannot hold the process forever.
                    try:
                        await flusher.drain()
                    except Exception:
                        # Defensive: drain() must not raise out of the
                        # finally block (would mask the original
                        # exception). Log and let the TaskGroup unwind.
                        logger.exception("event_flusher drain raised on shutdown")
        finally:
            # Pool ownership is the caller's; we just drop our reference
            # so handlers don't keep a dangling pointer if the caller
            # closes the pool but reuses the app object (e.g. test
            # harnesses that share a pool across multiple sub-apps).
            app.state.pool = None
            app.state.repo = None
            app.state.sweep_stop = None
            app.state.event_flusher = None
            app.state.event_queue = None
            app.state.event_flusher_task = None
            # Drop the repo's flusher reference so subsequent lifespan
            # cycles (e.g. test harnesses re-entering the same app)
            # don't pick up the now-defunct queue / coroutine.
            repo.attach_event_flusher(None)
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
            await repo.register_worker(worker_id, payload.hostname, token_hash, payload.owner_email)
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
    async def heartbeat(request: Request, worker_id: str, payload: HeartbeatRequest) -> HeartbeatResponse:
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
            #
            # Order matters: the body↔path 400 check stays AHEAD of
            # the token-owner 403 check below so VAL-AUTH-024's
            # ``(400, 401, 403)`` envelope keeps both branches
            # independently exercised (one test for the 400 schema
            # mismatch, one test for the 403 cross-worker bearer).
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"worker_id in body does not match path: path={worker_id!r} body={payload.worker_id!r}"),
            )
        # Token-owner check (TASK-101 scrutiny round-1 fix): worker A's
        # bearer cannot heartbeat worker B even when body == path == B.
        _require_token_owner(request, worker_id)
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
    async def claim(request: Request, payload: ClaimRequest) -> Response | ClaimResponse:
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
        # Token-owner check (TASK-101 scrutiny round-1 fix): worker A's
        # bearer cannot claim a task on behalf of worker B even though
        # the path carries no identity.
        _require_token_owner(request, payload.worker_id)
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
    async def complete_task(
        request: Request, task_id: str, payload: CompleteRequest
    ) -> CompleteResponse | JSONResponse:
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
        # Token-owner check (TASK-101 scrutiny round-1 fix): worker A's
        # bearer cannot complete a task on behalf of worker B even if
        # B legitimately claimed it.
        _require_token_owner(request, payload.worker_id)
        try:
            updated = await repo.complete_task(task_id, payload.version, payload.cost_usd)
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
            "complete_task: worker=%s task=%s version=%d cost_usd=%s → DONE",
            payload.worker_id,
            updated.id,
            updated.version,
            payload.cost_usd,
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
    async def fail_task(request: Request, task_id: str, payload: FailRequest) -> FailResponse | JSONResponse:
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
        # Token-owner check (TASK-101 scrutiny round-1 fix): worker A's
        # bearer cannot fail a task on behalf of worker B.
        _require_token_owner(request, payload.worker_id)
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
    async def release_task(request: Request, task_id: str, payload: ReleaseRequest) -> ReleaseResponse | JSONResponse:
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
        # Token-owner check (TASK-101 scrutiny round-1 fix): worker A's
        # bearer cannot release a task on behalf of worker B.
        _require_token_owner(request, payload.worker_id)
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

    @app.get(
        "/api/v1/plans/{plan_id}",
        # Public read-only surface — no auth dependency. Operators
        # can curl this from a dashboard / kubectl describe plan
        # without minting a worker token, and the response carries
        # only metadata that is already visible via ``whilly plan
        # show``. If we later add per-tenant scoping the auth dep
        # slots in here without touching the response shape.
    )
    async def get_plan(plan_id: str) -> JSONResponse:
        """Return ``{id, name, github_issue_ref, prd_file}`` for a single plan (VAL-FORGE-012).

        404 if the plan does not exist. The ``github_issue_ref`` field
        is ``null`` for plans created via ``whilly init`` (no GitHub
        anchor) and the canonical ``owner/repo/<number>`` triple for
        plans created via ``whilly forge intake``. The ``prd_file``
        field carries the absolute path of the generated PRD markdown
        file for Forge-originated plans (VAL-FORGE-005); ``null`` for
        plans without a generated PRD.

        Why a single endpoint and not a list / collection surface?
            VAL-FORGE-012 only pins single-row lookups by id; a
            collection endpoint (``GET /api/v1/plans``) would expand
            the auth surface (operator listing across tenants)
            without serving any current contract. Keep the surface
            minimal.
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, github_issue_ref, prd_file FROM plans WHERE id = $1",
                plan_id,
            )
        if row is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"plan {plan_id!r} not found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return JSONResponse(
            {
                "id": row["id"],
                "name": row["name"],
                "github_issue_ref": row["github_issue_ref"],
                "prd_file": row["prd_file"],
            }
        )

    return app


def _require_token_owner(request: Request, claimed_worker_id: str) -> None:
    """Reject cross-worker bearer use with 403 (TASK-101 scrutiny round-1 fix / VAL-AUTH-024).

    :func:`whilly.adapters.transport.auth.make_db_bearer_auth` stashes
    the DB-resolved ``worker_id`` on
    ``request.state.authenticated_worker_id`` when the per-worker hash
    lookup hits. This helper compares that resolved identity against
    the ``claimed_worker_id`` the route handler observed (from the
    request body or path) and raises 403 on mismatch — i.e. worker A's
    token cannot be used to ``heartbeat``/``claim``/``complete``/
    ``fail``/``release`` as worker B.

    Why 403 (not 401)?
        RFC 7235 §3.1 / RFC 6750 §3: 401 means "I don't know who you
        are"; 403 means "I know who you are, you can't do this". The
        token authenticated successfully — auth is *not* the failure;
        the *operation* is forbidden. VAL-AUTH-024's evidence clause
        accepts ``(400, 401, 403)``, so 403 is contract-compliant; we
        choose 403 to keep schema-validation 400s separate from
        authorisation 403s in operator dashboards / log queries.

    Legacy fallback (``WHILLY_WORKER_TOKEN`` shared bearer) — no-op
        On the legacy path the dep stashes ``None`` because the
        shared cluster token cannot identify a specific worker. We
        treat ``None`` as a no-op so the one-minor-version legacy
        compat window (VAL-AUTH-030/031/034) stays green: the legacy
        bearer is allowed to act as "any worker" exactly because
        identity is unknown. When the legacy fallback is removed in
        v4.2 this branch collapses (the dep itself will raise 401 on
        every non-DB-resolved request).

    Why a module-level helper rather than a FastAPI dependency?
        Each handler already extracts ``payload.worker_id`` from a
        typed request schema, so the comparison is one expression at
        the top of the handler. A separate ``Depends`` would have to
        re-parse the body or read the path parameter via ``Request``
        — twice the work for the same outcome. Inlining the call
        keeps the auth boundary explicit at the call site.
    """
    authenticated = getattr(request.state, "authenticated_worker_id", None)
    if authenticated is None:
        # Legacy fallback path (identity unknown) or an unauthenticated
        # request that somehow reached the handler — neither case can
        # be enforced here without regressing VAL-AUTH-030/031/034.
        return
    if authenticated != claimed_worker_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"token owner {authenticated!r} cannot act as worker {claimed_worker_id!r}"),
        )


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
