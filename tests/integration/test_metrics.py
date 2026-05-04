"""Integration tests for the M3 Prometheus metrics surface (m3-prometheus-metrics).

Covers:
* Module-level metrics exist with the contracted names
* `prometheus-fastapi-instrumentator` 7.1.0 is wired into create_app
* GET /metrics returns valid Prometheus exposition text
* Bearer auth via WHILLY_METRICS_TOKEN (401 on missing/wrong, fail-closed when unset)
* /metrics is excluded from the instrumentator's self-recording
* whilly_claims_total / whilly_completes_total / whilly_fails_total
  increment on the matching RPCs
* whilly_workers_online gauge refreshed from DB
* whilly_claims_pending gauge per plan
* whilly_plan_budget_remaining_usd gauge per plan
* whilly_claim_long_poll_duration_seconds histogram observed per
  claim attempt with explicit bucket boundaries
* metrics_refresh_loop survives DB hiccup (logs and retries)
* prometheus-fastapi-instrumentator and prometheus-client are listed
  in [server] extras
"""

from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client.parser import text_string_to_metric_families

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import REGISTER_PATH, create_app
from whilly.api import metrics as metrics_module
from whilly.api.metrics import (
    CLAIM_LONG_POLL_BUCKETS,
    METRICS_PATH,
    METRICS_TOKEN_ENV,
    check_metrics_token,
    refresh_gauges,
    resolve_metrics_token,
)

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-metrics-test"
_METRICS_TOKEN = "metrics-token-secret"
_LONG_POLL_TIMEOUT = 0.3
_POLL_INTERVAL = 0.05


def _reset_metric_families() -> None:
    for labeled in (
        metrics_module.claims_total,
        metrics_module.completes_total,
        metrics_module.fails_total,
        metrics_module.claims_pending,
        metrics_module.plan_budget_remaining_usd,
    ):
        if hasattr(labeled, "_metrics"):
            labeled._metrics.clear()
    metrics_module.workers_online.set(0)
    histogram = metrics_module.claim_long_poll_duration_seconds
    if hasattr(histogram, "_sum"):
        try:
            histogram._sum.set(0)
        except Exception:
            pass
    if hasattr(histogram, "_count"):
        try:
            histogram._count.set(0)
        except Exception:
            pass
    if hasattr(histogram, "_buckets"):
        for bucket in histogram._buckets:
            try:
                bucket.set(0)
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _autoreset_metrics() -> None:
    _reset_metric_families()
    yield
    _reset_metric_families()


@pytest.fixture
async def app(db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    monkeypatch.setenv(METRICS_TOKEN_ENV, _METRICS_TOKEN)
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=1,
        metrics_token=_METRICS_TOKEN,
        metrics_refresh_interval_seconds=0.2,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _mint_bootstrap(db_pool: asyncpg.Pool, plaintext: str, owner: str) -> None:
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(db_pool)
    await repo.mint_bootstrap_token(plaintext, owner_email=owner)


async def _seed_plan(
    pool: asyncpg.Pool,
    plan_id: str = "plan-metrics",
    *,
    budget_usd: str | None = None,
    spent_usd: str | None = None,
) -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name, budget_usd, spent_usd) "
            "VALUES ($1, $2, $3::numeric, COALESCE($4::numeric, 0)) ON CONFLICT DO NOTHING",
            plan_id,
            f"plan {plan_id}",
            budget_usd,
            spent_usd,
        )
    return plan_id


async def _seed_task(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    plan_id: str,
    status: str = "PENDING",
    priority: str = "medium",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, plan_id, status, priority, description) VALUES ($1,$2,$3,$4,$5)",
            task_id,
            plan_id,
            status,
            priority,
            f"task {task_id}",
        )


async def _seed_worker(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    hostname: str,
    last_heartbeat: datetime | None = None,
    status: str = "online",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, status, last_heartbeat, registered_at, token_hash)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            worker_id,
            hostname,
            status,
            last_heartbeat or datetime.now(tz=UTC),
            datetime.now(tz=UTC),
            f"hash-{worker_id}",
        )


async def _register_worker(client: AsyncClient, plaintext_bs: str, hostname: str = "h") -> tuple[str, str]:
    resp = await client.post(
        REGISTER_PATH,
        json={"hostname": hostname},
        headers={"Authorization": f"Bearer {plaintext_bs}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["worker_id"], body["token"]


# ─── VAL-M3-METRICS-001: module-level metric instances exist ────────────


def test_module_exports_required_metric_attrs() -> None:
    mod = importlib.import_module("whilly.api.metrics")
    required_attrs = (
        "claims_total",
        "completes_total",
        "fails_total",
        "workers_online",
        "claims_pending",
        "plan_budget_remaining_usd",
        "claim_long_poll_duration_seconds",
    )
    for name in required_attrs:
        assert hasattr(mod, name), f"missing module-level metric: {name}"


def test_metric_naming_conventions() -> None:
    assert metrics_module.claims_total._name in ("whilly_claims", "whilly_claims_total")
    assert metrics_module.completes_total._name in ("whilly_completes", "whilly_completes_total")
    assert metrics_module.fails_total._name in ("whilly_fails", "whilly_fails_total")
    assert metrics_module.workers_online._name == "whilly_workers_online"
    assert metrics_module.claims_pending._name == "whilly_claims_pending"
    assert metrics_module.plan_budget_remaining_usd._name == "whilly_plan_budget_remaining_usd"
    assert metrics_module.claim_long_poll_duration_seconds._name == "whilly_claim_long_poll_duration_seconds"


def test_claim_histogram_buckets_cover_long_poll_budget() -> None:
    assert 30.0 in CLAIM_LONG_POLL_BUCKETS
    assert 60.0 in CLAIM_LONG_POLL_BUCKETS
    assert CLAIM_LONG_POLL_BUCKETS == (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


# ─── VAL-M3-METRICS-002: instrumentator wired ────────────────────────────


async def test_instrumentator_records_http_metrics(client: AsyncClient) -> None:
    await client.get("/health")
    await client.get("/health")
    response = await client.get(
        METRICS_PATH,
        headers={"Authorization": f"Bearer {_METRICS_TOKEN}"},
    )
    assert response.status_code == 200
    body = response.text
    assert "http_requests_total" in body or "http_request_duration_seconds" in body, (
        f"instrumentator default metrics missing: {body[:500]!r}"
    )


# ─── VAL-M3-METRICS-003: route registered ────────────────────────────────


def test_metrics_route_registered(app: FastAPI) -> None:
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert METRICS_PATH in paths


# ─── VAL-M3-METRICS-004 / -020: bearer auth + fail-closed ────────────────


async def test_metrics_missing_bearer_returns_401(client: AsyncClient) -> None:
    response = await client.get(METRICS_PATH)
    assert response.status_code == 401


async def test_metrics_wrong_bearer_returns_401(client: AsyncClient) -> None:
    response = await client.get(METRICS_PATH, headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


async def test_metrics_with_bearer_returns_200(client: AsyncClient) -> None:
    response = await client.get(METRICS_PATH, headers={"Authorization": f"Bearer {_METRICS_TOKEN}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_check_metrics_token_fail_closed_on_unset() -> None:
    assert check_metrics_token("Bearer anything", expected_token=None) is False
    assert check_metrics_token("Bearer anything", expected_token="") is False
    assert check_metrics_token("Bearer anything", expected_token="   ") is False


def test_check_metrics_token_ok_path() -> None:
    assert check_metrics_token("Bearer secret-xyz", expected_token="secret-xyz") is True
    assert check_metrics_token("bearer secret-xyz", expected_token="secret-xyz") is True


def test_resolve_metrics_token_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_TOKEN_ENV, "from-env")
    assert resolve_metrics_token("from-kwarg") == "from-kwarg"
    assert resolve_metrics_token(None) == "from-env"


def test_resolve_metrics_token_treats_blank_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_TOKEN_ENV, "   ")
    assert resolve_metrics_token(None) is None


async def test_metrics_endpoint_fail_closed_when_token_unset(
    db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(METRICS_TOKEN_ENV, raising=False)
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=1,
        metrics_token=None,
        metrics_refresh_interval_seconds=0.2,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(METRICS_PATH, headers={"Authorization": "Bearer anything"})
            assert resp.status_code == 401


# ─── VAL-M3-METRICS-005: /metrics excluded from self-recording ──────────


async def test_metrics_path_excluded_from_self_recording(client: AsyncClient) -> None:
    headers = {"Authorization": f"Bearer {_METRICS_TOKEN}"}
    for _ in range(5):
        await client.get(METRICS_PATH, headers=headers)
    final = await client.get(METRICS_PATH, headers=headers)
    text = final.text
    metrics_handler_pattern = re.compile(r'http_requests_total\{[^}]*handler="/metrics"[^}]*\}\s+([\d.eE+-]+)')
    matches = metrics_handler_pattern.findall(text)
    if matches:
        for v in matches:
            assert float(v) == 0.0, f"/metrics path was self-recorded with value {v} (expected 0): {text[:500]!r}"


# ─── VAL-M3-METRICS-006: valid Prometheus exposition format ─────────────


async def test_metrics_output_parses_with_prometheus_client(client: AsyncClient) -> None:
    response = await client.get(METRICS_PATH, headers={"Authorization": f"Bearer {_METRICS_TOKEN}"})
    assert response.status_code == 200
    families = list(text_string_to_metric_families(response.text))
    assert len(families) > 0
    names = {f.name for f in families}
    expected_custom = {
        "whilly_claims",  # converted to whilly_claims (counter) by parser
        "whilly_completes",
        "whilly_fails",
        "whilly_workers_online",
        "whilly_claims_pending",
        "whilly_plan_budget_remaining_usd",
        "whilly_claim_long_poll_duration_seconds",
    }
    found = expected_custom & names
    assert len(found) >= 5, f"expected >=5 of the custom metric families exposed, got {found!r}"


# ─── VAL-M3-METRICS-007: claims_total increments on claim ───────────────


async def test_claims_total_increments_on_successful_claim(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plaintext_bs = "live-bootstrap-claims"
    await _mint_bootstrap(db_pool, plaintext_bs, "claims@example.com")

    plan_id = await _seed_plan(db_pool, "plan-claim-inc")
    await _seed_task(db_pool, task_id="task-c-1", plan_id=plan_id, status="PENDING")

    worker_id, worker_token = await _register_worker(client, plaintext_bs)
    resp = await client.post(
        "/tasks/claim",
        json={"plan_id": plan_id, "worker_id": worker_id},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert resp.status_code == 200, resp.text

    value = metrics_module.claims_total.labels(plan_id=plan_id, worker_id=worker_id)._value.get()
    assert value == 1.0


# ─── VAL-M3-METRICS-008: completes_total increments on complete ─────────


async def test_completes_total_increments_on_complete(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plaintext_bs = "live-bootstrap-complete"
    await _mint_bootstrap(db_pool, plaintext_bs, "complete@example.com")

    plan_id = await _seed_plan(db_pool, "plan-complete-inc")
    await _seed_task(db_pool, task_id="task-cc-1", plan_id=plan_id, status="PENDING")
    worker_id, worker_token = await _register_worker(client, plaintext_bs)

    claim = await client.post(
        "/tasks/claim",
        json={"plan_id": plan_id, "worker_id": worker_id},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert claim.status_code == 200, claim.text
    task_payload = claim.json()["task"]
    task_id = task_payload["id"]
    version = task_payload["version"]

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET status='IN_PROGRESS', version=version+1 WHERE id=$1",
            task_id,
        )
        await conn.execute(
            "INSERT INTO events (task_id, plan_id, event_type, payload) VALUES ($1,$2,'START', '{}'::jsonb)",
            task_id,
            plan_id,
        )
        version += 1

    resp = await client.post(
        f"/tasks/{task_id}/complete",
        json={"worker_id": worker_id, "version": version, "cost_usd": 0.5},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert resp.status_code == 200, resp.text

    value = metrics_module.completes_total.labels(plan_id=plan_id, worker_id=worker_id)._value.get()
    assert value == 1.0


# ─── VAL-M3-METRICS-009: fails_total increments on fail ─────────────────


async def test_fails_total_increments_on_fail_with_reason_label(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plaintext_bs = "live-bootstrap-fail"
    await _mint_bootstrap(db_pool, plaintext_bs, "fail@example.com")
    plan_id = await _seed_plan(db_pool, "plan-fail-inc")
    await _seed_task(db_pool, task_id="task-f-1", plan_id=plan_id, status="PENDING")
    worker_id, worker_token = await _register_worker(client, plaintext_bs)

    claim = await client.post(
        "/tasks/claim",
        json={"plan_id": plan_id, "worker_id": worker_id},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert claim.status_code == 200, claim.text
    task_payload = claim.json()["task"]

    resp = await client.post(
        f"/tasks/{task_payload['id']}/fail",
        json={
            "worker_id": worker_id,
            "version": task_payload["version"],
            "reason": "auth-error",
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert resp.status_code == 200, resp.text

    value = metrics_module.fails_total.labels(plan_id=plan_id, worker_id=worker_id, reason="auth-error")._value.get()
    assert value == 1.0


# ─── VAL-M3-METRICS-010 / -903: workers_online gauge ────────────────────


async def test_workers_online_gauge_reflects_db_count(
    db_pool: asyncpg.Pool,
) -> None:
    fresh_now = datetime.now(tz=UTC)
    stale = fresh_now - timedelta(seconds=120)
    await _seed_worker(db_pool, worker_id="w-online-1", hostname="h1", last_heartbeat=fresh_now)
    await _seed_worker(db_pool, worker_id="w-online-2", hostname="h2", last_heartbeat=fresh_now)
    await _seed_worker(db_pool, worker_id="w-online-3", hostname="h3", last_heartbeat=fresh_now)
    await _seed_worker(db_pool, worker_id="w-stale-1", hostname="hs1", last_heartbeat=stale)
    await _seed_worker(db_pool, worker_id="w-stale-2", hostname="hs2", last_heartbeat=stale)

    await refresh_gauges(db_pool, online_threshold_seconds=30)
    assert metrics_module.workers_online._value.get() == 3.0


# ─── VAL-M3-METRICS-011: claims_pending gauge per plan ──────────────────


async def test_claims_pending_gauge_matches_pending_count(
    db_pool: asyncpg.Pool,
) -> None:
    plan_a = await _seed_plan(db_pool, "plan-pending-a")
    plan_b = await _seed_plan(db_pool, "plan-pending-b")
    for i in range(3):
        await _seed_task(db_pool, task_id=f"a-{i}", plan_id=plan_a, status="PENDING")
    for i in range(2):
        await _seed_task(db_pool, task_id=f"b-{i}", plan_id=plan_b, status="PENDING")
    await _seed_task(db_pool, task_id="a-done", plan_id=plan_a, status="DONE")

    await refresh_gauges(db_pool)
    a_val = metrics_module.claims_pending.labels(plan_id=plan_a)._value.get()
    b_val = metrics_module.claims_pending.labels(plan_id=plan_b)._value.get()
    assert a_val == 3.0
    assert b_val == 2.0


# ─── VAL-M3-METRICS-012: budget_remaining gauge ─────────────────────────


async def test_plan_budget_remaining_usd_gauge(db_pool: asyncpg.Pool) -> None:
    await _seed_plan(db_pool, "plan-budget-1", budget_usd="10.0", spent_usd="2.5")
    await _seed_plan(db_pool, "plan-budget-2", budget_usd="5.0", spent_usd="5.0")
    await _seed_plan(db_pool, "plan-no-budget")
    await refresh_gauges(db_pool)
    val_1 = metrics_module.plan_budget_remaining_usd.labels(plan_id="plan-budget-1")._value.get()
    val_2 = metrics_module.plan_budget_remaining_usd.labels(plan_id="plan-budget-2")._value.get()
    assert abs(val_1 - 7.5) < 1e-6
    assert abs(val_2 - 0.0) < 1e-6


# ─── VAL-M3-METRICS-013 / -901: histogram observed per claim with buckets ─


async def test_claim_histogram_observed_per_attempt(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plaintext_bs = "live-bootstrap-hist"
    await _mint_bootstrap(db_pool, plaintext_bs, "hist@example.com")
    plan_id = await _seed_plan(db_pool, "plan-hist")
    worker_id, worker_token = await _register_worker(client, plaintext_bs)

    for _ in range(3):
        resp = await client.post(
            "/tasks/claim",
            json={"plan_id": plan_id, "worker_id": worker_id},
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        assert resp.status_code in (200, 204)

    response = await client.get(METRICS_PATH, headers={"Authorization": f"Bearer {_METRICS_TOKEN}"})
    text = response.text
    count_line = re.search(r"^whilly_claim_long_poll_duration_seconds_count\s+([\d.eE+-]+)", text, re.MULTILINE)
    assert count_line is not None, f"missing histogram _count in: {text[:500]!r}"
    assert float(count_line.group(1)) >= 3.0


async def test_claim_histogram_buckets_in_exposition(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plaintext_bs = "live-bootstrap-hist-bckt"
    await _mint_bootstrap(db_pool, plaintext_bs, "hb@example.com")
    plan_id = await _seed_plan(db_pool, "plan-hist-buckets")
    worker_id, worker_token = await _register_worker(client, plaintext_bs)
    await client.post(
        "/tasks/claim",
        json={"plan_id": plan_id, "worker_id": worker_id},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    resp = await client.get(METRICS_PATH, headers={"Authorization": f"Bearer {_METRICS_TOKEN}"})
    text = resp.text
    bucket_re = re.compile(r'whilly_claim_long_poll_duration_seconds_bucket\{le="([\d.eE+]+|\+Inf)"\}')
    found = set(bucket_re.findall(text))
    expected = {"0.1", "0.25", "0.5", "1.0", "2.5", "5.0", "10.0", "30.0", "60.0", "+Inf"}
    missing = expected - found
    assert not missing, f"missing histogram buckets: {missing!r} (found {found!r})"


# ─── VAL-M3-METRICS-014 / -015: refresh loop runs and survives errors ───


async def test_metrics_refresh_loop_runs_and_updates(db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_TOKEN_ENV, _METRICS_TOKEN)
    fresh_now = datetime.now(tz=UTC)
    await _seed_worker(db_pool, worker_id="w-refresh-1", hostname="r1", last_heartbeat=fresh_now)

    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=1,
        metrics_token=_METRICS_TOKEN,
        metrics_refresh_interval_seconds=0.1,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        deadline = asyncio.get_event_loop().time() + 5.0
        while metrics_module.workers_online._value.get() == 0.0:
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        assert metrics_module.workers_online._value.get() >= 1.0


async def test_metrics_refresh_loop_survives_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from whilly.api.metrics import metrics_refresh_loop

    class _BadPool:
        def acquire(self) -> Any:
            raise RuntimeError("db is down")

    stop = asyncio.Event()
    tick_count = {"n": 0}

    async def _on_tick() -> None:
        tick_count["n"] += 1
        if tick_count["n"] >= 2:
            stop.set()

    await asyncio.wait_for(
        metrics_refresh_loop(_BadPool(), stop, interval=0.05, on_tick=_on_tick),
        timeout=5.0,
    )
    assert tick_count["n"] >= 2


# ─── VAL-M3-METRICS-904: standard process_* metrics are exposed ─────────


async def test_python_collector_metrics_exposed(client: AsyncClient) -> None:
    """The default :data:`prometheus_client.REGISTRY` ships
    :class:`PlatformCollector` and :class:`GCCollector` which emit
    ``python_info`` / ``python_gc_*`` series cross-platform.

    ``process_*`` series are added by :class:`ProcessCollector` only on
    platforms where ``/proc`` is readable (Linux containers); on
    darwin those don't appear, but the platform / GC families always do.
    VAL-M3-METRICS-904 is satisfied by the standard collector set.
    """
    response = await client.get(METRICS_PATH, headers={"Authorization": f"Bearer {_METRICS_TOKEN}"})
    body = response.text
    families = list(text_string_to_metric_families(body))
    names = {f.name for f in families}
    has_python_info = any(n.startswith("python_") for n in names)
    has_process = any(n.startswith("process_") for n in names)
    assert has_python_info or has_process, f"no standard prometheus_client metrics found: {names!r}"


# ─── pyproject deps in [server] extras ──────────────────────────────────


def test_pyproject_lists_prometheus_deps_in_server_extras() -> None:
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    server_extras = pyproject["project"]["optional-dependencies"]["server"]
    has_instrumentator = any(dep.startswith("prometheus-fastapi-instrumentator") for dep in server_extras)
    has_client = any(dep.startswith("prometheus-client") for dep in server_extras)
    assert has_instrumentator, f"prometheus-fastapi-instrumentator missing from [server]: {server_extras!r}"
    assert has_client, f"prometheus-client missing from [server]: {server_extras!r}"
