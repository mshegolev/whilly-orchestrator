"""Integration tests for the task-facing HTTP routes (TASK-021c1 / TASK-021c2, PRD FR-1.1 / FR-1.3 / FR-2.4 / TC-6).

This module exercises ``POST /tasks/claim`` and the terminal-state
``POST /tasks/{task_id}/complete`` / ``POST /tasks/{task_id}/fail``
endpoints end-to-end against a real Postgres (testcontainers). The
unit tests in :mod:`tests.unit.test_transport_server` already cover
``create_app``'s construction-time validation; this suite is the
load-bearing contract that the long-poll loop really polls, the SQL
really fires, 204 is the canonical "no work right now" outcome,
terminal-state RPCs flip the row in the database, and the 409
``ErrorResponse`` envelope carries the full :class:`VersionConflictError`
payload the remote worker (TASK-022a3 / 022b1) branches on.

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
* Bearer token is required on every task RPC (401 without; 401 with
  the bootstrap secret) — symmetric with the heartbeat tests' PRD
  FR-1.2 split.
* ``POST /tasks/{task_id}/complete`` flips an IN_PROGRESS row to
  DONE, advances the version, and returns the post-update payload.
* ``POST /tasks/{task_id}/fail`` accepts a ``reason``, flips a
  CLAIMED *or* IN_PROGRESS row to FAILED, advances the version,
  appends the reason to ``events.payload``.
* :class:`whilly.adapters.db.VersionConflictError` maps to 409 with a
  fully-populated :class:`ErrorResponse` envelope on stale-version
  and wrong-status calls — the contract TASK-022a3 reads.
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


# ---------------------------------------------------------------------------
# Helpers — drive the full claim → start lattice through the repository
# ---------------------------------------------------------------------------


async def _claim_and_start(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
    *,
    plan_id: str,
    task_id: str,
    hostname: str,
) -> tuple[str, int]:
    """Seed → register → claim → start, returning ``(worker_id, version)``.

    The terminal-state tests need a row in ``IN_PROGRESS`` (for
    ``complete``) or ``CLAIMED`` (for ``fail``'s pre-START path).
    ``complete_task`` requires ``IN_PROGRESS`` per the SQL filter, so
    we drive the row all the way through the state machine via the
    existing endpoints + a single ``start_task`` repo call. Going
    through ``/tasks/claim`` (rather than seeding the row directly as
    CLAIMED) means a regression in the claim path surfaces here too.

    Returns the registered ``worker_id`` and the *post-claim* version
    so the caller can either use it directly (for the CLAIMED-state
    fail tests) or advance one more hop with
    :meth:`TaskRepository.start_task` (the complete tests do this
    explicitly so the test reads top-down).
    """
    await _seed_task(db_pool, plan_id, task_id)
    worker_id, _ = await _register(http_client, hostname)
    response = await http_client.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    version = int(response.json()["task"]["version"])
    return worker_id, version


# ---------------------------------------------------------------------------
# /tasks/{task_id}/complete — happy path
# ---------------------------------------------------------------------------


async def test_complete_transitions_in_progress_to_done(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A successful complete RPC flips IN_PROGRESS → DONE and bumps the version.

    Drives the full lattice (seed → register → claim → start → complete)
    so any regression in the upstream transitions surfaces in this test
    rather than as a confusing 409 on the complete itself. We assert
    the response body, the database row, and the audit row in a single
    test because they are three projections of the same transaction —
    splitting them would multiply the seeding cost without adding signal.
    """
    plan_id = "PLAN-COMPLETE-1"
    task_id = "T-complete-1"
    worker_id, claim_version = await _claim_and_start(
        http_client, db_pool, plan_id=plan_id, task_id=task_id, hostname="host-complete-1"
    )
    # ``start_task`` is the missing hop between CLAIMED and IN_PROGRESS;
    # we go through the repository directly because /tasks/start is not
    # an HTTP endpoint (TASK-019a's local worker calls the repo straight).
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(db_pool)
    started = await repo.start_task(task_id, claim_version)
    assert started.version == claim_version + 1

    response = await http_client.post(
        f"/tasks/{task_id}/complete",
        json={"worker_id": worker_id, "version": started.version},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "DONE"
    assert body["task"]["version"] == started.version + 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        events = await conn.fetch(
            "SELECT event_type FROM events WHERE task_id = $1 ORDER BY id",
            task_id,
        )
    assert row is not None
    assert row["status"] == "DONE"
    assert row["version"] == started.version + 1
    # Audit trail: CLAIM → START → COMPLETE in that order. We don't
    # assert the payloads here (other tests do) — the contract this
    # test pins is the *order* and the post-complete tail.
    event_types = [r["event_type"] for r in events]
    assert event_types[-1] == "COMPLETE", event_types
    assert "START" in event_types


# ---------------------------------------------------------------------------
# /tasks/{task_id}/complete — version conflict (409)
# ---------------------------------------------------------------------------


async def test_complete_with_stale_version_returns_409_with_envelope(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A stale ``version`` triggers a 409 with the full :class:`ErrorResponse` envelope.

    The lock-free contract is in :class:`VersionConflictError`'s
    docstring; this test pins the *wire* contract — the remote worker
    (TASK-022a3) maps the JSON body field-by-field, so any drift in
    the envelope shape surfaces here.
    """
    plan_id = "PLAN-COMPLETE-CONFLICT"
    task_id = "T-complete-conflict"
    worker_id, claim_version = await _claim_and_start(
        http_client, db_pool, plan_id=plan_id, task_id=task_id, hostname="host-complete-conflict"
    )
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(db_pool)
    started = await repo.start_task(task_id, claim_version)

    # Send the *pre-start* version: the row is now at
    # ``started.version`` but the worker thinks it's still at
    # ``claim_version``. This is the canonical "lost update" /
    # split-brain scenario the optimistic lock catches.
    stale_version = claim_version
    response = await http_client.post(
        f"/tasks/{task_id}/complete",
        json={"worker_id": worker_id, "version": stale_version},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "version_conflict"
    assert body["task_id"] == task_id
    assert body["expected_version"] == stale_version
    assert body["actual_version"] == started.version
    # Status is still IN_PROGRESS after the failed complete — the
    # transaction rolled back without writing anything else.
    assert body["actual_status"] == "IN_PROGRESS"
    # Database state is untouched by the conflict path.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
    assert row is not None
    assert row["status"] == "IN_PROGRESS"
    assert row["version"] == started.version


async def test_complete_on_already_done_task_returns_409(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """Completing a row that's already DONE returns 409 with ``actual_status='DONE'``.

    The idempotent-retry case: a worker that completed a task,
    crashed before its bookkeeping finished, and on restart re-issues
    the complete. The server detects this via the ``status`` filter
    rather than the ``version`` filter (versions match — status does
    not), so the 409 envelope reports
    ``actual_version == expected_version`` and a non-IN_PROGRESS
    ``actual_status``. TASK-022a3's client treats this as success.
    """
    plan_id = "PLAN-COMPLETE-IDEMP"
    task_id = "T-complete-idemp"
    worker_id, claim_version = await _claim_and_start(
        http_client, db_pool, plan_id=plan_id, task_id=task_id, hostname="host-complete-idemp"
    )
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(db_pool)
    started = await repo.start_task(task_id, claim_version)
    completed = await repo.complete_task(task_id, started.version)
    assert completed.status.value == "DONE"

    response = await http_client.post(
        f"/tasks/{task_id}/complete",
        json={"worker_id": worker_id, "version": completed.version},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "version_conflict"
    assert body["expected_version"] == completed.version
    assert body["actual_version"] == completed.version
    assert body["actual_status"] == "DONE"


# ---------------------------------------------------------------------------
# /tasks/{task_id}/complete — auth + body validation
# ---------------------------------------------------------------------------


async def test_complete_without_authorization_header_returns_401(
    http_client: AsyncClient,
) -> None:
    """No bearer header → 401 + WWW-Authenticate (RFC 6750)."""
    response = await http_client.post(
        "/tasks/T-x/complete",
        json={"worker_id": "w-x", "version": 1},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


async def test_complete_with_bootstrap_token_returns_401(
    http_client: AsyncClient,
) -> None:
    """The bootstrap secret does not authenticate the per-worker route (PRD FR-1.2)."""
    response = await http_client.post(
        "/tasks/T-x/complete",
        json={"worker_id": "w-x", "version": 1},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 401


async def test_complete_validates_request_body(
    http_client: AsyncClient,
) -> None:
    """Negative / missing version is rejected at the schema layer (422)."""
    response = await http_client.post(
        "/tasks/T-x/complete",
        json={"worker_id": "w-x", "version": -1},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /tasks/{task_id}/fail — happy path (CLAIMED → FAILED, no START hop needed)
# ---------------------------------------------------------------------------


async def test_fail_transitions_claimed_to_failed_with_reason(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A successful fail RPC flips a CLAIMED row directly to FAILED.

    Pinned because :meth:`TaskRepository.fail_task`'s SQL accepts both
    CLAIMED *and* IN_PROGRESS as source states — the worker may crash
    before ``start_task`` ever fires. This test exercises the
    pre-START fail path; ``test_fail_transitions_in_progress_to_failed``
    below covers the post-START one. The reason is asserted on the
    audit row because that's what the dashboard (TASK-027) reads.
    """
    plan_id = "PLAN-FAIL-CLAIMED"
    task_id = "T-fail-claimed"
    worker_id, claim_version = await _claim_and_start(
        http_client, db_pool, plan_id=plan_id, task_id=task_id, hostname="host-fail-claimed"
    )

    response = await http_client.post(
        f"/tasks/{task_id}/fail",
        json={
            "worker_id": worker_id,
            "version": claim_version,
            "reason": "agent crashed before start",
        },
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "FAILED"
    assert body["task"]["version"] == claim_version + 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        fail_event = await conn.fetchrow(
            "SELECT event_type, payload FROM events "
            "WHERE task_id = $1 AND event_type = 'FAIL' "
            "ORDER BY id DESC LIMIT 1",
            task_id,
        )
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["version"] == claim_version + 1
    assert fail_event is not None
    # ``payload`` is JSON text on the wire; round-trip it for assertion
    # rather than coupling the test to asyncpg's codec config.
    import json as _json

    payload = _json.loads(fail_event["payload"]) if isinstance(fail_event["payload"], str) else fail_event["payload"]
    assert payload["reason"] == "agent crashed before start"
    assert payload["version"] == claim_version + 1


async def test_fail_transitions_in_progress_to_failed(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """The post-START fail path: IN_PROGRESS → FAILED with the reason persisted.

    Symmetric with the CLAIMED case above — exercises the second
    source state ``fail_task``'s SQL accepts. The two tests together
    pin the full ``status IN ('CLAIMED','IN_PROGRESS')`` filter
    contract.
    """
    plan_id = "PLAN-FAIL-IP"
    task_id = "T-fail-ip"
    worker_id, claim_version = await _claim_and_start(
        http_client, db_pool, plan_id=plan_id, task_id=task_id, hostname="host-fail-ip"
    )
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(db_pool)
    started = await repo.start_task(task_id, claim_version)

    response = await http_client.post(
        f"/tasks/{task_id}/fail",
        json={
            "worker_id": worker_id,
            "version": started.version,
            "reason": "exit_code=1; tests failed",
        },
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["status"] == "FAILED"
    assert body["task"]["version"] == started.version + 1


# ---------------------------------------------------------------------------
# /tasks/{task_id}/fail — version conflict (409)
# ---------------------------------------------------------------------------


async def test_fail_with_stale_version_returns_409_with_envelope(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A stale ``version`` on /fail produces the same 409 envelope as /complete.

    Mirrors :func:`test_complete_with_stale_version_returns_409_with_envelope`
    — the wire contract for the conflict envelope is shared between
    both terminal-state RPCs (centralised in :func:`_conflict_response`),
    so any drift on one route would break the other and TASK-022a3's
    client mapper would have to special-case which.
    """
    plan_id = "PLAN-FAIL-CONFLICT"
    task_id = "T-fail-conflict"
    worker_id, claim_version = await _claim_and_start(
        http_client, db_pool, plan_id=plan_id, task_id=task_id, hostname="host-fail-conflict"
    )
    from whilly.adapters.db import TaskRepository

    repo = TaskRepository(db_pool)
    started = await repo.start_task(task_id, claim_version)
    # Stale: send the pre-start version after the row has advanced.
    stale_version = claim_version

    response = await http_client.post(
        f"/tasks/{task_id}/fail",
        json={
            "worker_id": worker_id,
            "version": stale_version,
            "reason": "should-not-be-persisted",
        },
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "version_conflict"
    assert body["task_id"] == task_id
    assert body["expected_version"] == stale_version
    assert body["actual_version"] == started.version
    assert body["actual_status"] == "IN_PROGRESS"
    # The reason from the failed call must NOT have leaked into the
    # audit log — a 409 should leave the database completely
    # untouched (the repo wraps the UPDATE + INSERT INTO events in a
    # single transaction that rolls back on the version-conflict
    # branch).
    async with db_pool.acquire() as conn:
        leaked = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE task_id = $1 AND payload::text LIKE '%should-not-be-persisted%'",
            task_id,
        )
    assert leaked == 0


# ---------------------------------------------------------------------------
# /tasks/{task_id}/fail — auth + body validation
# ---------------------------------------------------------------------------


async def test_fail_without_authorization_header_returns_401(
    http_client: AsyncClient,
) -> None:
    """No bearer header → 401 (symmetric with /complete)."""
    response = await http_client.post(
        "/tasks/T-x/fail",
        json={"worker_id": "w-x", "version": 1, "reason": "boom"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


async def test_fail_with_bootstrap_token_returns_401(
    http_client: AsyncClient,
) -> None:
    """Bootstrap secret does not authenticate /fail (PRD FR-1.2 split)."""
    response = await http_client.post(
        "/tasks/T-x/fail",
        json={"worker_id": "w-x", "version": 1, "reason": "boom"},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 401


async def test_fail_rejects_empty_reason(
    http_client: AsyncClient,
) -> None:
    """An empty ``reason`` is rejected by the :class:`FailRequest` schema (422).

    The audit row's whole point is the human-readable cause — a blank
    reason would defeat the dashboard / post-mortem queries
    (TASK-027). Pinning the rejection at the schema layer means the
    database is never touched on a malformed fail.
    """
    response = await http_client.post(
        "/tasks/T-x/fail",
        json={"worker_id": "w-x", "version": 1, "reason": ""},
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 422
