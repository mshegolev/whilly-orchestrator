"""ASGI contract tests for the admin-only swarm proposal sidecar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api import swarm_proposals
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware

SECRET = b"proposal-api-test-secret-long-enough-0000"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object):
        self.queries.append((query, args))
        return self.rows

    async def fetchrow(self, query: str, *args: object):
        self.queries.append((query, args))
        if "swarm_product_features" in query:
            return {"plan_revision": 7}
        return self.rows[0] if self.rows else None

    async def fetchval(self, query: str, *args: object):
        self.queries.append((query, args))
        return 11 if "swarm_product_features" in query else None


class FakeAcquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = FakeConn(rows)

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeStore:
    def __init__(self, _pool) -> None:
        self.row = {
            "id": "p1",
            "product_id": "default",
            "origin_feature_id": "f1",
            "origin_task_id": None,
            "target_project": "project-a",
            "target_module": "whilly/api.py",
            "evidence_refs": ("evidence:1",),
            "outcome": "make the API",
            "contract_impact": "none",
            "acceptance": ("tests pass",),
            "dependencies": (),
            "resource_class": "small",
            "fingerprint": "fp",
            "status": "proposed",
            "actor_id": "specialist",
            "actor_host": "host",
            "created_at": NOW,
            "updated_at": NOW,
        }

    async def get(self, product_id: str, proposal_id: str):
        return self.row if product_id == "default" and proposal_id == "p1" else None

    async def events(self, product_id: str, proposal_id: str):
        return () if product_id == "default" and proposal_id == "p1" else ()


class FakeService:
    def __init__(self) -> None:
        self.evaluated: list[tuple[object, str]] = []
        self.accepted: list[tuple[object, str, int, str]] = []
        self.rejected: list[tuple[object, str, str]] = []

    async def evaluate(self, principal, proposal_id):
        self.evaluated.append((principal, proposal_id))
        return SimpleNamespace(id=proposal_id, status="eligible", reason="ready")

    async def accept_for_planning(self, principal, proposal_id, *, expected_revision, reason):
        self.accepted.append((principal, proposal_id, expected_revision, reason))
        return SimpleNamespace(id=proposal_id, status="awaiting_approval", reason="accepted_for_planning")

    async def reject(self, principal, proposal_id, *, reason):
        self.rejected.append((principal, proposal_id, reason))
        return SimpleNamespace(id=proposal_id, status="rejected", reason="rejected")


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    project = SimpleNamespace(path=str(tmp_path), base_ref="main")
    registry = SimpleNamespace(projects={"project-a": project, "project-b": project}, raw={"name": "demo"})
    pool = FakePool([])
    store = FakeStore(pool)
    service = FakeService()
    loads: list[tuple[str, bool]] = []

    def load(path, *, check_git=True):
        loads.append((path, check_git))
        return registry

    monkeypatch.setattr(swarm_proposals, "load_registry", load)
    monkeypatch.setattr(swarm_proposals, "PostgresProposalStore", lambda pool, actor_host: store)
    monkeypatch.setattr(swarm_proposals, "build_proposal_service", lambda *args, **kwargs: service)
    monkeypatch.setattr(
        users_repo, "get_user_by_session_email", AsyncMock(return_value=SimpleNamespace(username="admin", role="admin"))
    )
    monkeypatch.setattr(
        sessions,
        "verify_session",
        AsyncMock(
            return_value=SimpleNamespace(
                email="admin@example.com", session_id="test", expires_at=NOW + timedelta(hours=1)
            )
        ),
    )
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware)
    app.include_router(swarm_proposals.build_proposal_router(pool, SECRET, str(tmp_path / "registry.json")))
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test", email="admin@example.com", ttl_seconds=3600
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
        cookies={COOKIE_NAME: cookie},
    ) as http:
        yield http, pool, service, loads


async def test_list_is_bounded_scoped_and_evaluated(harness):
    http, pool, service, loads = harness
    pool.conn.rows = [
        {
            "id": "p1",
            "product_id": "default",
            "target_project": "project-a",
            "status": "proposed",
            "created_at": NOW,
            "evidence_refs": '["evidence:1"]',
            "acceptance": '["tests pass"]',
            "dependencies": "[]",
            "feature_revision": 11,
        }
    ]
    response = await http.get("/api/v1/swarm/proposals")
    assert response.status_code == 200
    assert response.json()["proposals"][0]["feature_revision"] == 11
    assert response.json()["proposals"][0]["evidence_refs"] == ["evidence:1"]
    assert response.json()["proposals"][0]["acceptance"] == ["tests pass"]
    assert response.json()["proposals"][0]["dependencies"] == []
    assert response.json()["proposals"][0]["evaluation"] == {"status": "eligible", "reason": "ready"}
    query, args = pool.conn.queries[-1]
    assert "JOIN swarm_product_features" in query and "f.revision AS feature_revision" in query
    assert "plan_revision" not in query and "LIMIT 100" in query
    assert args[0] == "default"
    assert loads == [(str(loads[0][0]), False)]
    assert service.evaluated[-1][1] == "p1"


async def test_detail_returns_proposal_events_evaluation_and_revision(harness):
    http, pool, _service, _loads = harness
    response = await http.get("/api/v1/swarm/proposals/p1")
    assert response.status_code == 200
    assert set(response.json()) == {"proposal", "events", "evaluation", "feature_revision"}
    assert response.json()["feature_revision"] == 11
    assert "SELECT revision FROM swarm_product_features" in pool.conn.queries[-1][0]


async def test_accept_routes_only_bounded_authenticated_decision(harness):
    http, _pool, service, _loads = harness
    response = await http.post(
        "/api/v1/swarm/proposals/p1/accept", json={"expected_revision": 7, "reason": "ready to plan"}
    )
    assert response.status_code == 200
    assert response.json() == {"id": "p1", "status": "awaiting_approval", "reason": "accepted_for_planning"}
    assert service.accepted[0][2:] == (7, "ready to plan")


async def test_unknown_backend_error_code_is_not_echoed(harness):
    http, _pool, service, _loads = harness

    async def fail(*_args, **_kwargs):
        raise ValueError("credential-token-123")

    service.accept_for_planning = fail
    response = await http.post(
        "/api/v1/swarm/proposals/p1/accept", json={"expected_revision": 7, "reason": "ready to plan"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "proposal decision rejected"}


async def test_blocked_accept_is_conflict_with_safe_reason_and_blockers(harness):
    http, _pool, service, _loads = harness
    service.accept_for_planning = AsyncMock(
        return_value=SimpleNamespace(
            id="p1",
            status="blocked",
            reason="dependency_cycle",
            blockers=("dependency_cycle", "SELECT password FROM secrets"),
            feature_revision=11,
        )
    )
    response = await http.post(
        "/api/v1/swarm/proposals/p1/accept", json={"expected_revision": 7, "reason": "ready to plan"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "dependency_cycle", "blockers": ["dependency_cycle"]}


async def test_contract_proof_blocker_is_safe_for_ui(harness):
    http, _pool, service, _loads = harness
    service.accept_for_planning = AsyncMock(
        return_value=SimpleNamespace(
            id="p1",
            status="blocked",
            reason="contract_verification_proof_type_missing",
            blockers=("contract_verification_proof_type_missing",),
        )
    )
    response = await http.post(
        "/api/v1/swarm/proposals/p1/accept", json={"expected_revision": 7, "reason": "ready to plan"}
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "contract_verification_proof_type_missing",
        "blockers": ["contract_verification_proof_type_missing"],
    }


async def test_blocked_reject_uses_generic_reason_for_unknown_backend_text(harness):
    http, _pool, service, _loads = harness
    service.reject = AsyncMock(
        return_value=SimpleNamespace(
            id="p1",
            status="blocked",
            reason="private SQL text",
            blockers=("credential-token-123",),
            feature_revision=11,
        )
    )
    response = await http.post("/api/v1/swarm/proposals/p1/reject", json={"reason": "not now"})
    assert response.status_code == 409
    assert response.json() == {"detail": "proposal_blocked", "blockers": []}


async def test_reject_requires_nonblank_bounded_reason_and_forbids_extra(harness):
    http, _pool, service, _loads = harness
    for payload in ({"reason": " "}, {"reason": "x" * 257}, {"reason": "no", "actor_id": "forged"}):
        response = await http.post("/api/v1/swarm/proposals/p1/reject", json=payload)
        assert response.status_code == 422
    assert not service.rejected


async def test_decision_not_found_is_indistinguishable(harness, monkeypatch):
    http, _pool, _service, _loads = harness
    unknown = await http.post("/api/v1/swarm/proposals/unknown/reject", json={"reason": "no"})
    monkeypatch.setattr(
        swarm_proposals,
        "load_registry",
        lambda path, check_git=False: SimpleNamespace(
            projects={"project-b": SimpleNamespace(path="/tmp", base_ref="main")}
        ),
    )
    foreign = await http.post("/api/v1/swarm/proposals/p1/reject", json={"reason": "no"})
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "proposal not found"}
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "proposal not found"}


async def test_anonymous_and_non_csrf_mutation_are_rejected(harness, monkeypatch):
    http, _pool, _service, _loads = harness
    http.cookies.clear()
    assert (await http.get("/api/v1/swarm/proposals")).status_code == 401
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test", email="admin@example.com", ttl_seconds=3600
    )
    http.cookies.set(COOKIE_NAME, cookie)
    http.headers["Origin"] = "https://evil.example"
    assert (await http.post("/api/v1/swarm/proposals/p1/reject", json={"reason": "no"})).status_code == 403


async def test_get_does_not_call_decision_methods(harness):
    http, _pool, service, _loads = harness
    service.accept_for_planning = AsyncMock()
    service.reject = AsyncMock()
    assert (await http.get("/api/v1/swarm/proposals/p1")).status_code == 200
    service.accept_for_planning.assert_not_awaited()
    service.reject.assert_not_awaited()
