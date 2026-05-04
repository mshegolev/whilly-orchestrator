"""Prometheus metrics surface for the M3 control plane (m3-prometheus-metrics).

Module-level :class:`prometheus_client.Counter` / :class:`Gauge` /
:class:`Histogram` instances expose the four custom metric families
required by the validation contract (VAL-M3-METRICS-007..013) plus the
``prometheus-fastapi-instrumentator`` adapter that records the standard
HTTP request / latency / response-size series automatically.

Custom metric families
----------------------
* ``whilly_claims_total{plan_id,worker_id}`` — Counter, incremented by
  the ``POST /tasks/claim`` handler on a successful PENDING → CLAIMED
  transition. Monotonic; RELEASE / visibility-timeout sweeps DO NOT
  decrement (VAL-M3-METRICS-902).
* ``whilly_completes_total{plan_id,worker_id}`` — Counter, incremented
  on ``POST /tasks/{id}/complete`` success.
* ``whilly_fails_total{plan_id,worker_id,reason}`` — Counter, incremented
  on ``POST /tasks/{id}/fail`` success. ``reason`` mirrors the request
  body field so the dashboard can attribute failures by category.
* ``whilly_workers_online`` — Gauge, refreshed every ``interval`` seconds
  from ``SELECT count(*) FROM workers WHERE status='online' AND
  last_heartbeat > NOW() - INTERVAL '<heartbeat_timeout> seconds'``.
* ``whilly_claims_pending{plan_id}`` — Gauge, one series per plan_id
  with PENDING tasks. Refreshed every tick from
  ``SELECT plan_id, count(*) FROM tasks WHERE status='PENDING' GROUP BY plan_id``.
* ``whilly_plan_budget_remaining_usd{plan_id}`` — Gauge, one series
  per plan with a non-null ``budget_usd``. Computed as
  ``max(0, budget_usd - spent_usd)``.
* ``whilly_claim_long_poll_duration_seconds`` — Histogram, observed
  once per ``POST /tasks/claim`` handler return (200 OR 204). Buckets
  pinned by VAL-M3-METRICS-901 to span the worker's 30s long-poll
  budget.

Instrumentator wiring
---------------------
:func:`instrument_app` attaches a ``prometheus-fastapi-instrumentator``
:class:`Instrumentator` to the FastAPI app with
``excluded_handlers=("/metrics",)`` so scrape requests don't self-record
as ``http_requests_total{handler="/metrics"}`` (VAL-M3-METRICS-005).
The instrumentator does NOT register its own ``/metrics`` route — the
control-plane app declares the endpoint inline so we can gate it with
the bearer-token dependency (VAL-M3-METRICS-004).

Refresh loop
------------
:func:`metrics_refresh_loop` is a coroutine intended to run inside the
:func:`whilly.adapters.transport.server.create_app` lifespan TaskGroup.
It wakes every ``interval`` seconds (default 15s — VAL-M3-METRICS-014)
and re-queries the three gauge series. The loop catches
:class:`Exception` per tick so a transient asyncpg disconnect / pgbouncer
restart does not take the loop down (VAL-M3-METRICS-015) — gauges
retain their last-known-good values until the next successful refresh.
``BaseException`` propagates so cancellation / shutdown still works.

Token gate
----------
The HTTP gate is implemented inline in ``server.py``'s ``/metrics``
handler, but the helper :func:`check_metrics_token` lives here so the
"fail-closed when ``WHILLY_METRICS_TOKEN`` is unset" contract
(VAL-M3-METRICS-020) is the same string in tests and in production.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Final, Protocol

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.registry import REGISTRY
from prometheus_fastapi_instrumentator import Instrumentator

logger = logging.getLogger(__name__)

#: Environment variable holding the bearer token required to scrape
#: ``/metrics``. Mirrored by :data:`METRICS_TOKEN_ENV` in tests so a
#: typo here surfaces in CI immediately (VAL-M3-METRICS-004).
METRICS_TOKEN_ENV: Final[str] = "WHILLY_METRICS_TOKEN"

#: Default refresh cadence for :func:`metrics_refresh_loop` (seconds).
#: VAL-M3-METRICS-014 pins the upper bound at 15 ± 1s; the loop wakes
#: every ``REFRESH_INTERVAL_DEFAULT_SECONDS`` and re-queries the three
#: gauge series. Tests override via the ``interval`` kwarg so the
#: refresh tick fires inside a sub-second budget.
REFRESH_INTERVAL_DEFAULT_SECONDS: Final[float] = 15.0

#: Default heartbeat-staleness threshold (seconds) used by the
#: ``whilly_workers_online`` gauge query. VAL-M3-METRICS-903 names this
#: predicate explicitly: a worker counts as "online" when its
#: ``last_heartbeat`` is newer than ``NOW() - INTERVAL '30 seconds'``.
ONLINE_HEARTBEAT_THRESHOLD_DEFAULT_SECONDS: Final[int] = 30

#: Histogram buckets for ``whilly_claim_long_poll_duration_seconds``.
#: Pinned by VAL-M3-METRICS-901 — the 30s bucket exists because the
#: worker's claim long-poll budget is 30s; the 60s bucket catches
#: outliers caused by event-loop pressure or pathological proxies.
CLAIM_LONG_POLL_BUCKETS: Final[tuple[float, ...]] = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

#: Path of the metrics scrape endpoint. The instrumentator's
#: ``excluded_handlers`` config and the FastAPI route decorator both
#: reference this constant so a future rename only touches one line
#: (VAL-M3-METRICS-005).
METRICS_PATH: Final[str] = "/metrics"


def _make_metrics(
    registry: CollectorRegistry,
) -> tuple[Counter, Counter, Counter, Gauge, Gauge, Gauge, Histogram]:
    """Construct the seven custom metric instances against ``registry``.

    Pulled into a helper so tests can spin a fresh
    :class:`CollectorRegistry` for isolation without re-instantiating
    the module-level instances. Production callers reuse the
    module-level :data:`REGISTRY`.
    """
    claims_total = Counter(
        "whilly_claims_total",
        "Total successful task claims (PENDING → CLAIMED).",
        ("plan_id", "worker_id"),
        registry=registry,
    )
    completes_total = Counter(
        "whilly_completes_total",
        "Total successful task completions (IN_PROGRESS → DONE).",
        ("plan_id", "worker_id"),
        registry=registry,
    )
    fails_total = Counter(
        "whilly_fails_total",
        "Total failed task transitions (CLAIMED|IN_PROGRESS → FAILED).",
        ("plan_id", "worker_id", "reason"),
        registry=registry,
    )
    workers_online = Gauge(
        "whilly_workers_online",
        "Currently-online workers (status='online' AND fresh heartbeat).",
        registry=registry,
    )
    claims_pending = Gauge(
        "whilly_claims_pending",
        "Pending tasks awaiting claim, per plan.",
        ("plan_id",),
        registry=registry,
    )
    plan_budget_remaining_usd = Gauge(
        "whilly_plan_budget_remaining_usd",
        "Remaining USD budget per plan (max(0, budget_usd - spent_usd)).",
        ("plan_id",),
        registry=registry,
    )
    claim_long_poll_duration_seconds = Histogram(
        "whilly_claim_long_poll_duration_seconds",
        "Wall-clock duration of POST /tasks/claim handler invocations.",
        buckets=CLAIM_LONG_POLL_BUCKETS,
        registry=registry,
    )
    return (
        claims_total,
        completes_total,
        fails_total,
        workers_online,
        claims_pending,
        plan_budget_remaining_usd,
        claim_long_poll_duration_seconds,
    )


(
    claims_total,
    completes_total,
    fails_total,
    workers_online,
    claims_pending,
    plan_budget_remaining_usd,
    claim_long_poll_duration_seconds,
) = _make_metrics(REGISTRY)


class _PoolLike(Protocol):
    """Structural type for the asyncpg pool the refresh loop consumes."""

    def acquire(self) -> Any: ...


def check_metrics_token(authorization: str | None, *, expected_token: str | None) -> bool:
    """Return True iff ``authorization`` carries a valid metrics bearer.

    Fail-closed when ``expected_token`` is ``None`` or empty/whitespace
    (VAL-M3-METRICS-020): even with the env var unset, the endpoint
    must NOT serve metrics publicly. Constant-time comparison via
    :func:`secrets.compare_digest` so a timing oracle on the bearer
    cannot leak the token bit by bit.
    """
    import secrets as _secrets

    if expected_token is None or not expected_token.strip():
        return False
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    presented = authorization.split(" ", 1)[1].strip()
    if not presented:
        return False
    return _secrets.compare_digest(presented, expected_token)


def resolve_metrics_token(explicit: str | None) -> str | None:
    """Resolve the metrics bearer from kwarg or env, returning ``None`` when unset.

    Used by :func:`whilly.adapters.transport.server.create_app` so the
    token is captured once at app build time and the handler closes
    over a stable value rather than re-reading the env on every
    request (which would defeat the rotate-on-restart contract,
    VAL-M3-METRICS-019).
    """
    if explicit is not None:
        return explicit if explicit.strip() else None
    raw = os.environ.get(METRICS_TOKEN_ENV)
    if raw is None:
        return None
    raw = raw.strip()
    return raw if raw else None


def render_metrics(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """Render the Prometheus exposition payload + content-type tuple.

    Wraps :func:`prometheus_client.generate_latest` so callers don't
    need a direct dependency on the rendering surface. Returns
    ``(body, content_type)`` — the content type is the canonical
    ``text/plain; version=0.0.4; charset=utf-8`` value Prometheus
    expects (VAL-M3-METRICS-006).
    """
    return generate_latest(registry), CONTENT_TYPE_LATEST


def instrument_app(
    app: Any,
    *,
    excluded_handlers: tuple[str, ...] = (METRICS_PATH,),
) -> Instrumentator:
    """Attach the ``prometheus-fastapi-instrumentator`` to ``app``.

    Records the standard HTTP request / latency / response-size series
    on every non-excluded route. The ``/metrics`` path is excluded by
    default so a Prometheus scrape does not self-record as
    ``http_requests_total{handler="/metrics"}`` (VAL-M3-METRICS-005).

    Returns the :class:`Instrumentator` instance so callers can attach
    additional metric definitions (none today; placeholder for future
    M3 work).
    """
    instrumentator = Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        should_instrument_requests_inprogress=False,
        excluded_handlers=list(excluded_handlers),
        env_var_name="WHILLY_METRICS_INSTRUMENT",
    )
    instrumentator.instrument(app)
    return instrumentator


_ONLINE_WORKERS_SQL: Final[str] = """
SELECT COUNT(*)::bigint AS n
FROM workers
WHERE status = 'online'
  AND last_heartbeat > NOW() - make_interval(secs => $1)
"""

_CLAIMS_PENDING_SQL: Final[str] = """
SELECT plan_id, COUNT(*)::bigint AS n
FROM tasks
WHERE status = 'PENDING'
GROUP BY plan_id
"""

_PLAN_BUDGET_SQL: Final[str] = """
SELECT id, budget_usd, spent_usd
FROM plans
WHERE budget_usd IS NOT NULL
"""


async def _refresh_workers_online(
    pool: _PoolLike,
    *,
    online_threshold_seconds: int,
    gauge: Gauge = workers_online,
) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ONLINE_WORKERS_SQL, online_threshold_seconds)
    n = int(row["n"] if row is not None else 0)
    gauge.set(n)


async def _refresh_claims_pending(
    pool: _PoolLike,
    *,
    gauge: Gauge = claims_pending,
) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CLAIMS_PENDING_SQL)
    seen: set[str] = set()
    for row in rows:
        plan_id = row["plan_id"]
        gauge.labels(plan_id=plan_id).set(int(row["n"]))
        seen.add(plan_id)
    return seen


async def _refresh_plan_budgets(
    pool: _PoolLike,
    *,
    gauge: Gauge = plan_budget_remaining_usd,
) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PLAN_BUDGET_SQL)
    seen: set[str] = set()
    for row in rows:
        budget = row["budget_usd"]
        spent = row["spent_usd"] or Decimal(0)
        remaining = budget - spent
        if remaining < 0:
            remaining = Decimal(0)
        gauge.labels(plan_id=row["id"]).set(float(remaining))
        seen.add(row["id"])
    return seen


_seen_claims_pending_plan_ids: set[str] = set()
_seen_plan_budget_plan_ids: set[str] = set()


def _drop_stale_labels(gauge: Gauge, stale_plan_ids: set[str]) -> None:
    for plan_id in stale_plan_ids:
        try:
            gauge.remove(plan_id)
        except KeyError:
            pass


async def refresh_gauges(
    pool: _PoolLike,
    *,
    online_threshold_seconds: int = ONLINE_HEARTBEAT_THRESHOLD_DEFAULT_SECONDS,
) -> None:
    """One-shot refresh of every DB-backed gauge.

    Helper for :func:`metrics_refresh_loop` and for tests that want to
    drive a single tick deterministically rather than waiting on the
    loop's interval. Splits each gauge family into a sub-task so a
    failure in one query family doesn't prevent the others from
    refreshing — the loop's outer ``except Exception`` then logs the
    composite failure.

    After repopulating each per-plan gauge, plan_ids that were present
    on the previous tick but absent from the current SQL result set are
    removed from the gauge's label registry (VAL-M3-METRICS-018 /
    VAL-M3-METRICS-903 — bounded cardinality + parity with SQL truth).
    Without this, a plan whose tasks all leave the PENDING bucket would
    keep emitting its last-known ``whilly_claims_pending`` value
    indefinitely.
    """
    global _seen_claims_pending_plan_ids, _seen_plan_budget_plan_ids
    await _refresh_workers_online(pool, online_threshold_seconds=online_threshold_seconds)
    seen_claims_pending = await _refresh_claims_pending(pool)
    seen_plan_budgets = await _refresh_plan_budgets(pool)
    _drop_stale_labels(claims_pending, _seen_claims_pending_plan_ids - seen_claims_pending)
    _drop_stale_labels(plan_budget_remaining_usd, _seen_plan_budget_plan_ids - seen_plan_budgets)
    _seen_claims_pending_plan_ids = seen_claims_pending
    _seen_plan_budget_plan_ids = seen_plan_budgets


async def metrics_refresh_loop(
    pool: _PoolLike,
    stop: asyncio.Event,
    *,
    interval: float = REFRESH_INTERVAL_DEFAULT_SECONDS,
    online_threshold_seconds: int = ONLINE_HEARTBEAT_THRESHOLD_DEFAULT_SECONDS,
    on_tick: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Periodic gauge-refresh coroutine.

    Wakes every ``interval`` seconds and re-queries the three gauge
    series via :func:`refresh_gauges`. Catches :class:`Exception` per
    tick so a transient DB hiccup doesn't take the loop down
    (VAL-M3-METRICS-015) — gauges retain their last-known-good values
    until the next successful refresh. The shared ``stop`` event
    drives shutdown: setting it interrupts the next sleep and the
    coroutine returns cleanly.

    ``on_tick`` is an optional async callback fired AFTER each refresh
    attempt (success or failure). Tests inject it to deterministically
    rendezvous with the refresh cadence without monkey-patching
    :func:`asyncio.sleep`.
    """
    if interval <= 0:
        raise ValueError(f"metrics_refresh_loop: interval must be > 0, got {interval!r}")
    logger.info(
        "metrics_refresh started: interval=%.1fs online_threshold=%ds",
        interval,
        online_threshold_seconds,
    )
    try:
        await refresh_gauges(pool, online_threshold_seconds=online_threshold_seconds)
    except Exception:
        logger.exception("metrics_refresh: initial refresh failed; retrying on next tick")
    if on_tick is not None:
        try:
            await on_tick()
        except Exception:
            logger.exception("metrics_refresh: on_tick callback raised (initial)")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except TimeoutError:
            pass
        try:
            await refresh_gauges(pool, online_threshold_seconds=online_threshold_seconds)
        except Exception:
            logger.exception("metrics_refresh tick failed; will retry in %.1fs", interval)
        if on_tick is not None:
            try:
                await on_tick()
            except Exception:
                logger.exception("metrics_refresh: on_tick callback raised")
    logger.info("metrics_refresh stopped")


METRICS_REFRESH_TASK_NAME: Final[str] = "whilly-metrics-refresh"


__all__ = [
    "CLAIM_LONG_POLL_BUCKETS",
    "METRICS_PATH",
    "METRICS_REFRESH_TASK_NAME",
    "METRICS_TOKEN_ENV",
    "ONLINE_HEARTBEAT_THRESHOLD_DEFAULT_SECONDS",
    "REFRESH_INTERVAL_DEFAULT_SECONDS",
    "check_metrics_token",
    "claim_long_poll_duration_seconds",
    "claims_pending",
    "claims_total",
    "completes_total",
    "fails_total",
    "instrument_app",
    "metrics_refresh_loop",
    "plan_budget_remaining_usd",
    "refresh_gauges",
    "render_metrics",
    "resolve_metrics_token",
    "workers_online",
]
