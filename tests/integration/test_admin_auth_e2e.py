"""End-to-end coverage of the M2 admin / bootstrap auth surface.

Real testcontainers Postgres + ASGITransport ``httpx.AsyncClient``
exercises the assertions pinned by validation-contract.md
``VAL-M2-ADMIN-AUTH-*``. Companion to
``tests/unit/test_admin_auth.py`` which pins the closure contracts
narrowly with a fake repo.

Coverage map vs. the validation contract
----------------------------------------
* VAL-M2-ADMIN-AUTH-001 — register with valid per-user bootstrap
  token returns 201 and binds ``workers.owner_email`` to the mint
  owner (NOT to whatever was in the request body).
* VAL-M2-ADMIN-AUTH-002 — register with revoked bootstrap token
  → 401.
* VAL-M2-ADMIN-AUTH-003 — register with expired bootstrap token
  → 401.
* VAL-M2-ADMIN-AUTH-004 — malformed bearer → 401.
* VAL-M2-ADMIN-AUTH-005 — missing ``Authorization`` header → 401
  + RFC 6750 ``WWW-Authenticate`` envelope.
* VAL-M2-ADMIN-AUTH-006 — legacy ``WHILLY_WORKER_BOOTSTRAP_TOKEN``
  env-var still accepted.
* VAL-M2-ADMIN-AUTH-007 — legacy env path emits a single per-
  process deprecation WARNING.
* VAL-M2-ADMIN-AUTH-008 — admin route requires admin token; non-
  admin per-user token → 403.
* VAL-M2-ADMIN-AUTH-010 — admin route returns 401 on missing
  bearer (no DB lookup performed before the bearer parse passes).
* VAL-M2-ADMIN-AUTH-011 — ``workers.owner_email`` propagated into
  the CLAIM event's ``payload.owner_email`` JSONB.
* VAL-M2-ADMIN-AUTH-901 — admin token can both register workers
  AND access admin routes.
* VAL-M2-ADMIN-AUTH-902 — non-admin token can register workers
  but cannot access admin routes (200 + 403).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.transport.auth import reset_legacy_warning_state
from whilly.adapters.transport.server import CLAIM_PATH, REGISTER_PATH, create_app

pytestmark = DOCKER_REQUIRED


_LONG_POLL_TIMEOUT = 0.3
_POLL_INTERVAL = 0.05
_LEGACY_BOOTSTRAP = "legacy-shared-bootstrap-xyz"


@pytest.fixture(autouse=True)
def _reset_warning() -> None:
    """Reset the one-shot legacy-bootstrap warning between tests."""
    reset_legacy_warning_state()


@pytest.fixture
async def app_db_only(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """App built without any legacy bootstrap env — DB-backed only."""
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=None,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client_db_only(app_db_only: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_db_only)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def app_with_legacy(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """App built WITH a legacy bootstrap token (env-fallback path active)."""
    app: FastAPI = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_LEGACY_BOOTSTRAP,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client_with_legacy(app_with_legacy: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_with_legacy)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def repo(db_pool: asyncpg.Pool) -> TaskRepository:
    return TaskRepository(db_pool)


async def _seed_task(pool: asyncpg.Pool, plan_id: str, task_id: str) -> None:
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
# VAL-M2-ADMIN-AUTH-001 — register with per-user token binds owner_email
# ---------------------------------------------------------------------------


async def test_register_with_per_user_bootstrap_token_binds_owner_email(
    client_db_only: AsyncClient,
    db_pool: asyncpg.Pool,
    repo: TaskRepository,
) -> None:
    plaintext = "alice-bootstrap-001"
    await repo.mint_bootstrap_token(plaintext, owner_email="alice@example.com")

    response = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-alice"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    worker_id = body["worker_id"]
    assert body["token"]

    async with db_pool.acquire() as conn:
        owner_email = await conn.fetchval(
            "SELECT owner_email FROM workers WHERE worker_id = $1",
            worker_id,
        )
    assert owner_email == "alice@example.com"


async def test_register_with_per_user_token_overrides_body_owner_email(
    client_db_only: AsyncClient,
    db_pool: asyncpg.Pool,
    repo: TaskRepository,
) -> None:
    """Operator cannot spoof owner_email via the request body when the per-user
    bootstrap path resolved the operator's email at the auth layer."""
    plaintext = "alice-bootstrap-spoof-guard"
    await repo.mint_bootstrap_token(plaintext, owner_email="alice@example.com")

    response = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-alice", "owner_email": "mallory@example.com"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 201, response.text
    worker_id = response.json()["worker_id"]
    async with db_pool.acquire() as conn:
        owner_email = await conn.fetchval(
            "SELECT owner_email FROM workers WHERE worker_id = $1",
            worker_id,
        )
    assert owner_email == "alice@example.com"


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-002 — revoked bootstrap token → 401
# ---------------------------------------------------------------------------


async def test_register_with_revoked_bootstrap_token_returns_401(
    client_db_only: AsyncClient,
    repo: TaskRepository,
) -> None:
    plaintext = "bob-bootstrap-revoke"
    token_hash = await repo.mint_bootstrap_token(plaintext, owner_email="bob@example.com")
    await repo.revoke_bootstrap_token(token_hash)

    response = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-bob"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-003 — expired bootstrap token → 401
# ---------------------------------------------------------------------------


async def test_register_with_expired_bootstrap_token_returns_401(
    client_db_only: AsyncClient,
    repo: TaskRepository,
) -> None:
    plaintext = "carol-bootstrap-expired"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await repo.mint_bootstrap_token(plaintext, owner_email="carol@example.com", expires_at=past)

    response = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-carol"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-004 / 005 — malformed / missing bearer → 401
# ---------------------------------------------------------------------------


async def test_register_with_malformed_bearer_returns_401(
    client_db_only: AsyncClient,
) -> None:
    response = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-x"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


async def test_register_with_missing_authorization_header_returns_401(
    client_db_only: AsyncClient,
) -> None:
    response = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-x"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-006 / 007 — legacy env path
# ---------------------------------------------------------------------------


async def test_legacy_env_bootstrap_token_still_accepted(
    client_with_legacy: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    response = await client_with_legacy.post(
        REGISTER_PATH,
        json={"hostname": "host-legacy"},
        headers={"Authorization": f"Bearer {_LEGACY_BOOTSTRAP}"},
    )
    assert response.status_code == 201, response.text
    worker_id = response.json()["worker_id"]
    async with db_pool.acquire() as conn:
        owner_email = await conn.fetchval(
            "SELECT owner_email FROM workers WHERE worker_id = $1",
            worker_id,
        )
    # Legacy fallback path leaves owner_email NULL — the shared cluster
    # secret cannot identify a specific operator.
    assert owner_email is None


async def test_legacy_env_bootstrap_path_emits_single_deprecation_warning(
    client_with_legacy: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = {"Authorization": f"Bearer {_LEGACY_BOOTSTRAP}"}
    with caplog.at_level(logging.WARNING, logger="whilly.adapters.transport.auth"):
        for i in range(3):
            r = await client_with_legacy.post(
                REGISTER_PATH,
                json={"hostname": f"host-legacy-{i}"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
    deprecation_records = [
        rec for rec in caplog.records if rec.levelno == logging.WARNING and "deprecated" in rec.getMessage().lower()
    ]
    assert len(deprecation_records) == 1, (
        f"expected exactly one deprecation WARNING per process, got {len(deprecation_records)}"
    )


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-008 / 010 / 901 / 902 — admin route gating
# ---------------------------------------------------------------------------


async def test_admin_health_returns_401_without_bearer(
    client_db_only: AsyncClient,
) -> None:
    response = await client_db_only.get("/api/v1/admin/health")
    assert response.status_code == 401


async def test_admin_health_returns_403_for_non_admin_token(
    client_db_only: AsyncClient,
    repo: TaskRepository,
) -> None:
    plaintext = "alice-non-admin"
    await repo.mint_bootstrap_token(plaintext, owner_email="alice@example.com")

    response = await client_db_only.get(
        "/api/v1/admin/health",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 403, response.text


async def test_admin_health_returns_200_for_admin_token(
    client_db_only: AsyncClient,
    repo: TaskRepository,
) -> None:
    plaintext = "admin-token-001"
    await repo.mint_bootstrap_token(plaintext, owner_email="admin@example.com", is_admin=True)

    response = await client_db_only.get(
        "/api/v1/admin/health",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "owner": "admin@example.com"}


async def test_admin_token_can_both_register_and_access_admin_routes(
    client_db_only: AsyncClient,
    repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """VAL-M2-ADMIN-AUTH-901 — admin scope is a strict superset."""
    plaintext = "admin-superset"
    await repo.mint_bootstrap_token(plaintext, owner_email="admin@example.com", is_admin=True)
    headers = {"Authorization": f"Bearer {plaintext}"}

    r_register = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-admin"},
        headers=headers,
    )
    assert r_register.status_code == 201, r_register.text

    r_admin = await client_db_only.get("/api/v1/admin/health", headers=headers)
    assert r_admin.status_code == 200

    async with db_pool.acquire() as conn:
        owner_email = await conn.fetchval(
            "SELECT owner_email FROM workers WHERE worker_id = $1",
            r_register.json()["worker_id"],
        )
    assert owner_email == "admin@example.com"


async def test_non_admin_token_can_register_but_not_access_admin(
    client_db_only: AsyncClient,
    repo: TaskRepository,
) -> None:
    """VAL-M2-ADMIN-AUTH-902 — non-admin gets 201 + 403 matrix."""
    plaintext = "alice-non-admin-2"
    await repo.mint_bootstrap_token(plaintext, owner_email="alice2@example.com")
    headers = {"Authorization": f"Bearer {plaintext}"}

    r_register = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-alice2"},
        headers=headers,
    )
    assert r_register.status_code == 201

    r_admin = await client_db_only.get("/api/v1/admin/health", headers=headers)
    assert r_admin.status_code == 403


# ---------------------------------------------------------------------------
# VAL-M2-ADMIN-AUTH-011 — owner_email propagated into events.payload
# ---------------------------------------------------------------------------


async def test_owner_email_propagated_into_claim_event_payload(
    client_db_only: AsyncClient,
    repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    plaintext = "owner-event-propagation"
    await repo.mint_bootstrap_token(plaintext, owner_email="dora@example.com")

    r_register = await client_db_only.post(
        REGISTER_PATH,
        json={"hostname": "host-dora"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r_register.status_code == 201, r_register.text
    worker_id = r_register.json()["worker_id"]
    bearer = r_register.json()["token"]

    plan_id = "PLAN-OWNER-PROP"
    task_id = "T-owner-prop-1"
    await _seed_task(db_pool, plan_id, task_id)

    r_claim = await client_db_only.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert r_claim.status_code == 200, r_claim.text

    async with db_pool.acquire() as conn:
        payload_owner = await conn.fetchval(
            "SELECT payload->>'owner_email' FROM events "
            "WHERE task_id = $1 AND event_type = 'CLAIM' "
            "ORDER BY id DESC LIMIT 1",
            task_id,
        )
    assert payload_owner == "dora@example.com"


async def test_legacy_worker_path_emits_no_owner_email_in_payload(
    client_with_legacy: AsyncClient,
    repo: TaskRepository,
    db_pool: asyncpg.Pool,
) -> None:
    """A worker registered via the legacy env-bootstrap path has no
    ``owner_email`` and the CLAIM event payload omits the key (preserves
    the v4.4.0 baseline payload shape for legacy deployments)."""
    r_register = await client_with_legacy.post(
        REGISTER_PATH,
        json={"hostname": "host-legacy-events"},
        headers={"Authorization": f"Bearer {_LEGACY_BOOTSTRAP}"},
    )
    assert r_register.status_code == 201, r_register.text
    worker_id = r_register.json()["worker_id"]
    bearer = r_register.json()["token"]

    plan_id = "PLAN-LEGACY-EVENTS"
    task_id = "T-legacy-events-1"
    await _seed_task(db_pool, plan_id, task_id)

    r_claim = await client_with_legacy.post(
        CLAIM_PATH,
        json={"worker_id": worker_id, "plan_id": plan_id},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert r_claim.status_code == 200, r_claim.text

    async with db_pool.acquire() as conn:
        payload_owner = await conn.fetchval(
            "SELECT payload->>'owner_email' FROM events "
            "WHERE task_id = $1 AND event_type = 'CLAIM' "
            "ORDER BY id DESC LIMIT 1",
            task_id,
        )
    assert payload_owner is None
