"""Router contract with a deterministic runtime seam; no agent or database needed."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware

SECRET = b"swarm-ui-test-secret-32-bytes-long"
SID = "s0123456789"
UTC = timezone.utc


class Service:
    def __init__(self, pool):
        self.rows = {}
        self.messages = []
        self.revisions = [{"revision": 1, "status": "proposed", "plan": {"tasks": []}}]
        self.calls = []
        self.gate = asyncio.Event()
        self.fail = False
        self.store = self

    async def create_session(self, registry_path, *, title=""):
        self.calls.append(("create", registry_path))
        self.rows[SID] = {"id": SID, "title": title, "applied_revision": None}
        return SID

    async def get_session(self, sid):
        return self.rows.get(sid)

    async def require_session(self, sid):
        return self.rows[sid]

    async def list_sessions(self, limit=100, *, include_test=False, include_archived=False):
        return [
            r
            for r in self.rows.values()
            if (include_test or not r.get("is_test")) and (include_archived or not r.get("archived"))
        ][:limit]

    async def set_session_flags(self, sid, *, archived=None, is_test=None):
        row = self.rows.get(sid)
        if row is not None:
            for key, value in [("archived", archived), ("is_test", is_test)]:
                row[key] = value if value is not None else row.get(key, False)
        return row

    async def chat_history(self, sid):
        return list(self.messages)

    async def list_revisions(self, sid):
        return self.revisions

    async def add_chat_message(self, sid, sender, body):
        self.messages.append({"sender": sender, "body": body})

    async def chat(self, sid, text, *, request_plan=False):
        self.calls.append(("chat", request_plan))
        await self.gate.wait()
        if self.fail:
            raise RuntimeError("private detail must not leak")
        await self.add_chat_message(sid, "assistant", text)
        return SimpleNamespace(error=None, reply=text)

    async def apply_revision(self, sid, revision):
        self.calls.append(("apply", revision))
        self.rows[sid]["applied_revision"] = revision
        self.revisions[0]["status"] = "applied"
        return revision, []

    async def status(self, sid):
        return {"session": self.rows[sid], "tasks": []}

    async def report(self, sid):
        return {"summary": "result"}

    async def stop(self, sid, *, kill=False):
        self.calls.append(("stop", kill))


class Coordinator:
    instances = []

    def __init__(self, service, sid, *, max_parallel=None):
        self.service = service
        self.stopped = False
        self.cancelled = False
        self.instances.append(self)

    def request_stop(self, reason):
        self.stopped = True

    async def run(self):
        try:
            await self.service.gate.wait()
            return SimpleNamespace(status="finished", accepted=[], failed=[])
        finally:
            self.cancelled = True


@pytest.fixture
async def harness(monkeypatch):
    from whilly.api import swarm_ui

    service = Service(None)
    Coordinator.instances = []
    monkeypatch.setattr(swarm_ui, "_runtime", lambda: (lambda pool: service, Coordinator))
    session_obj = SimpleNamespace(
        email="admin@local",
        session_id="test-session",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(sessions, "verify_session", AsyncMock(return_value=session_obj))
    user = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=user))
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware)
    router = swarm_ui.build_swarm_router(pool=None, secret=SECRET, registry_path="/trusted/registry.json")
    app.include_router(router)
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test-session", email="admin@local", ttl_seconds=3600
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
            cookies={COOKIE_NAME: cookie},
        ) as client:
            yield client, service, user


@pytest.mark.parametrize("role,code", [(None, 401), ("readonly", 403), ("operator", 403)])
async def test_auth_before_all_actions(harness, role, code):
    client, service, user = harness
    if role is None:
        client.cookies.clear()
        client.headers["Authorization"] = "Bearer worker-token"
    else:
        user.role = role
    for method, path, body in [
        ("GET", "/swarm", None),
        ("GET", "/api/v1/swarm/sessions", None),
        ("POST", "/api/v1/swarm/sessions", {}),
        ("PATCH", f"/api/v1/swarm/sessions/{SID}/flags", {"archived": True}),
        ("GET", f"/api/v1/swarm/sessions/{SID}", None),
        *[("POST", f"/api/v1/swarm/sessions/{SID}/{action}", {}) for action in ("chat", "run", "resume", "stop")],
    ]:
        response = await client.request(method, path, json=body)
        assert response.status_code == code, response.text
    assert service.calls == []


async def create(client):
    response = await client.post("/api/v1/swarm/sessions", json={"title": "Conversation"})
    assert response.status_code == 201, response.text
    return f"/api/v1/swarm/sessions/{response.json()['id']}"


async def test_session_organization_and_validation(harness):
    client, service, _ = harness
    base = await create(client)
    assert (await client.patch(base + "/flags", json={"is_test": True})).status_code == 200
    assert (await client.get("/api/v1/swarm/sessions")).json()["sessions"] == []
    assert len((await client.get("/api/v1/swarm/sessions?include_test=true")).json()["sessions"]) == 1
    assert (await client.patch(base + "/flags", json={"archived": True})).status_code == 200
    assert (await client.get("/api/v1/swarm/sessions?include_test=true")).json()["sessions"] == []
    assert (await client.patch(base + "/flags", json={"archived": False, "is_test": False})).status_code == 200
    assert len((await client.get("/api/v1/swarm/sessions")).json()["sessions"]) == 1
    assert (await client.patch(base + "/flags", json={"archived": "yes"})).status_code == 422
    assert (await client.patch(base + "/flags", json={})).status_code == 422
    assert (await client.patch(base + "/flags", json={"delete": True})).status_code == 422
    assert (await client.patch("/api/v1/swarm/sessions/s0000000000/flags", json={"archived": True})).status_code == 404
    assert (
        await client.patch(base + "/flags", json={"archived": True}, headers={"Origin": "https://foreign.example"})
    ).status_code == 403


async def settle(client, base):
    for _ in range(100):
        data = (await client.get(base)).json()
        if not data["job"]["active"]:
            return data
        await asyncio.sleep(0.001)
    pytest.fail("job did not finish")


async def test_admin_discuss_plan_and_explicit_revision(harness):
    client, service, _ = harness
    base = await create(client)
    assert service.calls == [("create", "/trusted/registry.json")]
    service.gate.set()
    for mode in ("discuss", "plan"):
        assert (await client.post(base + "/chat", json={"text": "hello", "mode": mode})).status_code == 202
        settled = await settle(client, base)
        assert settled["job"]["error"] is None
    assert ("chat", False) in service.calls and ("chat", True) in service.calls
    assert not Coordinator.instances
    assert (await client.post(base + "/run", json={"revision": 1, "workers": 2})).status_code == 202
    data = await settle(client, base)
    assert data["job"]["error"] is None
    assert ("apply", 1) in service.calls
    assert data["status"]["session"]["applied_revision"] == 1
    assert (await client.get(base + "/report")).json() == {"summary": "result"}
    assert (await client.get("/swarm")).status_code == 200


async def test_history_is_raw_json_for_textcontent(harness):
    client, service, _ = harness
    base = await create(client)
    service.messages.append({"sender": "assistant", "body": "<tag>&"})
    payload = (await client.get(base)).json()
    assert payload["history"][-1]["body"] == "<tag>&"


@pytest.mark.parametrize(
    "action,payload",
    [
        ("chat", {"text": ""}),
        ("chat", {"text": "x" * 16001}),
        ("chat", {"text": "ok", "mode": "execute"}),
        ("run", {}),
        ("run", {"revision": 0}),
        ("run", {"revision": True}),
        ("run", {"revision": 1, "workers": 9}),
        ("resume", {"workers": 0}),
        ("stop", {"kill": True}),
    ],
)
async def test_validation(harness, action, payload):
    client, service, _ = harness
    base = await create(client)
    assert (await client.post(base + "/" + action, json=payload)).status_code == 422
    assert len(service.calls) == 1


async def test_unknown_revision_registry_override_and_csrf(harness):
    client, service, _ = harness
    assert (await client.get("/api/v1/swarm/sessions/s9999999999")).status_code == 404
    assert (await client.get("/api/v1/swarm/sessions/bad!")).status_code == 422
    assert (await client.post("/api/v1/swarm/sessions", json={"registry_path": "/evil"})).status_code == 422
    base = await create(client)
    assert (await client.post(base + "/run", json={"revision": 9})).status_code == 409
    assert (await client.post(base + "/resume", json={})).status_code == 409
    assert (
        await client.post(base + "/chat", json={"text": "x"}, headers={"Origin": "https://evil.example"})
    ).status_code == 403
    assert len(service.calls) == 1


async def test_duplicate_error_persistence_and_stop(harness):
    client, service, _ = harness
    base = await create(client)
    assert (await client.post(base + "/chat", json={"text": "hello"})).status_code == 202
    assert (await client.post(base + "/chat", json={"text": "duplicate"})).status_code == 409
    assert (await client.post(base + "/run", json={"revision": 1})).status_code == 409
    service.fail = True
    service.gate.set()
    data = await settle(client, base)
    assert data["job"]["error"] == "chat_failed: RuntimeError"
    assert service.messages[-1]["body"] == "chat_failed: RuntimeError"
    service.gate.clear()
    assert (await client.post(base + "/run", json={"revision": 1})).status_code == 202
    await asyncio.sleep(0.01)
    assert (await client.post(base + "/stop", json={})).status_code == 200
    assert Coordinator.instances[-1].stopped and Coordinator.instances[-1].cancelled
    assert (await client.post(base + "/resume", json={})).status_code == 202


async def test_shutdown_cancels_coordinator(harness):
    client, service, _ = harness
    base = await create(client)
    await client.post(base + "/run", json={"revision": 1})
    await asyncio.sleep(0.01)
    # Exercise the actual merged router lifespan while a coordinator is active.
    app = client._transport.app
    coordinator = Coordinator.instances[-1]
    assert not coordinator.cancelled
    async with app.router.lifespan_context(app):
        pass
    assert coordinator.stopped and coordinator.cancelled
    data = (await client.get(base)).json()
    assert not data["job"]["active"]
