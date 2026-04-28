"""Integration tests for the worker-side HTTP routes (TASK-021b, PRD FR-1.1 / FR-1.6 / TC-6).

This module exercises the two endpoints added in TASK-021b end-to-end
against a real Postgres (testcontainers) — the unit tests in
:mod:`tests.unit.test_transport_server` already cover the happy/error
paths against fakes; this suite is the load-bearing contract that the
SQL really fires and the rows really land.

What's covered
--------------
* ``POST /workers/register`` mints a fresh ``worker_id`` + plaintext
  token, inserts a ``workers`` row, and persists *only* the SHA-256
  hash of the token (PRD NFR-3 — plaintext is never on disk).
* ``POST /workers/register`` requires the bootstrap token. Wrong
  bootstrap, missing header, and a per-worker token in its place all
  return 401 — the per-worker token must not double as the cluster-
  join secret.
* ``POST /workers/{id}/heartbeat`` advances ``workers.last_heartbeat``
  for a registered worker.
* Heartbeat without a bearer / with the bootstrap secret returns 401:
  the per-worker bearer is the only thing that authenticates a steady-
  state RPC, even though the bootstrap secret was good enough to
  *register* the worker.
* Heartbeat for an unknown ``worker_id`` returns 200 with
  ``{"ok": false}`` — the recoverable state documented on
  :class:`whilly.adapters.transport.HeartbeatResponse`.
* Heartbeat with a body whose ``worker_id`` differs from the path
  returns 400 — defence-in-depth against mis-routed clients.

Why integration, not unit
-------------------------
Both endpoints touch ``workers`` rows. A unit test would either have to
mock the repository (which means the asserted contract is "the handler
called the method", which is what the AC explicitly is *not* about)
or stand up a fake pool that simulates row inserts (a 50-line
maintenance burden that drifts from production behaviour). The
testcontainers Postgres makes the test ~3× slower than a unit suite
but keeps the asserted contract identical to what runs in production.

Why ``httpx.AsyncClient`` + manual lifespan instead of ``TestClient``
---------------------------------------------------------------------
``fastapi.testclient.TestClient`` is sync (it spawns a portal thread
to drive the ASGI app), and the project's pytest fixtures
(``db_pool``, ``task_repo``) are async-native — mixing the two leads
to either deadlocks or fragile workarounds. Driving the app through
``httpx.AsyncClient(transport=ASGITransport(app=app))`` and explicitly
entering ``app.router.lifespan_context(app)`` keeps the whole test in
one event loop and makes the lifespan boundary explicit, which is
exactly what we want to assert against (the lifespan is what wires
``app.state.repo``).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import REGISTER_PATH, create_app

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-tok-integration"
_WORKER_TOKEN = "worker-tok-integration"


@pytest.fixture
async def http_client(db_pool: asyncpg.Pool) -> AsyncIterator[AsyncClient]:
    """Async HTTP client driving a fresh FastAPI app under its lifespan.

    Builds a fresh ``create_app`` per test (cheap — just wires deps),
    enters the lifespan so ``app.state.repo`` is populated, and yields
    an ``httpx.AsyncClient`` bound to the in-process ASGI transport.
    The ``base_url`` is a placeholder — ASGI transport never opens a
    socket — so ``http://test`` is the canonical convention.

    Per-test ``db_pool`` already truncates ``workers`` on setup, so
    each test starts with a clean slate.
    """
    app: FastAPI = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


# ---------------------------------------------------------------------------
# /workers/register
# ---------------------------------------------------------------------------


async def test_register_returns_worker_id_and_token_and_201(http_client: AsyncClient) -> None:
    """Happy path: 201 + worker_id + token in the body."""
    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": "host-alpha"},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "worker_id" in body and body["worker_id"].startswith("w-")
    assert "token" in body and len(body["token"]) > 0


async def test_register_persists_token_hash_not_plaintext(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """PRD NFR-3 (load-bearing): plaintext never reaches Postgres.

    Reads the ``workers`` row that the handler inserted and asserts:

    * ``token_hash`` matches ``sha256(plaintext)`` from the response,
    * ``token_hash`` does NOT equal the plaintext token,
    * ``hostname`` round-tripped intact.
    """
    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": "host-beta"},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 201
    body = response.json()
    worker_id = body["worker_id"]
    plaintext = body["token"]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT worker_id, hostname, token_hash FROM workers WHERE worker_id = $1",
            worker_id,
        )
    assert row is not None, "register did not insert a workers row"
    assert row["hostname"] == "host-beta"
    expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert row["token_hash"] == expected_hash, (
        f"token_hash mismatch: stored={row['token_hash']!r} expected={expected_hash!r}"
    )
    assert row["token_hash"] != plaintext, "plaintext token must NEVER be persisted (PRD NFR-3)"


async def test_register_without_authorization_header_returns_401(
    http_client: AsyncClient,
) -> None:
    """A worker with no credentials at all gets a 401 — RFC 6750 baseline."""
    response = await http_client.post(REGISTER_PATH, json={"hostname": "host-gamma"})
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


async def test_register_with_wrong_bootstrap_token_returns_401(
    http_client: AsyncClient,
) -> None:
    """A bad bootstrap secret never authenticates, no matter how close to right."""
    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": "host-delta"},
        headers={"Authorization": "Bearer not-the-bootstrap-token"},
    )
    assert response.status_code == 401


async def test_register_with_per_worker_token_instead_of_bootstrap_returns_401(
    http_client: AsyncClient,
) -> None:
    """The per-worker bearer must NOT double as the cluster-join secret.

    This is the load-bearing PRD FR-1.2 split: rotating the bootstrap
    token has to be a separate operation from rotating the per-worker
    bearer. If the per-worker token authenticated /workers/register
    too, the rotation contract would collapse.
    """
    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": "host-epsilon"},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 401


async def test_register_validates_request_body(http_client: AsyncClient) -> None:
    """An empty hostname is rejected by the pydantic schema (422)."""
    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": ""},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /workers/{worker_id}/heartbeat
# ---------------------------------------------------------------------------


async def _register(http_client: AsyncClient, hostname: str = "host-default") -> tuple[str, str]:
    """Register a worker via the HTTP API and return ``(worker_id, plaintext_token)``.

    Tests for /heartbeat go through /workers/register first rather than
    seeding the ``workers`` table directly — that way a regression in
    /workers/register surfaces here too instead of being papered over
    by hand-crafted seed SQL.
    """
    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": hostname},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["worker_id"], body["token"]


async def test_heartbeat_advances_last_heartbeat(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """The endpoint touches ``workers.last_heartbeat`` and returns ok=true.

    The default ``last_heartbeat = NOW()`` in the schema would race
    against the test's "after" timestamp on machines with low-
    resolution clocks (Postgres truncates to microseconds). Backdating
    by an hour after registration makes the inequality unambiguous —
    same trick used in :mod:`tests.integration.test_worker_heartbeat`.
    """
    worker_id, _ = await _register(http_client, "host-hb-1")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE workers SET last_heartbeat = NOW() - interval '1 hour' WHERE worker_id = $1",
            worker_id,
        )
        before = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )

    response = await http_client.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": worker_id},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}

    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT last_heartbeat FROM workers WHERE worker_id = $1",
            worker_id,
        )
    assert after > before, f"heartbeat did not advance last_heartbeat: before={before} after={after}"


async def test_heartbeat_without_bearer_returns_401(http_client: AsyncClient) -> None:
    """No ``Authorization`` header → 401 + WWW-Authenticate."""
    worker_id, _ = await _register(http_client, "host-hb-401")
    response = await http_client.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": worker_id},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


async def test_heartbeat_with_bootstrap_token_returns_401(http_client: AsyncClient) -> None:
    """Symmetric to /workers/register: the bootstrap secret does NOT double as a per-worker bearer.

    Together with :func:`test_register_with_per_worker_token_instead_of_bootstrap_returns_401`
    this pins the PRD FR-1.2 split — both directions have to fail.
    """
    worker_id, _ = await _register(http_client, "host-hb-bootstrap")
    response = await http_client.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": worker_id},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 401


async def test_heartbeat_for_unknown_worker_returns_ok_false(
    http_client: AsyncClient,
) -> None:
    """Recoverable state: unknown worker_id → 200 ``{"ok": false}``.

    Pinned because the worker's heartbeat loop reads this bool to
    decide whether to re-register and continue. A 4xx here would
    crash the loop instead of letting it self-heal — see
    :class:`whilly.adapters.transport.HeartbeatResponse`'s docstring.
    """
    response = await http_client.post(
        "/workers/w-does-not-exist/heartbeat",
        json={"worker_id": "w-does-not-exist"},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False}


async def test_heartbeat_rejects_path_body_worker_id_mismatch(
    http_client: AsyncClient,
) -> None:
    """Defence-in-depth: a misrouted client whose body disagrees with the URL gets 400."""
    worker_id, _ = await _register(http_client, "host-hb-mismatch")
    response = await http_client.post(
        f"/workers/{worker_id}/heartbeat",
        json={"worker_id": "w-different-from-path"},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 400
    body = response.json()
    # The error message should name both ids so the client can diagnose
    # the mismatch from the response body alone (no extra log spelunking).
    assert worker_id in body["detail"]
    assert "w-different-from-path" in body["detail"]
