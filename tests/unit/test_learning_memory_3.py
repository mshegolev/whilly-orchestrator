"""ASGI boundary tests for the L1.3 learning-memory API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api import swarm_memory
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.swarm.learning.domain import ContextPackage, KnowledgeRevision

SECRET = b"memory-test-secret-long-enough-0000"


class FakeStore:
    def __init__(self) -> None:
        self.appended: list[KnowledgeRevision] = []
        self.error: Exception | None = None

    async def append(self, revision: KnowledgeRevision) -> KnowledgeRevision:
        if self.error is not None:
            raise self.error
        self.appended.append(revision)
        return revision

    async def visible(self, principal, product_id, project_ids):
        return []

    async def redact(self, principal, revision_id):
        return False


class FakeProductStore:
    async def ensure_product(self, name):
        return {"id": "default", "name": name}


class FakeProducts:
    def __init__(self, pool, registry_path):
        self.store = FakeProductStore()


class _FakeMemoryService:
    def __init__(self, store):
        self.store = store

    async def context(self, principal, product_id, project_ids, query, *, max_chars):
        self.store.query = query
        return ContextPackage((), (), (), ())

    def status(self):
        return SimpleNamespace(
            to_dict=lambda: {"backend": "l1", "ready": True, "last_elapsed_ms": 0, "error_code": None}
        )

    async def redact(self, principal, revision_id):
        return SimpleNamespace(redacted=True, purge_pending=True)


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    project = SimpleNamespace(path=str(tmp_path), base_ref="main")
    registry = SimpleNamespace(projects={"project-a": project}, raw={"name": "demo"})
    store = FakeStore()
    monkeypatch.setattr(swarm_memory, "load_registry", lambda path, check_git=False: registry)
    monkeypatch.setattr(swarm_memory, "PostgresMemoryStore", lambda pool: store)
    monkeypatch.setattr(swarm_memory, "build_memory_coordinator", lambda pool, registry: _FakeMemoryService(store))
    monkeypatch.setattr(swarm_memory, "ProductService", FakeProducts)
    monkeypatch.setattr(swarm_memory, "GitSourceVerifier", lambda projects: object(), raising=False)

    principal = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=principal))
    monkeypatch.setattr(
        sessions,
        "verify_session",
        AsyncMock(
            return_value=SimpleNamespace(
                email="admin@example.com", session_id="test", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
            )
        ),
    )
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware)
    app.include_router(swarm_memory.build_memory_router(None, SECRET, str(tmp_path / "registry.json")))
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test", email="admin@example.com", ttl_seconds=3600
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
        cookies={COOKIE_NAME: cookie},
    ) as http:
        yield http, principal, store


def body(**overrides):
    value = {
        "project_id": "project-a",
        "kind": "fact",
        "body": "candidate body",
        "source_uri": "git:docs/fact.md",
        "source_sha": "a" * 40,
        "evidence_hash": "evidence-hash",
    }
    value.update(overrides)
    return value


async def test_worker_bearer_is_401(harness):
    http, _, _ = harness
    http.cookies.clear()
    http.headers["Authorization"] = "Bearer worker-token"
    assert (await http.get("/api/v1/swarm/knowledge?project_id=project-a")).status_code == 401


async def test_operator_is_403(harness):
    http, principal, _ = harness
    principal.role = "operator"
    assert (await http.get("/api/v1/swarm/knowledge?project_id=project-a")).status_code == 403


async def test_write_requires_csrf(harness):
    http, _, _ = harness
    http.headers["Origin"] = "https://untrusted.example"
    assert (await http.post("/api/v1/swarm/knowledge", json=body())).status_code == 403


async def test_identity_and_status_spoof_are_rejected(harness):
    http, _, _ = harness
    response = await http.post("/api/v1/swarm/knowledge", json=body(author_id="attacker", status="verified"))
    assert response.status_code == 422


async def test_unknown_project_is_400(harness):
    http, _, _ = harness
    assert (await http.post("/api/v1/swarm/knowledge", json=body(project_id="unknown"))).status_code == 400


async def test_session_ingestion_always_requires_trusted_retention_policy(harness):
    http, _, _ = harness
    response = await http.post("/api/v1/swarm/knowledge", json=body(ingestion_kind="session"))
    assert response.status_code == 409
    assert response.json()["detail"] == "retention_policy_required"
    assert (
        await http.post("/api/v1/swarm/knowledge", json=body(ingestion_kind="session", retention_policy="client-claim"))
    ).status_code == 422


async def test_candidate_is_pinned_to_authenticated_identity(harness):
    http, _, store = harness
    response = await http.post("/api/v1/swarm/knowledge", json=body())
    assert response.status_code == 201
    revision = store.appended[-1]
    assert revision.author_id == "admin"
    assert revision.status == "candidate"
    assert revision.classification == "internal"
    assert revision.verifier_id is None


async def test_store_error_is_sanitized(harness):
    http, _, store = harness
    store.error = RuntimeError("secret database detail")
    response = await http.post("/api/v1/swarm/knowledge", json=body())
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid knowledge request"
    assert "secret" not in response.text


async def test_naive_expiry_is_422_not_500(harness):
    http, _, _ = harness
    response = await http.post("/api/v1/swarm/knowledge", json=body(expires_at="2030-01-01T00:00:00"))
    assert response.status_code == 422


async def test_get_returns_parsed_context_object(harness):
    http, _, _ = harness
    response = await http.get("/api/v1/swarm/knowledge?project_id=project-a")
    assert response.status_code == 200
    assert response.json() == {"bounded": True, "conflicts": [], "items": [], "revision_manifest": [], "omissions": []}


async def test_get_accepts_query_and_redaction_reports_pending(harness, monkeypatch):
    http, _, store = harness
    response = await http.get("/api/v1/swarm/knowledge?project_id=project-a&query=feature+intent")
    assert response.status_code == 200
    assert store.query == "feature intent"

    response = await http.post("/api/v1/swarm/knowledge/r1/redact")
    assert response.status_code == 200
    assert response.json() == {"redacted": True, "purge_pending": True}


async def test_status_requires_admin(harness):
    http, principal, _ = harness
    principal.role = "operator"
    response = await http.get("/api/v1/swarm/knowledge/status")
    assert response.status_code == 403


async def test_status_contains_only_safe_backend_fields(harness):
    http, _, _ = harness
    response = await http.get("/api/v1/swarm/knowledge/status")
    assert response.status_code == 200
    assert set(response.json()) == {"backend", "ready", "last_elapsed_ms", "error_code"}
    assert "secret" not in response.text.lower()
    assert "uri" not in response.text.lower()


def test_registry_load_failure_is_named(monkeypatch, tmp_path):
    monkeypatch.setattr(
        swarm_memory, "load_registry", lambda path, check_git=False: (_ for _ in ()).throw(OSError("secret"))
    )
    with pytest.raises(RuntimeError, match="memory registry load failed"):
        swarm_memory.build_memory_router(None, SECRET, str(tmp_path / "registry.json"))
