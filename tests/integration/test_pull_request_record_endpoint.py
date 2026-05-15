"""Integration tests for ``POST /api/v1/plans/{plan_id}/pull_requests``.

Closes the symmetry gap between the local worker (which calls
:func:`whilly.sinks.post_complete_pr_hook.run_post_complete_pr_hook`
in-process after a task completes) and the remote worker (an HTTP
client with no direct DB access). The remote worker performs the
``git push`` + ``glab mr create`` / ``gh pr create`` locally and then
POSTs the resulting PR/MR triple to this endpoint, which performs the
DB insert + ``pr.opened`` event emission server-side so dashboards
that key off ``events.pr.opened`` behave identically regardless of
worker flavour.

What's covered
--------------
* 201 happy path: plan + DONE task → POST → row in ``pull_requests``
  + ``pr.opened`` event in ``events`` with the canonical payload shape.
* 404 plan not found.
* 404 task not found / task belongs to a different plan_id.
* 409 task exists but is not in DONE state.
* 200 idempotent re-POST against the same (plan_id, task_id) — returns
  existing row's data and emits no second event.
* Auth: missing bearer → 401. Cookie-only auth → 401 (worker-contract
  endpoint deliberately does not honour the dashboard session cookie).
* Body validation: missing ``task_id`` → 422. Negative ``pr_number``
  → 422.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import REGISTER_PATH, create_app

pytestmark = DOCKER_REQUIRED

_BOOTSTRAP_TOKEN = "bootstrap-tok-pr-record"
_WORKER_TOKEN = "worker-tok-pr-record"

_PLAN_ID = "PLAN-PR-REC-1"
_TASK_ID = "T-pr-rec-1"


@pytest.fixture
async def http_client(db_pool: asyncpg.Pool) -> AsyncIterator[AsyncClient]:
    """Async HTTP client driving a fresh FastAPI app under its lifespan."""

    app: FastAPI = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _register(http_client: AsyncClient, hostname: str = "host-pr-rec") -> tuple[str, str]:
    """Mint a per-worker bearer via ``/workers/register``."""

    response = await http_client.post(
        REGISTER_PATH,
        json={"hostname": hostname},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["worker_id"], body["token"]


async def _seed_plan_with_done_task(
    pool: asyncpg.Pool,
    plan_id: str = _PLAN_ID,
    task_id: str = _TASK_ID,
    *,
    status_value: str = "DONE",
) -> None:
    """Insert one plan + one task already in the target status."""

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                plan_id,
                f"plan-{plan_id}",
            )
            await conn.execute(
                "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, $3, 'medium')",
                task_id,
                plan_id,
                status_value,
            )


def _record_body(
    *,
    task_id: str = _TASK_ID,
    pr_url: str = "https://gitlab.example.com/qa-team/e2e/-/merge_requests/7",
    branch: str = "whilly/T-pr-rec-1",
    pr_number: int = 7,
    head_sha: str | None = "deadbeefcafe",
    repo_target_id: str | None = None,
    provider: str = "gitlab",
    worker_id: str | None = "w-pr-rec",
) -> dict[str, object]:
    body: dict[str, object] = {
        "task_id": task_id,
        "pr_url": pr_url,
        "branch": branch,
        "pr_number": pr_number,
        "provider": provider,
    }
    if head_sha is not None:
        body["head_sha"] = head_sha
    if repo_target_id is not None:
        body["repo_target_id"] = repo_target_id
    if worker_id is not None:
        body["worker_id"] = worker_id
    return body


# ---------------------------------------------------------------------------
# 201 happy path
# ---------------------------------------------------------------------------


async def test_record_pull_request_201_inserts_row_and_emits_event(
    http_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A POST for a DONE task records the PR row + ``pr.opened`` event."""

    await _seed_plan_with_done_task(db_pool)
    await _register(http_client)

    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=_record_body(),
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["plan_id"] == _PLAN_ID
    assert body["task_id"] == _TASK_ID
    assert body["pr_url"] == "https://gitlab.example.com/qa-team/e2e/-/merge_requests/7"
    assert body["pr_number"] == 7
    assert body["branch"] == "whilly/T-pr-rec-1"
    assert body["provider"] == "gitlab"
    assert body["recorded_at"], "recorded_at should be ISO8601 string"

    async with db_pool.acquire() as conn:
        pr_rows = await conn.fetch(
            "SELECT plan_id, task_id, pr_number, pr_url, branch, head_sha, state FROM pull_requests WHERE plan_id = $1",
            _PLAN_ID,
        )
        event_rows = await conn.fetch(
            "SELECT plan_id, task_id, event_type, payload FROM events WHERE plan_id = $1 AND event_type = 'pr.opened'",
            _PLAN_ID,
        )

    assert len(pr_rows) == 1
    pr = pr_rows[0]
    assert pr["task_id"] == _TASK_ID
    assert pr["pr_number"] == 7
    assert pr["pr_url"] == "https://gitlab.example.com/qa-team/e2e/-/merge_requests/7"
    assert pr["branch"] == "whilly/T-pr-rec-1"
    assert pr["head_sha"] == "deadbeefcafe"
    assert pr["state"] == "open"

    assert len(event_rows) == 1
    event = event_rows[0]
    assert event["task_id"] == _TASK_ID
    import json as _json

    raw_payload = event["payload"]
    payload = _json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    assert payload["pr_url"] == "https://gitlab.example.com/qa-team/e2e/-/merge_requests/7"
    assert payload["pr_number"] == 7
    assert payload["branch"] == "whilly/T-pr-rec-1"
    assert payload["head_sha"] == "deadbeefcafe"
    assert payload["task_id"] == _TASK_ID
    assert payload["provider"] == "gitlab"
    assert payload["worker_id"] == "w-pr-rec"


# ---------------------------------------------------------------------------
# 404 — plan / task not found
# ---------------------------------------------------------------------------


async def test_record_pull_request_404_plan_not_found(http_client: AsyncClient) -> None:
    """Unknown plan_id returns 404 with ``plan_not_found`` error code."""

    await _register(http_client)
    response = await http_client.post(
        "/api/v1/plans/PLAN-DOES-NOT-EXIST/pull_requests",
        json=_record_body(),
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "plan_not_found"


async def test_record_pull_request_404_task_not_found(http_client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    """Plan exists but task does not → 404 with ``task_not_found``."""

    await _seed_plan_with_done_task(db_pool, plan_id=_PLAN_ID, task_id=_TASK_ID)
    await _register(http_client)
    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=_record_body(task_id="T-never-existed"),
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "task_not_found"


async def test_record_pull_request_404_task_wrong_plan(http_client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    """Task exists but on a different plan_id → 404 ``task_not_found``."""

    await _seed_plan_with_done_task(db_pool, plan_id="PLAN-A", task_id="T-on-A")
    await _seed_plan_with_done_task(db_pool, plan_id="PLAN-B", task_id="T-on-B")
    await _register(http_client)

    # Address task T-on-B via plan PLAN-A — should 404.
    response = await http_client.post(
        "/api/v1/plans/PLAN-A/pull_requests",
        json=_record_body(task_id="T-on-B"),
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "task_not_found"


# ---------------------------------------------------------------------------
# 409 — task not DONE
# ---------------------------------------------------------------------------


async def test_record_pull_request_409_task_not_done(http_client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    """Task is PENDING / CLAIMED / IN_PROGRESS → 409 ``task_not_complete``."""

    await _seed_plan_with_done_task(db_pool, plan_id=_PLAN_ID, task_id=_TASK_ID, status_value="IN_PROGRESS")
    await _register(http_client)
    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=_record_body(),
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "task_not_complete"
    assert "DONE" in body["detail"]


# ---------------------------------------------------------------------------
# 200 — idempotent re-POST
# ---------------------------------------------------------------------------


async def test_record_pull_request_idempotent_second_post_returns_200(
    http_client: AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Re-POSTing the same (plan, task) returns 200 with the existing row.

    No second ``pull_requests`` row, no second ``pr.opened`` event.
    """

    await _seed_plan_with_done_task(db_pool)
    await _register(http_client)
    headers = {"Authorization": f"Bearer {_WORKER_TOKEN}"}
    body = _record_body()

    first = await http_client.post(f"/api/v1/plans/{_PLAN_ID}/pull_requests", json=body, headers=headers)
    assert first.status_code == 201, first.text

    # Second POST with a *different* pr_number to prove idempotency is
    # keyed on (plan_id, task_id) and the original row wins.
    second_body = dict(body)
    second_body["pr_number"] = 99
    second_body["pr_url"] = "https://gitlab.example.com/qa-team/e2e/-/merge_requests/99"
    second = await http_client.post(f"/api/v1/plans/{_PLAN_ID}/pull_requests", json=second_body, headers=headers)
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["pr_number"] == 7, "idempotent reply must echo the original row, not the new POST body"
    assert second_payload["pr_url"] == body["pr_url"]

    async with db_pool.acquire() as conn:
        pr_count = await conn.fetchval(
            "SELECT COUNT(*) FROM pull_requests WHERE plan_id = $1 AND task_id = $2",
            _PLAN_ID,
            _TASK_ID,
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE plan_id = $1 AND event_type = 'pr.opened'",
            _PLAN_ID,
        )
    assert pr_count == 1
    assert event_count == 1


# ---------------------------------------------------------------------------
# Auth — worker bearer is mandatory; cookie does not authenticate
# ---------------------------------------------------------------------------


async def test_record_pull_request_without_bearer_returns_401(http_client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    """No ``Authorization`` header → 401."""

    await _seed_plan_with_done_task(db_pool)
    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=_record_body(),
    )
    assert response.status_code == 401, response.text


async def test_record_pull_request_with_cookie_only_is_rejected(
    http_client: AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """A request carrying only the dashboard session cookie is rejected.

    The PR record endpoint is a worker-contract RPC — even if the
    operator's browser holds a valid ``whilly_session`` cookie, they
    should not be able to forge a PR record from a fetch in dev-tools.

    Two layers conspire to reject the request: the CSRF gate fires
    first on a cookie-bearing state-mutating verb with no allowlisted
    Origin (403), and if that gate were ever bypassed the worker
    bearer dep would still 401 the absent ``Authorization`` header.
    We assert the broader contract — rejection in the 4xx auth band
    with zero DB side effects — so the test stays green if the CSRF
    middleware is reordered later.
    """

    await _seed_plan_with_done_task(db_pool)
    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=_record_body(),
        cookies={"whilly_session": "forged-session-blob"},
    )
    assert response.status_code in {401, 403}, response.text

    async with db_pool.acquire() as conn:
        pr_count = await conn.fetchval(
            "SELECT COUNT(*) FROM pull_requests WHERE plan_id = $1",
            _PLAN_ID,
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE plan_id = $1 AND event_type = 'pr.opened'",
            _PLAN_ID,
        )
    assert pr_count == 0, "cookie-only request must not insert a pull_requests row"
    assert event_count == 0, "cookie-only request must not emit a pr.opened event"


# ---------------------------------------------------------------------------
# Body validation — pydantic 422
# ---------------------------------------------------------------------------


async def test_record_pull_request_missing_task_id_returns_422(http_client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    """Body without ``task_id`` is rejected by pydantic with 422."""

    await _seed_plan_with_done_task(db_pool)
    await _register(http_client)
    body = _record_body()
    body.pop("task_id")
    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=body,
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 422, response.text


async def test_record_pull_request_negative_pr_number_returns_422(
    http_client: AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """``pr_number < 0`` violates ``ge=0`` and is rejected at the wire."""

    await _seed_plan_with_done_task(db_pool)
    await _register(http_client)
    body = _record_body()
    body["pr_number"] = -1
    response = await http_client.post(
        f"/api/v1/plans/{_PLAN_ID}/pull_requests",
        json=body,
        headers={"Authorization": f"Bearer {_WORKER_TOKEN}"},
    )
    assert response.status_code == 422, response.text
