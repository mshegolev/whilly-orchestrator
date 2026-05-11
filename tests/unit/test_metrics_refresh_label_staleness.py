"""Regression test for the gauge-label staleness bug in
:mod:`whilly.api.metrics` (scrutiny-m3 round 3 finding #5b).

The bug:
    :func:`_refresh_claims_pending` and :func:`_refresh_plan_budgets`
    repopulate gauge label series each tick but never DROP labels for
    plan_ids that have fallen out of the current SQL result set. The
    consequence is that a plan_id that briefly had PENDING tasks (or a
    non-null budget) keeps its ``whilly_claims_pending{plan_id=X}`` /
    ``whilly_plan_budget_remaining_usd{plan_id=X}`` series forever with
    its last-seen value — violating VAL-M3-METRICS-018 (label
    cardinality bounded) and VAL-M3-METRICS-903 (gauge ↔ SQL parity).

The test seeds two plans into a stub asyncpg-shaped pool, calls
:func:`refresh_gauges`, drops one plan from the stub's result set,
calls :func:`refresh_gauges` again, and asserts that
:func:`generate_latest` shows only the surviving ``plan_id`` for both
the claims_pending and plan_budget_remaining_usd families.

The test is also a forward-going contract: when a plan re-appears in
the SQL result set after being dropped, the gauge re-emits it with the
new value. The closing assertion confirms this round-trip path so
future refactors don't accidentally re-introduce the staleness.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from prometheus_client import generate_latest

from whilly.api import metrics as metrics_module
from whilly.api.metrics import refresh_gauges


class _Row(dict):
    """A dict that also exposes ``row['key']`` semantics — asyncpg's
    Record protocol used by the metrics module is just subscript +
    iteration, so a plain dict works."""


class _StubConn:
    """asyncpg-shaped Connection stub returning canned rows.

    Only the two surfaces that ``whilly.api.metrics`` actually uses are
    implemented:

    * ``fetch(sql)`` → list of rows; we discriminate by SQL substring so
      one stub can serve both ``_CLAIMS_PENDING_SQL`` (selects FROM tasks)
      and ``_PLAN_BUDGET_SQL`` (selects FROM plans).
    * ``fetchrow(sql, *args)`` → single row for the workers_online query
      (which the metrics module always issues as the first refresh).
    """

    def __init__(
        self,
        *,
        claims_rows: list[_Row],
        budget_rows: list[_Row],
        workers_count: int = 0,
    ) -> None:
        self._claims_rows = claims_rows
        self._budget_rows = budget_rows
        self._workers_count = workers_count

    async def fetch(self, sql: str, *args: Any) -> list[_Row]:
        if "FROM tasks" in sql:
            return list(self._claims_rows)
        if "FROM plans" in sql:
            return list(self._budget_rows)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> _Row:
        if "FROM workers" in sql:
            return _Row(n=self._workers_count)
        return _Row()


class _AcquireCtx:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StubPool:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)

    def set_conn(self, conn: _StubConn) -> None:
        self._conn = conn


def _reset_metrics_state() -> None:
    for labeled in (metrics_module.claims_pending, metrics_module.plan_budget_remaining_usd):
        if hasattr(labeled, "_metrics"):
            labeled._metrics.clear()
    metrics_module.workers_online.set(0)
    metrics_module._seen_claims_pending_plan_ids.clear()
    metrics_module._seen_plan_budget_plan_ids.clear()


@pytest.fixture(autouse=True)
def _autoreset() -> Iterator[None]:
    _reset_metrics_state()
    yield
    _reset_metrics_state()


def _exposition_plan_ids_for(metric_name: str, body: bytes) -> set[str]:
    """Parse the prometheus exposition body for label values of ``metric_name``."""
    import re

    text = body.decode("utf-8")
    pattern = re.compile(rf'^{re.escape(metric_name)}\{{plan_id="([^"]+)"\}}\s+', re.MULTILINE)
    return set(pattern.findall(text))


def test_claims_pending_drops_plan_id_when_no_longer_in_sql() -> None:
    """Plan A's claims_pending series should disappear once A stops
    appearing in the SQL result set. VAL-M3-METRICS-018, -903."""
    conn = _StubConn(
        claims_rows=[_Row(plan_id="plan-A", n=3), _Row(plan_id="plan-B", n=5)],
        budget_rows=[],
    )
    pool = _StubPool(conn)

    asyncio.run(refresh_gauges(pool))

    body = generate_latest()
    seen = _exposition_plan_ids_for("whilly_claims_pending", body)
    assert seen == {"plan-A", "plan-B"}, f"first refresh exposition: {seen!r}"

    pool.set_conn(_StubConn(claims_rows=[_Row(plan_id="plan-B", n=5)], budget_rows=[]))
    asyncio.run(refresh_gauges(pool))

    body = generate_latest()
    seen = _exposition_plan_ids_for("whilly_claims_pending", body)
    assert seen == {"plan-B"}, f"after drop, exposition still has stale labels: {seen!r}"


def test_plan_budget_drops_plan_id_when_no_longer_in_sql() -> None:
    """Plan A's plan_budget_remaining_usd series should disappear once
    A stops appearing in the SQL result set. VAL-M3-METRICS-018, -903."""
    conn = _StubConn(
        claims_rows=[],
        budget_rows=[
            _Row(id="plan-A", budget_usd=Decimal("10"), spent_usd=Decimal("2")),
            _Row(id="plan-B", budget_usd=Decimal("5"), spent_usd=Decimal("1")),
        ],
    )
    pool = _StubPool(conn)

    asyncio.run(refresh_gauges(pool))

    body = generate_latest()
    seen = _exposition_plan_ids_for("whilly_plan_budget_remaining_usd", body)
    assert seen == {"plan-A", "plan-B"}, f"first refresh exposition: {seen!r}"

    pool.set_conn(
        _StubConn(
            claims_rows=[],
            budget_rows=[_Row(id="plan-B", budget_usd=Decimal("5"), spent_usd=Decimal("1"))],
        )
    )
    asyncio.run(refresh_gauges(pool))

    body = generate_latest()
    seen = _exposition_plan_ids_for("whilly_plan_budget_remaining_usd", body)
    assert seen == {"plan-B"}, f"after drop, exposition still has stale labels: {seen!r}"


def test_dropped_plan_can_reappear_with_new_value() -> None:
    """Round-trip: A → drop A → A re-appears with new value should
    re-emit the series at the new value (no orphaned old value)."""
    conn = _StubConn(
        claims_rows=[_Row(plan_id="plan-A", n=3)],
        budget_rows=[_Row(id="plan-A", budget_usd=Decimal("10"), spent_usd=Decimal("0"))],
    )
    pool = _StubPool(conn)
    asyncio.run(refresh_gauges(pool))

    pool.set_conn(_StubConn(claims_rows=[], budget_rows=[]))
    asyncio.run(refresh_gauges(pool))
    body = generate_latest()
    assert _exposition_plan_ids_for("whilly_claims_pending", body) == set()
    assert _exposition_plan_ids_for("whilly_plan_budget_remaining_usd", body) == set()

    pool.set_conn(
        _StubConn(
            claims_rows=[_Row(plan_id="plan-A", n=42)],
            budget_rows=[_Row(id="plan-A", budget_usd=Decimal("100"), spent_usd=Decimal("25"))],
        )
    )
    asyncio.run(refresh_gauges(pool))

    body = generate_latest()
    assert _exposition_plan_ids_for("whilly_claims_pending", body) == {"plan-A"}
    assert _exposition_plan_ids_for("whilly_plan_budget_remaining_usd", body) == {"plan-A"}

    new_pending = metrics_module.claims_pending.labels(plan_id="plan-A")._value.get()
    new_remaining = metrics_module.plan_budget_remaining_usd.labels(plan_id="plan-A")._value.get()
    assert new_pending == 42.0
    assert abs(new_remaining - 75.0) < 1e-9


def test_remove_swallows_keyerror_when_label_already_gone() -> None:
    """If a previously-seen plan_id is missing from the gauge's
    ``_metrics`` mapping (e.g. a manual reset wiped it), the next
    refresh tick must NOT raise: the bug-fix wraps :meth:`Gauge.remove`
    in ``try/except KeyError``."""
    conn = _StubConn(claims_rows=[_Row(plan_id="plan-A", n=1)], budget_rows=[])
    pool = _StubPool(conn)
    asyncio.run(refresh_gauges(pool))

    metrics_module.claims_pending._metrics.clear()
    pool.set_conn(_StubConn(claims_rows=[], budget_rows=[]))
    asyncio.run(refresh_gauges(pool))

    body = generate_latest()
    assert _exposition_plan_ids_for("whilly_claims_pending", body) == set()


def test_seen_state_is_module_level_and_inspectable() -> None:
    """The bug-fix must keep its tracked-set state visible at module
    level so tests can reset it; this guards against the implementation
    re-hiding it inside a function-local closure (which would re-leak
    state across tests)."""
    assert hasattr(metrics_module, "_seen_claims_pending_plan_ids")
    assert hasattr(metrics_module, "_seen_plan_budget_plan_ids")
    assert isinstance(metrics_module._seen_claims_pending_plan_ids, set)
    assert isinstance(metrics_module._seen_plan_budget_plan_ids, set)
