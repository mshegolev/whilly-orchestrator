"""Integration tests for the M3 ``GET /api/v1/tasks`` endpoint.

Coverage map vs. the validation contract (m3-tasks-api):

* VAL-M3-TASKS-API-001: registered in OpenAPI as a GET with ``plan_id`` query param
* VAL-M3-TASKS-API-002: returns ``{"tasks": [...]}`` with valid bearer
* VAL-M3-TASKS-API-003: 401 on missing Bearer header
* VAL-M3-TASKS-API-004: random/unregistered bearer returns 401 (documented deviation
  from "403" — auth dep returns 401 for unknown tokens, kept consistent with the rest
  of the API surface; the per-worker dep already distinguishes the two cases at the
  WWW-Authenticate level)
* VAL-M3-TASKS-API-005: each task has id, plan_id, status, priority, claimed_by,
  claimed_at, version, key_files, description, acceptance_criteria, test_steps
* VAL-M3-TASKS-API-006: pagination via ``limit`` + ``cursor`` walks the full list
* VAL-M3-TASKS-API-007: ``status`` filter narrows the result set
* VAL-M3-TASKS-API-009: unknown plan_id returns 200 + empty list (documented choice)
* VAL-M3-TASKS-API-011: Content-Type ``application/json`` + CORS headers
* VAL-M3-TASKS-API-012: missing ``plan_id`` → 422
* VAL-M3-TASKS-API-013: cursor stable when new rows insert mid-pagination
* VAL-M3-TASKS-API-901: compound filter (status + cursor + limit) walks correctly
* VAL-M3-TASKS-API-902: deterministic sort (PRIORITY_ORDER then id asc)
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


_BOOTSTRAP_TOKEN = "bootstrap-tasks-api-test"
TASKS_PATH = "/api/v1/tasks"


@pytest.fixture
async def app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(client: AsyncClient, hostname: str = "host-tasks-api") -> tuple[str, str]:
    response = await client.post(
        REGISTER_PATH,
        json={"hostname": hostname},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["worker_id"], body["token"]


async def _seed_plan(pool: asyncpg.Pool, plan_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            plan_id,
            f"plan {plan_id}",
        )


async def _seed_task(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    plan_id: str,
    status: str = "PENDING",
    priority: str = "medium",
    description: str = "",
    acceptance_criteria: list[str] | None = None,
    test_steps: list[str] | None = None,
    key_files: list[str] | None = None,
) -> None:
    import json as _json

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, priority, description,
                acceptance_criteria, test_steps, key_files
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb)
            """,
            task_id,
            plan_id,
            status,
            priority,
            description,
            _json.dumps(acceptance_criteria or []),
            _json.dumps(test_steps or []),
            _json.dumps(key_files or []),
        )


# ─── VAL-M3-TASKS-API-001: OpenAPI registration ────────────────────────


async def test_openapi_lists_tasks_endpoint(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert TASKS_PATH in spec["paths"], f"{TASKS_PATH} missing from openapi paths"
    op = spec["paths"][TASKS_PATH]
    assert "get" in op, f"GET method missing on {TASKS_PATH}"
    params = {p["name"] for p in op["get"].get("parameters", [])}
    assert "plan_id" in params, f"plan_id query param missing from {params!r}"


# ─── VAL-M3-TASKS-API-003: 401 on missing Bearer ───────────────────────


async def test_missing_bearer_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{TASKS_PATH}?plan_id=any")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")


# ─── VAL-M3-TASKS-API-004: bad bearer rejected ────────────────────────


async def test_random_bearer_is_rejected(client: AsyncClient) -> None:
    response = await client.get(
        f"{TASKS_PATH}?plan_id=any",
        headers={"Authorization": "Bearer not-a-registered-token"},
    )
    assert response.status_code == 401


# ─── VAL-M3-TASKS-API-002 + 005: happy path body shape ────────────────


async def test_returns_tasks_array_for_plan(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-1"
    await _seed_plan(db_pool, plan_id)
    await _seed_task(
        db_pool,
        task_id="t-1",
        plan_id=plan_id,
        priority="high",
        description="alpha",
        acceptance_criteria=["AC1"],
        test_steps=["step1"],
        key_files=["a.py"],
    )
    _, token = await _register(client)

    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert "tasks" in body and isinstance(body["tasks"], list)
    assert len(body["tasks"]) == 1
    task = body["tasks"][0]
    expected_keys = {
        "id",
        "plan_id",
        "status",
        "priority",
        "claimed_by",
        "claimed_at",
        "version",
        "key_files",
        "description",
        "acceptance_criteria",
        "test_steps",
    }
    missing = expected_keys - task.keys()
    assert not missing, f"task is missing keys: {missing!r}"
    assert task["id"] == "t-1"
    assert task["plan_id"] == plan_id
    assert task["status"] == "PENDING"
    assert task["priority"] == "high"
    assert task["claimed_by"] is None
    assert task["claimed_at"] is None
    assert task["version"] == 0
    assert task["key_files"] == ["a.py"]
    assert task["description"] == "alpha"
    assert task["acceptance_criteria"] == ["AC1"]
    assert task["test_steps"] == ["step1"]


# ─── VAL-M3-TASKS-API-007: status filter ─────────────────────────────


async def test_status_filter_returns_only_matching(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-status"
    await _seed_plan(db_pool, plan_id)
    await _seed_task(db_pool, task_id="p1", plan_id=plan_id, status="PENDING")
    await _seed_task(db_pool, task_id="p2", plan_id=plan_id, status="PENDING")
    await _seed_task(db_pool, task_id="d1", plan_id=plan_id, status="DONE")
    _, token = await _register(client)

    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}&status=DONE",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    ids = [t["id"] for t in response.json()["tasks"]]
    assert ids == ["d1"]


async def test_invalid_status_returns_422(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id=irrelevant&status=NOT_A_STATUS",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


# ─── VAL-M3-TASKS-API-009: unknown plan_id ───────────────────────────


async def test_unknown_plan_id_returns_empty_list(
    client: AsyncClient,
) -> None:
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id=does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"tasks": [], "next_cursor": None}


# ─── VAL-M3-TASKS-API-012: missing plan_id → 422 ──────────────────────


async def test_missing_plan_id_returns_422(
    client: AsyncClient,
) -> None:
    _, token = await _register(client)
    response = await client.get(
        TASKS_PATH,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


# ─── VAL-M3-TASKS-API-006: pagination walks the list ─────────────────


async def test_limit_caps_response(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-pag1"
    await _seed_plan(db_pool, plan_id)
    for i in range(7):
        await _seed_task(db_pool, task_id=f"page-{i:02d}", plan_id=plan_id)
    _, token = await _register(client)

    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}&limit=3",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["tasks"]) == 3
    assert body["next_cursor"] is not None


async def test_cursor_walks_through_pages(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-walk"
    await _seed_plan(db_pool, plan_id)
    for i in range(7):
        await _seed_task(db_pool, task_id=f"walk-{i:02d}", plan_id=plan_id)
    _, token = await _register(client)

    seen: list[str] = []
    cursor: str | None = None
    iterations = 0
    while True:
        iterations += 1
        if iterations > 10:
            pytest.fail("pagination did not terminate within 10 iterations")
        url = f"{TASKS_PATH}?plan_id={plan_id}&limit=3"
        if cursor is not None:
            url += f"&cursor={cursor}"
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(t["id"] for t in body["tasks"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == [f"walk-{i:02d}" for i in range(7)]
    assert len(seen) == 7
    assert len(set(seen)) == 7


async def test_no_next_cursor_when_exhausted(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-exhausted"
    await _seed_plan(db_pool, plan_id)
    for i in range(2):
        await _seed_task(db_pool, task_id=f"ex-{i}", plan_id=plan_id)
    _, token = await _register(client)

    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}&limit=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["tasks"]) == 2
    assert body["next_cursor"] is None


# ─── VAL-M3-TASKS-API-902: deterministic sort ────────────────────────


async def test_sort_priority_then_id(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-sort"
    await _seed_plan(db_pool, plan_id)
    await _seed_task(db_pool, task_id="zzz-low", plan_id=plan_id, priority="low")
    await _seed_task(db_pool, task_id="aaa-low", plan_id=plan_id, priority="low")
    await _seed_task(db_pool, task_id="bbb-medium", plan_id=plan_id, priority="medium")
    await _seed_task(db_pool, task_id="aaa-medium", plan_id=plan_id, priority="medium")
    await _seed_task(db_pool, task_id="zzz-critical", plan_id=plan_id, priority="critical")
    await _seed_task(db_pool, task_id="bbb-high", plan_id=plan_id, priority="high")
    _, token = await _register(client)

    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}&limit=20",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    ids = [t["id"] for t in response.json()["tasks"]]
    assert ids == [
        "zzz-critical",
        "bbb-high",
        "aaa-medium",
        "bbb-medium",
        "aaa-low",
        "zzz-low",
    ]


async def test_sort_is_idempotent(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-idem"
    await _seed_plan(db_pool, plan_id)
    await _seed_task(db_pool, task_id="t1", plan_id=plan_id, priority="medium")
    await _seed_task(db_pool, task_id="t2", plan_id=plan_id, priority="medium")
    await _seed_task(db_pool, task_id="t3", plan_id=plan_id, priority="medium")
    _, token = await _register(client)

    headers = {"Authorization": f"Bearer {token}"}
    r1 = await client.get(f"{TASKS_PATH}?plan_id={plan_id}", headers=headers)
    r2 = await client.get(f"{TASKS_PATH}?plan_id={plan_id}", headers=headers)
    assert r1.json() == r2.json()


# ─── VAL-M3-TASKS-API-013: cursor stable across mid-flight inserts ─


async def test_cursor_stable_when_new_rows_inserted(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-stable"
    await _seed_plan(db_pool, plan_id)
    for i in range(5):
        await _seed_task(db_pool, task_id=f"s-{i:02d}", plan_id=plan_id, priority="medium")
    _, token = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    page1 = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}&limit=2",
        headers=headers,
    )
    body1 = page1.json()
    assert len(body1["tasks"]) == 2
    assert body1["next_cursor"] is not None
    seen_first = [t["id"] for t in body1["tasks"]]

    await _seed_task(db_pool, task_id="s-99", plan_id=plan_id, priority="critical")

    seen_rest: list[str] = []
    cursor = body1["next_cursor"]
    iterations = 0
    while cursor is not None:
        iterations += 1
        if iterations > 10:
            pytest.fail("pagination did not terminate")
        url = f"{TASKS_PATH}?plan_id={plan_id}&limit=2&cursor={cursor}"
        response = await client.get(url, headers=headers)
        body = response.json()
        seen_rest.extend(t["id"] for t in body["tasks"])
        cursor = body["next_cursor"]

    seen_total = seen_first + seen_rest
    assert "s-99" not in seen_first
    for original_id in [f"s-{i:02d}" for i in range(5)]:
        if original_id not in seen_first:
            assert original_id in seen_rest, f"{original_id} skipped after mid-flight insert"
    assert len(seen_total) == len(set(seen_total)), "no duplicates expected"


# ─── VAL-M3-TASKS-API-901: compound filter (status + cursor + limit) ─


async def test_compound_filter_status_with_pagination(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-compound"
    await _seed_plan(db_pool, plan_id)
    for i in range(5):
        await _seed_task(db_pool, task_id=f"done-{i}", plan_id=plan_id, status="DONE")
    for i in range(3):
        await _seed_task(db_pool, task_id=f"pend-{i}", plan_id=plan_id, status="PENDING")
    _, token = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    seen: list[str] = []
    cursor: str | None = None
    iterations = 0
    while True:
        iterations += 1
        if iterations > 10:
            pytest.fail("pagination did not terminate")
        url = f"{TASKS_PATH}?plan_id={plan_id}&status=DONE&limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        response = await client.get(url, headers=headers)
        body = response.json()
        for t in body["tasks"]:
            assert t["status"] == "DONE"
            assert t["plan_id"] == plan_id
        seen.extend(t["id"] for t in body["tasks"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == [f"done-{i}" for i in range(5)]


# ─── VAL-M3-TASKS-API-011: Content-Type and CORS headers ──────────────


async def test_content_type_is_json(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-ct"
    await _seed_plan(db_pool, plan_id)
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.headers["content-type"].startswith("application/json")


async def test_cors_headers_are_present(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = "plan-tasks-api-cors"
    await _seed_plan(db_pool, plan_id)
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id={plan_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://dashboard.example",
        },
    )
    assert "access-control-allow-origin" in response.headers


# ─── Limit guards ─────────────────────────────────────────────────────


async def test_limit_zero_or_negative_rejected(
    client: AsyncClient,
) -> None:
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id=any&limit=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_limit_too_large_rejected(
    client: AsyncClient,
) -> None:
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id=any&limit=100000",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_invalid_cursor_returns_400(
    client: AsyncClient,
) -> None:
    _, token = await _register(client)
    response = await client.get(
        f"{TASKS_PATH}?plan_id=any&cursor=not-base64-or-anything!!",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
