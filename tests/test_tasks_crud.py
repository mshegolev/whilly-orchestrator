"""Tasks CRUD integration tests for PRD-wui-multi-plan v2 Block 9.

Covers SC-3.1 (weak-ETag ``W/"v<int>"`` concurrency on tasks), SC-3.2
(``task.edited`` / ``task.deleted`` event payload shape) and SC-3.3
(claimed-task safety + archived-plan 410) for the Block 8 surface:

* ``PATCH /api/v1/tasks/{task_id}?plan_id=X``
* ``DELETE /api/v1/tasks/{task_id}?plan_id=X``

The tasks surface uses a *weak* ETag whose value is the literal
``W/"v<int>"`` string, where ``<int>`` is the optimistic-locking
``tasks.version`` column. PRD §6.5 calls out the weak/strong split: plans
hash the projection (immutable rows are rare so the strong hash is cheap
to compute on every read); tasks expose version directly because the
worker already maintains it on every state transition.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.api import auth_tokens, sessions
from whilly.api.auth_routes import EVENT_LOG_PATH_ENV, build_auth_router
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.api.plans_api import build_plans_router
from whilly.api.tasks_api_crud import build_tasks_crud_router

pytestmark = DOCKER_REQUIRED

_TEST_SECRET: bytes = b"tasks-crud-test-secret-32-bytes!"
_GOOD_ORIGIN: str = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
async def _truncate_auth_tables(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Reset ``magic_links`` / ``sessions`` between tests (Phase 1 pattern)."""
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE magic_links, sessions")
    yield


@pytest.fixture
def isolated_event_log() -> Iterator[Path]:
    """Point ``WHILLY_EVENT_LOG_PATH`` at a per-test file in tempdir.

    Required for the ``task.edited`` / ``task.deleted`` event-payload
    assertions: the production default ``whilly_logs/whilly_events.jsonl``
    is a shared file across the whole repo, so sibling tests would
    contaminate the JSONL line counts.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="whilly-tasks-crud-"))
    log_path = tmpdir / "events.jsonl"
    prior = os.environ.get(EVENT_LOG_PATH_ENV)
    os.environ[EVENT_LOG_PATH_ENV] = str(log_path)
    try:
        yield log_path
    finally:
        if prior is None:
            os.environ.pop(EVENT_LOG_PATH_ENV, None)
        else:
            os.environ[EVENT_LOG_PATH_ENV] = prior
        try:
            if log_path.exists():
                log_path.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


@pytest.fixture
async def crud_app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """Minimal FastAPI app with auth + plans + tasks-CRUD routers."""
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware, allowlist=[_GOOD_ORIGIN])
    app.include_router(build_auth_router(pool=db_pool, secret=_TEST_SECRET))
    app.include_router(build_plans_router(pool=db_pool, secret=_TEST_SECRET))
    app.include_router(build_tasks_crud_router(pool=db_pool, secret=_TEST_SECRET))
    yield app


@pytest.fixture
async def crud_client(crud_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=crud_app)
    async with AsyncClient(transport=transport, base_url=_GOOD_ORIGIN) as client:
        yield client


@pytest.fixture
async def session_cookie(db_pool: asyncpg.Pool) -> str:
    """Mint a valid session cookie for a fresh operator principal."""
    session = await sessions.create_session(db_pool, email="tasks-crud@example.com")
    ttl = max(1, int(session.expires_at.timestamp() - time.time()))
    return auth_tokens.mint_session_cookie_value(
        _TEST_SECRET,
        session_id=session.session_id,
        email=session.email,
        ttl_seconds=ttl,
    )


@pytest.fixture
async def seeded_task(db_pool: asyncpg.Pool) -> dict[str, str]:
    """Insert a plan + a PENDING task directly in the DB.

    Yields a dict carrying ``plan_id`` and ``task_id`` so tests can
    interact with the seeded row by reference. We bypass the route
    layer here because POST /api/v1/tasks lives on the production
    ``create_app`` (covered by ``test_acceptance_demo.py``); this
    fixture serves the per-test units that exercise PATCH/DELETE.
    """
    plan_id = "tasks-crud-plan"
    task_id = "T-001"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            plan_id,
            "Tasks CRUD plan",
        )
        await conn.execute(
            """
            INSERT INTO tasks
                (id, plan_id, status, priority, description, version)
            VALUES ($1, $2, 'PENDING', 'medium', $3, 0)
            """,
            task_id,
            plan_id,
            "Seeded task",
        )
    return {"plan_id": plan_id, "task_id": task_id}


def _auth_headers(if_match: str | None = None) -> dict[str, str]:
    """Build the headers needed for CSRF + JSON + optional If-Match."""
    out = {"Origin": _GOOD_ORIGIN, "Content-Type": "application/json"}
    if if_match is not None:
        out["If-Match"] = if_match
    return out


def _read_events(log_path: Path) -> list[dict]:
    """Return parsed JSONL events from the test-isolated event log."""
    if not log_path.exists():
        return []
    out: list[dict] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


# ─── SC-3.1: PATCH happy path bumps version + returns new ETag ──────────────


@pytest.mark.asyncio
async def test_patch_task_with_valid_if_match_bumps_version_and_etag(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
) -> None:
    """PATCH with current weak ETag → 200 + version bumped + ETag returned."""
    response = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"description": "Edited description", "priority": "high"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["description"] == "Edited description"
    assert body["priority"] == "high"
    assert body["version"] == 1, body
    assert response.headers["etag"] == 'W/"v1"'


# ─── SC-3.1: PATCH stale If-Match returns 412 + current ETag ────────────────


@pytest.mark.asyncio
async def test_patch_task_with_stale_if_match_returns_412(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
) -> None:
    """Stale If-Match → 412 Precondition Failed carrying the current ETag."""
    # First PATCH succeeds, version moves 0 → 1.
    ok = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"description": "first edit"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert ok.status_code == 200, ok.text

    # Retry with the original stale ETag → 412 with current ETag echoed.
    stale = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"description": "second edit"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert stale.status_code == 412, stale.text
    body = stale.json()
    assert body.get("error") == "precondition_failed", body
    assert stale.headers["etag"] == 'W/"v1"'
    assert body.get("current_etag") == 'W/"v1"'


# ─── SC-3.3: claimed task returns 409 with worker_id ────────────────────────


@pytest.mark.asyncio
async def test_patch_claimed_task_returns_409_task_claimed(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    db_pool: asyncpg.Pool,
) -> None:
    """While a worker holds the row, PATCH returns 409 ``task_claimed``.

    We seed a workers row (claimed_by is a FK), then set
    ``claimed_by`` + ``claimed_at`` directly via SQL — the route layer
    must refuse to edit a row the worker still owns.
    """
    worker_id = "fake-worker-id"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, token_hash)
            VALUES ($1, $2, $3)
            """,
            worker_id,
            "fake-host.local",
            "deadbeef" * 8,
        )
        await conn.execute(
            """
            UPDATE tasks SET claimed_by = $1, claimed_at = NOW()
            WHERE id = $2 AND plan_id = $3
            """,
            worker_id,
            seeded_task["task_id"],
            seeded_task["plan_id"],
        )

    response = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"description": "should be rejected"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body.get("error") == "task_claimed", body
    assert body.get("worker_id") == worker_id, body


@pytest.mark.asyncio
async def test_patch_in_progress_task_returns_409_transient(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    db_pool: asyncpg.Pool,
) -> None:
    """Status IN_PROGRESS without claimed_by also returns 409 (transient gate).

    The route layer treats IN_PROGRESS as "worker mid-flight, hands off"
    even when ``claimed_by`` is briefly NULL between claim phases.
    Asserting this prevents an edit racing the worker's first SQL write.
    """
    worker_id = "in-progress-worker"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            "ip-host.local",
            "cafef00d" * 8,
        )
        # claimed_by/at must be set together per ck_tasks_claim_pair_consistent.
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'IN_PROGRESS', claimed_by = $1, claimed_at = NOW()
            WHERE id = $2 AND plan_id = $3
            """,
            worker_id,
            seeded_task["task_id"],
            seeded_task["plan_id"],
        )

    response = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"priority": "low"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body.get("error") == "task_claimed", body


# ─── SC-3.2: DELETE happy path + task.deleted event payload ─────────────────


@pytest.mark.asyncio
async def test_delete_task_with_valid_if_match_returns_204_and_row_disappears(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    db_pool: asyncpg.Pool,
) -> None:
    """DELETE with valid If-Match → 204 and the row is gone from tasks."""
    response = await crud_client.delete(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 204, response.text

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM tasks WHERE id = $1 AND plan_id = $2",
            seeded_task["task_id"],
            seeded_task["plan_id"],
        )
    assert row is None, "task row must be removed after DELETE"


@pytest.mark.asyncio
async def test_task_deleted_event_carries_full_pre_deletion_row(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    isolated_event_log: Path,
) -> None:
    """SC-3.2: ``task.deleted`` event payload contains the full pre-deletion row.

    The audit trail survives the hard-delete by writing the entire
    pre-deletion TaskPayload dict to the event log. We assert the key
    columns the operator needs to reconstruct the row are present.
    """
    response = await crud_client.delete(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 204, response.text

    events = _read_events(isolated_event_log)
    deleted_events = [e for e in events if e.get("event_type") == "task.deleted"]
    assert len(deleted_events) == 1, events
    event = deleted_events[0]
    assert event["task_id"] == seeded_task["task_id"]
    assert event["plan_id"] == seeded_task["plan_id"]
    deleted_row = event.get("deleted_row")
    assert isinstance(deleted_row, dict), event
    # Full pre-deletion row JSON: id, plan_id, status, version, description.
    assert deleted_row["id"] == seeded_task["task_id"]
    assert deleted_row["plan_id"] == seeded_task["plan_id"]
    assert deleted_row["status"] == "PENDING"
    assert deleted_row["version"] == 0
    assert deleted_row["description"] == "Seeded task"


# ─── SC-3.2: task.edited diff payload shape ─────────────────────────────────


@pytest.mark.asyncio
async def test_task_edited_event_diff_carries_from_to_per_field(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    isolated_event_log: Path,
) -> None:
    """Each changed field in ``task.edited.diff`` must be ``{"from": ..., "to": ...}``."""
    response = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"description": "new description", "priority": "high"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 200, response.text

    events = _read_events(isolated_event_log)
    edited = [e for e in events if e.get("event_type") == "task.edited"]
    assert len(edited) == 1, events
    diff = edited[0].get("diff") or {}
    assert "description" in diff
    assert diff["description"] == {"from": "Seeded task", "to": "new description"}
    assert "priority" in diff
    assert diff["priority"] == {"from": "medium", "to": "high"}


# ─── SC-3.3: archived plan → 410 Gone on PATCH/DELETE ───────────────────────


@pytest.mark.asyncio
async def test_patch_task_on_archived_plan_returns_410(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    db_pool: asyncpg.Pool,
) -> None:
    """PATCH on a task whose parent plan is archived → 410 Gone."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE plans SET archived_at = NOW() WHERE id = $1",
            seeded_task["plan_id"],
        )
    response = await crud_client.patch(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        json={"description": "edit on archived"},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 410, response.text
    assert response.json().get("error") == "plan_archived"


@pytest.mark.asyncio
async def test_delete_task_on_archived_plan_returns_410(
    crud_client: AsyncClient,
    session_cookie: str,
    seeded_task: dict[str, str],
    db_pool: asyncpg.Pool,
) -> None:
    """DELETE on a task whose parent plan is archived → 410 Gone."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE plans SET archived_at = NOW() WHERE id = $1",
            seeded_task["plan_id"],
        )
    response = await crud_client.delete(
        f"/api/v1/tasks/{seeded_task['task_id']}",
        params={"plan_id": seeded_task["plan_id"]},
        cookies={COOKIE_NAME: session_cookie},
        headers=_auth_headers(if_match='W/"v0"'),
    )
    assert response.status_code == 410, response.text
    assert response.json().get("error") == "plan_archived"
