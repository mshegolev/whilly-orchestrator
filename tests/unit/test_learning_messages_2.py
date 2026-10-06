"""Focused ASGI tests for the bounded L2 swarm-message admin API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api import swarm_messages
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.swarm.learning.messages import DeliveryPolicy, DeliveryReceipt, MessageService

SECRET = b"messages-test-secret-long-enough-0000"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class FakeMessageStore:
    def __init__(self) -> None:
        self.sent = []
        self.history = []

    async def persist(self, envelope, max_fanout):
        self.sent.append((envelope, max_fanout))
        return DeliveryReceipt(
            message_id=envelope.id,
            state="persisted",
            sender_id=envelope.sender_id,
            recipient_project=envelope.recipient_project,
            recipient_role=envelope.recipient_role,
            idempotency_key=envelope.idempotency_key,
        )

    async def get(self, message_id):
        return None


class FakeProductStore:
    def __init__(self) -> None:
        self.calls = 0
        self.names = []

    async def ensure_product(self, name):
        self.calls += 1
        self.names.append(name)
        return {"id": "default", "name": name}


class FakeProducts:
    store = FakeProductStore()


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    project = SimpleNamespace(path=str(tmp_path), base_ref="main")
    role = SimpleNamespace(projects=("project-a",))
    registry = SimpleNamespace(projects={"project-a": project}, roles={"implementer": role}, raw={"name": "demo"})
    store = FakeMessageStore()
    products = FakeProductStore()
    monkeypatch.setattr(swarm_messages, "load_registry", lambda path, check_git=False: registry)

    def build(*args, **kwargs):
        service = MessageService(
            store,
            kwargs["policy"],
            lambda: NOW,
            {"default": tuple(registry.projects)},
            {project_id: ("implementer",) for project_id in registry.projects},
            sender_grants={kwargs["actor_id"]: kwargs["sender_scopes"]},
        )
        swarm_messages._test_service = service
        return service

    monkeypatch.setattr(swarm_messages, "build_delivery_service", build)
    monkeypatch.setattr(swarm_messages, "ProductStore", lambda pool: products)
    monkeypatch.setattr(swarm_messages, "delivery_policy_from_env", lambda: DeliveryPolicy(1000, 2, 4))
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
    app.include_router(swarm_messages.build_message_router(None, SECRET, str(tmp_path / "registry.json")))
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test", email="admin@example.com", ttl_seconds=3600
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
        cookies={COOKIE_NAME: cookie},
    ) as http:
        yield http, store, products


def message_body(**overrides):
    value = {
        "recipient_project": "project-a",
        "recipient_role": "implementer",
        "kind": "finding",
        "payload": {"html": "<b>data</b>"},
        "evidence_refs": ["test://evidence"],
        "correlation_id": "corr-1",
        "idempotency_key": "idem-1",
        "expires_at": "2030-01-02T00:00:00Z",
    }
    value.update(overrides)
    return value


async def test_status_reports_missing_policy_without_exposing_details(harness, monkeypatch):
    http, _, _ = harness
    monkeypatch.setattr(swarm_messages, "delivery_policy_from_env", lambda: None)
    response = await http.get("/api/v1/swarm/collaboration/status")
    assert response.json() == {"configured": False, "blocker": "message_delivery_policy_required"}


async def test_post_requires_csrf(harness):
    http, _, _ = harness
    http.headers["Origin"] = "https://untrusted.example"
    assert (await http.post("/api/v1/swarm/collaboration", json=message_body())).status_code == 403


async def test_post_policy_off_does_not_create_product_or_message(harness, monkeypatch):
    http, store, products = harness
    monkeypatch.setattr(swarm_messages, "delivery_policy_from_env", lambda: None)
    response = await http.post("/api/v1/swarm/collaboration", json=message_body())
    assert response.status_code == 409
    assert response.json()["detail"] == "message_delivery_policy_required"
    assert not store.sent
    assert products.calls == 0


async def test_forged_sender_and_authority_fields_are_rejected(harness):
    http, _, _ = harness
    response = await http.post(
        "/api/v1/swarm/collaboration",
        json=message_body(sender_id="attacker", authority={"admin": True}),
    )
    assert response.status_code == 422


async def test_html_payload_is_data_and_send_returns_persisted_outcome(harness):
    http, store, _ = harness
    response = await http.post("/api/v1/swarm/collaboration", json=message_body())
    assert response.status_code == 201
    assert response.json()["state"] == "persisted"
    envelope, _ = store.sent[-1]
    assert envelope.sender_id == "admin"
    assert envelope.payload["html"] == "<b>data</b>"


async def test_unknown_project_or_role_is_rejected_before_product_creation(harness):
    http, _, products = harness
    assert (
        await http.post("/api/v1/swarm/collaboration", json=message_body(recipient_project="nope"))
    ).status_code == 400
    assert (await http.post("/api/v1/swarm/collaboration", json=message_body(recipient_role="nope"))).status_code == 400
    assert products.calls == 0


async def test_invalid_kind_is_validation_error_without_product_write(harness):
    http, store, products = harness
    response = await http.post("/api/v1/swarm/collaboration", json=message_body(kind="not-a-kind"))
    assert response.status_code == 422
    assert not store.sent
    assert products.calls == 0


async def test_evidence_refs_are_bounded_strings_without_product_write(harness):
    http, store, products = harness
    for evidence_refs in ([""], ["x" * 513]):
        response = await http.post("/api/v1/swarm/collaboration", json=message_body(evidence_refs=evidence_refs))
        assert response.status_code == 422
    assert not store.sent
    assert products.calls == 0


async def test_oversize_payload_is_rejected_before_product_write(harness):
    http, store, products = harness
    response = await http.post("/api/v1/swarm/collaboration", json=message_body(payload={"data": "x" * 2000}))
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid message request"
    assert not store.sent
    assert products.calls == 0


async def test_manual_send_preserves_registry_product_name(harness):
    http, _, products = harness
    assert (await http.post("/api/v1/swarm/collaboration", json=message_body())).status_code == 201
    assert products.names == ["demo"]


async def test_history_is_read_only_bounded_and_exposes_expired_state(harness, monkeypatch):
    http, _, _ = harness
    history = [{"id": "m1", "product_id": "default", "state": "persisted", "expires_at": "2020-01-01T00:00:00+00:00"}]
    monkeypatch.setattr(swarm_messages, "history_for_projects", AsyncMock(return_value=history))
    response = await http.get("/api/v1/swarm/collaboration")
    assert response.status_code == 200
    assert response.json() == {"messages": [{**history[0], "state": "expired"}]}


async def test_non_admin_session_is_forbidden(harness, monkeypatch):
    http, _, _ = harness
    monkeypatch.setattr(
        users_repo, "get_user_by_session_email", AsyncMock(return_value=SimpleNamespace(username="op", role="operator"))
    )
    assert (await http.get("/api/v1/swarm/collaboration/status")).status_code == 403


def test_delivery_policy_rejects_malformed_and_extra_configuration(monkeypatch):
    monkeypatch.setenv("WHILLY_SWARM_DELIVERY_POLICY", "not-json")
    with pytest.raises(ValueError, match="valid JSON"):
        swarm_messages.delivery_policy_from_env()
    monkeypatch.setenv(
        "WHILLY_SWARM_DELIVERY_POLICY", '{"max_payload_bytes": 1, "max_hops": 1, "max_fanout": 1, "extra": 1}'
    )
    with pytest.raises(ValueError, match="exactly"):
        swarm_messages.delivery_policy_from_env()
