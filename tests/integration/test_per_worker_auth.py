"""Integration tests for per-worker bearer auth (TASK-101 / VAL-AUTH-010..052).

This module exercises the end-to-end auth surface introduced in
TASK-101: register a worker, authenticate every steady-state RPC
with the per-worker bearer, observe revocation via direct SQL, and
confirm the legacy ``WHILLY_WORKER_TOKEN`` fallback is gated by a
one-shot deprecation warning. All tests run against a real
testcontainers Postgres at ``head`` (migration 004 applied).

Coverage map vs. the validation contract
----------------------------------------
* VAL-AUTH-010: a freshly-registered worker's plaintext token claims
  a task (``POST /tasks/claim`` returns 200 + payload).
* VAL-AUTH-011: the dep maps the bearer through ``sha256`` to the
  registered ``workers.token_hash`` row — verified by direct SQL
  inspection of the round-trip.
* VAL-AUTH-012: all five steady-state RPCs (claim / complete / fail /
  release / heartbeat) accept the per-worker bearer (non-401).
* VAL-AUTH-020 / 021 / 022: unknown bearer / missing header / non-
  Bearer scheme all return 401 + ``WWW-Authenticate``.
* VAL-AUTH-023: ``UPDATE workers SET token_hash = NULL`` revokes
  the worker — the next RPC returns 401.
* VAL-AUTH-024: cross-worker bearer (worker A's token used on
  worker B's heartbeat path) returns a 4xx — body validation
  surfaces the mismatch.
* VAL-AUTH-030 / 031 / 032: the legacy ``WHILLY_WORKER_TOKEN``
  shared bearer is accepted with a one-shot deprecation log,
  suppressible via ``WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1``.
* VAL-AUTH-033: without ``WHILLY_WORKER_TOKEN`` set, a stale
  shared bearer is rejected (no deprecation log, just 401).
* VAL-AUTH-034: per-worker bearer takes precedence over the
  legacy fallback (no deprecation log when the per-worker hash
  matches).
* VAL-AUTH-052: two concurrent registers yield two distinct
  worker_ids and two distinct tokens (no race for token_hash
  collision).

Why a fresh ``create_app`` per fixture invocation?
    Each test wants to control whether ``WHILLY_WORKER_TOKEN`` /
    ``WHILLY_SUPPRESS_WORKER_TOKEN_WARNING`` are set, and the
    auth dep binds at app construction. The fixture rebuilds the
    app with the test's chosen token and resets the legacy-warning
    flag so the one-shot semantics start clean.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.auth import (
    SUPPRESS_WORKER_TOKEN_WARNING_ENV,
    hash_bearer_token,
    reset_legacy_warning_state,
)
from whilly.adapters.transport.server import CLAIM_PATH, REGISTER_PATH, create_app

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-tok-per-worker-auth"

# Aggressive long-poll knobs so timeout-driven tests finish in well
# under a second. Mirrors ``tests/integration/test_transport_tasks.py``.
_LONG_POLL_TIMEOUT = 0.3
_POLL_INTERVAL = 0.05


@pytest.fixture(autouse=True)
def _reset_legacy_warning() -> None:
    """Reset the one-shot legacy-bearer flag between tests.

    Without this, a previous test's ``WHILLY_WORKER_TOKEN`` match
    leaves the flag set and subsequent legacy-path tests would fail
    to observe the deprecation log.
    """
    reset_legacy_warning_state()


@pytest.fixture
async def app_no_legacy(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """App built without ``WHILLY_WORKER_TOKEN`` — purely DB-backed auth."""
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def http_client_no_legacy(
    app_no_legacy: FastAPI,
) -> AsyncIterator[AsyncClient]:
    """``httpx.AsyncClient`` against the no-legacy app."""
    transport = ASGITransport(app=app_no_legacy)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def app_with_legacy(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """App built WITH a legacy ``WHILLY_WORKER_TOKEN`` — fallback active."""
    app: FastAPI = create_app(
        db_pool,
        worker_token="legacy-shared-xyz",
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def http_client_with_legacy(
    app_with_legacy: FastAPI,
) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_with_legacy)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _register(client: AsyncClient, hostname: str) -> tuple[str, str]:
    """Register a worker and return ``(worker_id, plaintext_token)``."""
    response = await client.post(
        REGISTER_PATH,
        json={"hostname": hostname},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["worker_id"], body["token"]


async def _seed_task(
    pool: asyncpg.Pool,
    plan_id: str,
    task_id: str,
) -> None:
    """Insert one PENDING task in ``plan_id`` (creating the plan)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                plan_id,
                f"plan-{plan_id}",
            )
            await conn.execute(
                "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', 'medium')",
                task_id,
                plan_id,
            )


# ---------------------------------------------------------------------------
# VAL-AUTH-010 — registered worker claims a task
# ---------------------------------------------------------------------------


async def test_registered_worker_can_claim_task(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A worker's plaintext token authenticates POST /tasks/claim."""
    plan_id = "PLAN-PER-WORKER-1"
    task_id = "T-pwa-1"
    await _seed_task(db_pool, plan_id, task_id)
    worker_id, plaintext = await _register(http_client_no_legacy, "host-pwa-1")

    response = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "CLAIMED"
    assert body["task"]["version"] == 1


# ---------------------------------------------------------------------------
# VAL-AUTH-011 — token_hash round-trip via SHA-256
# ---------------------------------------------------------------------------


async def test_token_hash_is_sha256_of_plaintext(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """``workers.token_hash`` equals ``sha256(plaintext)`` in lower hex."""
    worker_id, plaintext = await _register(http_client_no_legacy, "host-hash")
    expected_hash = hash_bearer_token(plaintext)

    async with db_pool.acquire() as conn:
        stored_hash = await conn.fetchval(
            "SELECT token_hash FROM workers WHERE worker_id = $1",
            worker_id,
        )
    assert stored_hash == expected_hash


# ---------------------------------------------------------------------------
# VAL-AUTH-020 — unknown bearer returns 401
# ---------------------------------------------------------------------------


async def test_unknown_bearer_returns_401(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A 32-byte random string that no workers row knows → 401."""
    plan_id = "PLAN-UNKNOWN"
    await _seed_task(db_pool, plan_id, "T-unknown")
    response = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": "w-fake", "plan_id": plan_id},
        headers={"Authorization": "Bearer some-random-bearer-that-doesnt-match"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


# ---------------------------------------------------------------------------
# VAL-AUTH-021 — missing Authorization → 401
# ---------------------------------------------------------------------------


async def test_missing_authorization_header_returns_401(
    http_client_no_legacy: AsyncClient,
) -> None:
    """No header → 401 + WWW-Authenticate."""
    response = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": "w-x", "plan_id": "PLAN-X"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


# ---------------------------------------------------------------------------
# VAL-AUTH-022 — non-Bearer scheme → 401
# ---------------------------------------------------------------------------


async def test_basic_auth_scheme_returns_401(
    http_client_no_legacy: AsyncClient,
) -> None:
    """``Authorization: Basic ...`` → 401 invalid scheme."""
    response = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": "w-x", "plan_id": "PLAN-X"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid authorization scheme"


# ---------------------------------------------------------------------------
# VAL-AUTH-023 — UPDATE workers SET token_hash=NULL revokes
# ---------------------------------------------------------------------------


async def test_revocation_via_set_null_blocks_subsequent_rpc(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """Setting token_hash=NULL after a successful claim → next heartbeat 401."""
    worker_id, plaintext = await _register(http_client_no_legacy, "host-revoke")

    # Sanity: heartbeat works pre-revocation.
    pre = await http_client_no_legacy.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": worker_id},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert pre.status_code == 200

    # Revoke directly via SQL.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE workers SET token_hash = NULL WHERE worker_id = $1",
            worker_id,
        )

    # Same bearer is now refused.
    post = await http_client_no_legacy.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": worker_id},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert post.status_code == 401


# ---------------------------------------------------------------------------
# VAL-AUTH-024 — cross-worker bearer on every identity-bound RPC returns 403
# ---------------------------------------------------------------------------
#
# Each of the five steady-state RPCs is exercised independently (heartbeat,
# claim, complete, fail, release) — together with a sixth test that keeps
# the body↔path 400 branch covered. This is the TASK-101 scrutiny round-1
# fix: prior to the identity-binding change the only cross-worker test was
# heartbeat with body!=path (which surfaces as 400, NOT a real token-owner
# check). VAL-AUTH-024's evidence clause accepts ``(400, 401, 403)``;
# token-owner mismatch is 403 ("auth succeeded, you can't do this") so
# operator dashboards can split schema-validation 400s from authorisation
# 403s cleanly.


async def test_cross_worker_bearer_on_heartbeat_returns_403(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A's bearer on B's heartbeat (body=B, path=B) → 403; B's last_heartbeat unchanged."""
    worker_a, token_a = await _register(http_client_no_legacy, "host-cross-a")
    worker_b, _ = await _register(http_client_no_legacy, "host-cross-b")

    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_b,
        )

    response = await http_client_no_legacy.post(
        f"/workers/{worker_b}/heartbeat",
        json={"worker_id": worker_b},  # body and path both B; only bearer differs
        headers={"Authorization": f"Bearer {token_a}"},
    )

    assert response.status_code == 403, response.text
    assert worker_a in response.text or worker_b in response.text

    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_b,
        )
    assert after == before, "rejected heartbeat must not advance workers.last_heartbeat"


async def test_cross_worker_bearer_on_claim_returns_403(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A's bearer + body{worker_id:B, plan_id:...} → 403; task stays PENDING; no events."""
    plan_id = "PLAN-CROSS-CLAIM"
    task_id = "T-cross-claim-1"
    await _seed_task(db_pool, plan_id, task_id)
    _worker_a, token_a = await _register(http_client_no_legacy, "host-cross-claim-a")
    worker_b, _ = await _register(http_client_no_legacy, "host-cross-claim-b")

    response = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_b, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 403, response.text

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, claimed_by, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )
    assert row is not None
    assert row["status"] == "PENDING", "task must remain PENDING after rejected claim"
    assert row["claimed_by"] is None
    assert row["version"] == 0
    assert events_count == 0, "no event row may be written on a rejected claim"


async def test_cross_worker_bearer_on_complete_returns_403(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """B legitimately claims; A's bearer + body{worker_id:B} on /complete → 403; task unchanged."""
    plan_id = "PLAN-CROSS-COMPLETE"
    task_id = "T-cross-complete-1"
    await _seed_task(db_pool, plan_id, task_id)
    _worker_a, token_a = await _register(http_client_no_legacy, "host-cross-complete-a")
    worker_b, token_b = await _register(http_client_no_legacy, "host-cross-complete-b")

    # B claims legitimately.
    r_claim = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_b, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_claim.status_code == 200, r_claim.text
    claimed_version = r_claim.json()["task"]["version"]

    async with db_pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_before = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )

    response = await http_client_no_legacy.post(
        f"/tasks/{task_id}/complete",
        json={"worker_id": worker_b, "version": claimed_version, "cost_usd": "0.01"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 403, response.text

    async with db_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_after = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )
    assert after == before, "task row must not change on rejected complete"
    assert events_after == events_before, "no event row may be written on rejected complete"


async def test_cross_worker_bearer_on_fail_returns_403(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """B legitimately claims; A's bearer + body{worker_id:B} on /fail → 403; task unchanged."""
    plan_id = "PLAN-CROSS-FAIL"
    task_id = "T-cross-fail-1"
    await _seed_task(db_pool, plan_id, task_id)
    _worker_a, token_a = await _register(http_client_no_legacy, "host-cross-fail-a")
    worker_b, token_b = await _register(http_client_no_legacy, "host-cross-fail-b")

    r_claim = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_b, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_claim.status_code == 200, r_claim.text
    claimed_version = r_claim.json()["task"]["version"]

    async with db_pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_before = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )

    response = await http_client_no_legacy.post(
        f"/tasks/{task_id}/fail",
        json={
            "worker_id": worker_b,
            "version": claimed_version,
            "reason": "cross-worker-fail-test",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 403, response.text

    async with db_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_after = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )
    assert after == before, "task row must not change on rejected fail"
    assert events_after == events_before, "no event row may be written on rejected fail"


async def test_cross_worker_bearer_on_release_returns_403(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """B legitimately claims; A's bearer + body{worker_id:B} on /release → 403; task unchanged."""
    plan_id = "PLAN-CROSS-RELEASE"
    task_id = "T-cross-release-1"
    await _seed_task(db_pool, plan_id, task_id)
    _worker_a, token_a = await _register(http_client_no_legacy, "host-cross-release-a")
    worker_b, token_b = await _register(http_client_no_legacy, "host-cross-release-b")

    r_claim = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_b, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_claim.status_code == 200, r_claim.text
    claimed_version = r_claim.json()["task"]["version"]

    async with db_pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_before = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )

    response = await http_client_no_legacy.post(
        f"/tasks/{task_id}/release",
        json={
            "worker_id": worker_b,
            "version": claimed_version,
            "reason": "cross-worker-release-test",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 403, response.text

    async with db_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events_after = await conn.fetchval(
            "SELECT count(*) FROM events WHERE task_id = $1",
            task_id,
        )
    assert after == before, "task row must not change on rejected release"
    assert events_after == events_before, "no event row may be written on rejected release"


async def test_heartbeat_body_path_mismatch_returns_400(
    http_client_no_legacy: AsyncClient,
) -> None:
    """Body↔path mismatch is its own branch — independently exercised from the 403 token-owner check.

    Register A; POST /workers/{A}/heartbeat with body {worker_id: <other>}
    and bearer token_a. The body↔path 400 check fires AHEAD of the
    token-owner 403 check (server.py heartbeat handler), so this case
    must surface as 400 even though the bearer is valid for A.
    """
    worker_a, token_a = await _register(http_client_no_legacy, "host-bp-mismatch")

    response = await http_client_no_legacy.post(
        f"/workers/{worker_a}/heartbeat",
        json={"worker_id": "w-other"},  # body != path
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 400, response.text
    assert "does not match" in response.json()["detail"]


# ---------------------------------------------------------------------------
# VAL-AUTH-030 / 031 — legacy bearer accepted + one-shot warning
# ---------------------------------------------------------------------------


async def test_legacy_worker_token_accepted_with_warning(
    http_client_with_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Request with the legacy bearer succeeds AND logs a single WARNING."""
    plan_id = "PLAN-LEGACY-1"
    task_id = "T-legacy-1"
    # The legacy code path doesn't need a registered worker — but the
    # task FK on workers means a successful claim still requires a row.
    worker_id, _ = await _register(http_client_with_legacy, "host-legacy-1")
    await _seed_task(db_pool, plan_id, task_id)

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        # First successful legacy hit — warning fires.
        r1 = await http_client_with_legacy.post(
            CLAIM_PATH,
            json={"worker_id": worker_id, "plan_id": plan_id},
            headers={"Authorization": "Bearer legacy-shared-xyz"},
        )
        assert r1.status_code == 200, r1.text

        # Subsequent legacy hit on a fresh task — warning suppressed.
        await _seed_task(db_pool, plan_id, "T-legacy-2")
        r2 = await http_client_with_legacy.post(
            CLAIM_PATH,
            json={"worker_id": worker_id, "plan_id": plan_id},
            headers={"Authorization": "Bearer legacy-shared-xyz"},
        )
        assert r2.status_code == 200, r2.text

    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert len(deprecation_records) == 1, f"expected exactly one deprecation WARNING, got {len(deprecation_records)}"


# ---------------------------------------------------------------------------
# VAL-AUTH-032 — suppression env silences the warning
# ---------------------------------------------------------------------------


async def test_suppress_env_silences_deprecation_warning(
    db_pool: asyncpg.Pool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WHILLY_SUPPRESS_WORKER_TOKEN_WARNING=1`` silences the WARNING."""
    monkeypatch.setenv(SUPPRESS_WORKER_TOKEN_WARNING_ENV, "1")
    app: FastAPI = create_app(
        db_pool,
        worker_token="legacy-shared-xyz",
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            worker_id, _ = await _register(client, "host-suppress")
            plan_id = "PLAN-SUPPRESS"
            await _seed_task(db_pool, plan_id, "T-suppress-1")

            with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
                r = await client.post(
                    CLAIM_PATH,
                    json={"worker_id": worker_id, "plan_id": plan_id},
                    headers={"Authorization": "Bearer legacy-shared-xyz"},
                )
                assert r.status_code == 200

    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert deprecation_records == [], "deprecation WARNING must be suppressed"


# ---------------------------------------------------------------------------
# VAL-AUTH-033 — without WHILLY_WORKER_TOKEN, stale shared bearer is rejected
# ---------------------------------------------------------------------------


async def test_no_legacy_token_means_stale_shared_bearer_is_rejected(
    http_client_no_legacy: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale shared bearer + no env → 401, no deprecation log."""
    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        r = await http_client_no_legacy.post(
            CLAIM_PATH,
            json={"worker_id": "w-x", "plan_id": "PLAN-X"},
            headers={"Authorization": "Bearer stale-shared-xyz"},
        )
    assert r.status_code == 401
    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert deprecation_records == [], "no legacy token → no deprecation log on 401"


# ---------------------------------------------------------------------------
# VAL-AUTH-034 — per-worker bearer takes precedence; no warning emitted
# ---------------------------------------------------------------------------


async def test_per_worker_bearer_takes_precedence_over_legacy(
    http_client_with_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registered worker's bearer authenticates without firing the legacy log."""
    worker_id, plaintext = await _register(http_client_with_legacy, "host-precedence")
    plan_id = "PLAN-PREC"
    await _seed_task(db_pool, plan_id, "T-prec-1")

    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        r = await http_client_with_legacy.post(
            CLAIM_PATH,
            json={"worker_id": worker_id, "plan_id": plan_id},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert r.status_code == 200
    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert deprecation_records == [], "per-worker authentication path must NOT emit the legacy deprecation log"


# ---------------------------------------------------------------------------
# VAL-AUTH-052 — concurrent registers yield distinct workers + tokens
# ---------------------------------------------------------------------------


async def test_concurrent_registers_yield_distinct_tokens(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """Two concurrent registers → two worker_ids + two distinct token_hash rows."""

    async def _do_register(hostname: str) -> dict[str, Any]:
        response = await http_client_no_legacy.post(
            REGISTER_PATH,
            json={"hostname": hostname},
            headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
        )
        assert response.status_code == 201
        return response.json()

    results = await asyncio.gather(
        _do_register("host-conc-A"),
        _do_register("host-conc-B"),
    )
    body_a, body_b = results

    assert body_a["worker_id"] != body_b["worker_id"]
    assert body_a["token"] != body_b["token"]

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT worker_id, token_hash FROM workers WHERE worker_id IN ($1, $2)",
            body_a["worker_id"],
            body_b["worker_id"],
        )
    hashes = {row["token_hash"] for row in rows}
    assert len(hashes) == 2, "two registers must yield two distinct token_hash values"


# ---------------------------------------------------------------------------
# VAL-AUTH-012 — all five steady-state RPCs accept per-worker bearer
# ---------------------------------------------------------------------------


async def test_all_steady_state_rpcs_accept_per_worker_bearer(
    http_client_no_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """heartbeat / claim / complete / fail / release each return non-401 with a valid bearer."""
    worker_id, plaintext = await _register(http_client_no_legacy, "host-rpc-coverage")
    plan_id = "PLAN-RPC"

    # Seed two tasks: one for complete, one for fail. Use distinct
    # IDs so we can drive each terminal-state RPC in turn.
    await _seed_task(db_pool, plan_id, "T-complete-1")
    await _seed_task(db_pool, plan_id, "T-fail-1")
    await _seed_task(db_pool, plan_id, "T-release-1")

    headers = {"Authorization": f"Bearer {plaintext}"}

    # 1. Heartbeat.
    r_hb = await http_client_no_legacy.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": worker_id},
        headers=headers,
    )
    assert r_hb.status_code == 200, r_hb.text

    # 2. Claim → complete.
    r_claim = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers=headers,
    )
    assert r_claim.status_code == 200, r_claim.text
    claimed_complete = r_claim.json()["task"]
    r_complete = await http_client_no_legacy.post(
        f"/tasks/{claimed_complete['id']}/complete",
        json={"worker_id": worker_id, "version": claimed_complete["version"]},
        headers=headers,
    )
    assert r_complete.status_code == 200, r_complete.text

    # 3. Claim → fail.
    r_claim2 = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers=headers,
    )
    assert r_claim2.status_code == 200, r_claim2.text
    claimed_fail = r_claim2.json()["task"]
    r_fail = await http_client_no_legacy.post(
        f"/tasks/{claimed_fail['id']}/fail",
        json={
            "worker_id": worker_id,
            "version": claimed_fail["version"],
            "reason": "test-failure",
        },
        headers=headers,
    )
    assert r_fail.status_code == 200, r_fail.text

    # 4. Claim → release.
    r_claim3 = await http_client_no_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers=headers,
    )
    assert r_claim3.status_code == 200, r_claim3.text
    claimed_release = r_claim3.json()["task"]
    r_release = await http_client_no_legacy.post(
        f"/tasks/{claimed_release['id']}/release",
        json={
            "worker_id": worker_id,
            "version": claimed_release["version"],
            "reason": "test-release",
        },
        headers=headers,
    )
    assert r_release.status_code == 200, r_release.text
