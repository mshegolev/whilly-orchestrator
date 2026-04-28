"""Integration tests for the task-facing HTTP routes (TASK-021c1, PRD FR-1.1 / FR-1.3 / TC-6).

This module exercises ``POST /tasks/claim`` end-to-end against a real
Postgres (testcontainers). The unit tests in
:mod:`tests.unit.test_transport_server` already cover create_app's
construction-time validation; this suite is the load-bearing contract
that the long-poll loop really polls, the SQL really fires, and 204 is
the canonical "no work right now" outcome.

What's covered
--------------
* ``POST /tasks/claim`` returns 200 + the claimed :class:`TaskPayload`
  when a PENDING row exists. The repository state matches what the
  worker (TASK-022b1) will rely on: status flipped to ``CLAIMED`` in
  the database, ``version`` advanced by one, ``claimed_by`` set to
  the request's ``worker_id``.
* ``POST /tasks/claim`` returns 204 No Content when no PENDING rows
  exist for ``plan_id`` after the long-poll budget expires (the AC's
  load-bearing case for TASK-022b1's "204 → re-poll" branch).
* Long-polling really *polls* — when a task is seeded mid-poll, the
  same handler picks it up before the timeout fires.
* Bearer token is required (401 without; 401 with the bootstrap
  secret) — symmetric with the heartbeat tests' PRD FR-1.2 split.
* Empty / malformed body is rejected by pydantic (422).

Why integration, not unit
-------------------------
Same rationale as :mod:`tests.integration.test_transport_workers`: the
contract under test is "the handler claims a real row, advances its
version, and returns it" — mocking the repository would assert on
*method-call shape* instead of the actual DB transition, which is the
opposite of what these ACs care about.

Long-poll budget for tests
--------------------------
We pass ``claim_long_poll_timeout=0.3`` and
``claim_poll_interval=0.05`` to :func:`create_app` so the timeout
case lands in well under a second. The production defaults (30s /
1.5s) are exercised by the unit tests — making the integration suite
wait 30 seconds per timeout test would dominate runtime without
adding signal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import CLAIM_PATH, REGISTER_PATH, create_app

pytestmark = DOCKER_REQUIRED

_BOOTSTRAP_TOKEN = "bootstrap-tok-tasks"
_WORKER_TOKEN = "worker-tok-tasks"

# Aggressive timeouts — keep the suite fast while still exercising the
# poll loop's wall-clock semantics. ``_LONG_POLL_TIMEOUT`` is generous
# enough that the ``poll-then-find-task`` test has runway to seed a row
# mid-flight; ``_POLL_INTERVAL`` is small enough that the timeout test
# polls multiple times before bailing (so a regression that polls
# exactly once still surfaces as a wrong row count, not a wrong
# timeout).
_LONG_POLL_TIMEOUT = 0.3
_POLL_INTERVAL = 0.05


@pytest.fixture
async def http_client(db_pool: asyncpg.Pool) -> AsyncIterator[AsyncClient]:
    """Async HTTP client driving a fresh FastAPI app under its lifespan.

    Mirrors :mod:`tests.integration.test_transport_workers`'s fixture,
    but with shrunk long-poll knobs so the timeout case finishes in a
    few hundred milliseconds rather than 30 seconds. Per-test
    ``db_pool`` already truncates ``workers`` / ``tasks`` / ``plans``,
    so each test starts with a clean slate.
    """
    app: FastAPI = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _register(http_client: AsyncClient, hostname: str = "host-claim") -> tuple[str, str]:
    """Register a worker via the HTTP API and return ``(worker_id, plaintext_token)``.

    Routing claim tests through ``/workers/register`` (rather than
    seeding a ``workers`` row directly) means a regression in
    /workers/register surfaces here too, and the FK that
    ``tasks.claimed_by`` enforces against ``workers.worker_id`` is
    satisfied without test-only seed SQL.
    """
    response = await http_client.post(
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
    *,
    priority: str = "medium",
) -> None:
    """Insert one PENDING task row in ``plan_id``, creating the plan if needed.

    Single transaction so a half-seeded DB never leaks into a test if
    the seeding itself raises. Idempotent on the plan via ``ON CONFLICT
    DO NOTHING`` — multiple tests that share a plan_id would otherwise
    collide on the plan PK on the second seed.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                plan_id,
                f"plan-{plan_id}",
            )
            await conn.execute(
                "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', $3)",
                task_id,
                plan_id,
                priority,
            )


# ---------------------------------------------------------------------------
# Happy path — claim returns the task
# ---------------------------------------------------------------------------


async def test_claim_returns_pending_task(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A seeded PENDING row is returned by the very first claim attempt.

    Asserts the full client-visible contract:

    * 200 status code,
    * ``response.json()["task"]`` carries the seeded ``id`` and
      post-update ``status`` / ``version``,
    * the database row has flipped to ``CLAIMED`` and ``claimed_by``
      matches the registered worker.
    """
    plan_id = "PLAN-CLAIM-1"
    task_id = "T-claim-1"
    await _seed_task(db_pool, plan_id, task_id)
    worker_id, _ = await _register(http_client, "host-claim-1")

    response = await http_client.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"] is not None, "claim returned 200 but task is None"
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "CLAIMED"
    assert body["task"]["version"] == 1, "version should advance from 0 → 1 on first claim"
    # ``plan`` is intentionally None for TASK-021c1 (AC scope is "Task | 204").
    assert body.get("plan") is None

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, claimed_by, version FROM tasks WHERE id = $1",
            task_id,
        )
    assert row is not None
    assert row["status"] == "CLAIMED"
    assert row["claimed_by"] == worker_id
    assert row["version"] == 1


# ---------------------------------------------------------------------------
# 204 — long-poll timeout
# ---------------------------------------------------------------------------


async def test_claim_long_polling(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """No PENDING rows for the duration of the budget → 204 No Content.

    Pinned because TASK-022b1's worker loop branches on the 204
    status code: a regression here that returned 200 with a null body
    instead would crash the worker the first time the queue drained.

    Empty body is asserted explicitly — Starlette's 204 path can leak
    a Content-Length: 0 frame but no body bytes, and any drift here
    would surface as the worker reading malformed JSON on the empty
    case.
    """
    plan_id = "PLAN-CLAIM-EMPTY"
    # Seed the plan (without any tasks) so the FK on tasks.plan_id is
    # not the reason the claim returns empty — we want to assert on
    # the actual "no PENDING rows" path, not a "plan does not exist"
    # short-circuit.
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", plan_id, "empty plan")
    worker_id, _ = await _register(http_client, "host-claim-empty")

    response = await http_client.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )

    assert response.status_code == 204, response.text
    # 204 must not carry a body — the worker (TASK-022b1) decides on
    # the status code alone and any stray bytes here would either
    # fail strict JSON parsing or silently desync the contract.
    assert response.content == b""


async def test_claim_long_polling_picks_up_late_task(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A task seeded *during* the long-poll wait is returned before the timeout.

    This is what makes server-side long-polling worth the complexity:
    the worker doesn't have to back off and retry — the same in-flight
    request resolves the moment a row lands. We seed in a background
    task so the claim is already inside its poll loop when the row
    appears.
    """
    plan_id = "PLAN-CLAIM-LATE"
    task_id = "T-claim-late"
    # Seed only the plan up front; the task lands mid-poll.
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", plan_id, "late-arrival plan")
    worker_id, _ = await _register(http_client, "host-claim-late")

    async def seed_after_delay() -> None:
        # Sleep less than the long-poll timeout but more than the
        # poll interval, so the claim's first attempt definitely
        # finds nothing and at least one subsequent poll picks it up.
        await asyncio.sleep(_POLL_INTERVAL * 2)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', 'medium')",
                task_id,
                plan_id,
            )

    seeder = asyncio.create_task(seed_after_delay())
    try:
        response = await http_client.post(
            CLAIM_PATH,
            json={"worker_id": worker_id, "plan_id": plan_id},
            headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
        )
    finally:
        await seeder

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"] is not None
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "CLAIMED"


# ---------------------------------------------------------------------------
# Auth — bearer token is mandatory, bootstrap secret does not authenticate
# ---------------------------------------------------------------------------


async def test_claim_without_authorization_header_returns_401(
    http_client: AsyncClient,
) -> None:
    """No ``Authorization`` header → 401 + WWW-Authenticate (RFC 6750)."""
    response = await http_client.post(
        CLAIM_PATH,
        json={"worker_id": "w-x", "plan_id": "PLAN-Y"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


async def test_claim_with_bootstrap_token_returns_401(
    http_client: AsyncClient,
) -> None:
    """The bootstrap secret authenticates only ``/workers/register`` (PRD FR-1.2 split).

    Symmetric with
    :func:`tests.integration.test_transport_workers.test_heartbeat_with_bootstrap_token_returns_401`:
    the bootstrap and per-worker tokens must not cross-authenticate,
    or rotating one in isolation silently locks out only half of the
    cluster.
    """
    response = await http_client.post(
        CLAIM_PATH,
        json={"worker_id": "w-x", "plan_id": "PLAN-Y"},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Body validation — pydantic rejects malformed payloads
# ---------------------------------------------------------------------------


async def test_claim_validates_request_body(
    http_client: AsyncClient,
) -> None:
    """Empty ``worker_id`` / ``plan_id`` is rejected at the schema layer (422).

    The :class:`ClaimRequest` model declares both fields as
    ``NonEmptyShortStr`` — pydantic should reject an empty string
    before the handler runs, so the database is never touched on a
    malformed request.
    """
    response = await http_client.post(
        CLAIM_PATH,
        json={"worker_id": "", "plan_id": "PLAN-Y"},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 422
