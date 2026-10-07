from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from whilly.api import auth_tokens, sessions, users_repo
from whilly.api.csrf import COOKIE_NAME, WhillySessionCSRFMiddleware
from whilly.api.swarm_learning import build_learning_router
from whilly.swarm.learning.experiments import EvaluationReport, Metric

SECRET = b"learning-test-secret-long-enough-0000"


class MemoryStore:
    def __init__(self) -> None:
        self.reports = {}
        self.stopped = set()
        self.evaluations = {}
        self.saved_decisions = []

    async def save_report(self, report):
        self.reports[report.run_id] = report

    async def get_report(self, run_id):
        return self.reports.get(run_id)

    async def stop(self, run_id):
        self.stopped.add(run_id)
        return True

    async def is_stopped(self, run_id):
        return run_id in self.stopped

    async def get_evaluation(self, experiment_id):
        return self.evaluations.get(experiment_id)

    async def experiment_metadata(self, experiment_id):
        return {"proposer_id": "agent-1", "policy_version": "p1"}

    async def save_decision(self, receipt):
        self.saved_decisions.append(receipt)

    async def decisions(self, experiment_id):
        return tuple(self.saved_decisions)


@pytest.fixture
async def harness(monkeypatch):
    principal = SimpleNamespace(username="owner-1", role="admin")
    monkeypatch.setattr(users_repo, "get_user_by_username", AsyncMock(return_value=principal))
    monkeypatch.setattr(
        sessions,
        "verify_session",
        AsyncMock(
            return_value=SimpleNamespace(
                email="owner@example.com",
                session_id="test",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        ),
    )
    store = MemoryStore()
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware)
    app.include_router(build_learning_router(None, SECRET, store=store))
    cookie = auth_tokens.mint_session_cookie_value(
        SECRET, session_id="test", email="owner@example.com", ttl_seconds=3600
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
        cookies={COOKIE_NAME: cookie},
    ) as http:
        yield http, store, principal


async def test_status_names_disabled_schedule_and_export(harness):
    http, _, _ = harness
    response = await http.get("/api/v1/swarm/learning/status")
    assert response.json() == {
        "schedule_enabled": False,
        "export_enabled": False,
        "blockers": ["schedule_configuration_required", "export_policy_required"],
    }


async def test_fixture_dry_run_persists_report_and_stop_blocks_replay(harness):
    http, _, _ = harness
    response = await http.post(
        "/api/v1/swarm/learning/research/dry-run",
        json={"run_id": "run-1", "events": [{"kind": "task_outcome", "outcome": "blocked", "cost": None}]},
    )
    assert response.status_code == 200
    assert response.json()["report"]["sample_size"] == 1
    repeated = await http.post(
        "/api/v1/swarm/learning/research/dry-run",
        json={"run_id": "run-1", "events": [{"kind": "task_outcome", "outcome": "blocked", "cost": None}]},
    )
    assert repeated.json() == response.json()
    conflict = await http.post(
        "/api/v1/swarm/learning/research/dry-run",
        json={"run_id": "run-1", "events": [{"kind": "task_outcome", "outcome": "changed"}]},
    )
    assert conflict.status_code == 400
    assert conflict.json()["detail"] == "run_identity_conflict"
    assert (await http.get("/api/v1/swarm/learning/research/run-1/report")).status_code == 200
    assert (await http.post("/api/v1/swarm/learning/research/run-1/stop")).status_code == 200
    blocked = await http.post("/api/v1/swarm/learning/research/dry-run", json={"run_id": "run-1", "events": []})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "stop_requested"


async def test_dry_run_rejects_unredacted_query_and_forged_authority(harness):
    http, _, _ = harness
    response = await http.post(
        "/api/v1/swarm/learning/research/dry-run",
        json={"run_id": "run-2", "events": [{"kind": "research_query", "raw_query": "secret"}]},
    )
    assert response.status_code == 400
    forged = await http.post(
        "/api/v1/swarm/learning/research/dry-run",
        json={"run_id": "run-2", "events": [], "owner": True},
    )
    assert forged.status_code == 422


async def test_post_requires_csrf(harness):
    http, _, _ = harness
    http.headers["Origin"] = "https://untrusted.example"
    assert (await http.post("/api/v1/swarm/learning/research/run-1/stop")).status_code == 403


async def test_experiment_read_serializes_frozen_metrics(harness):
    http, store, _ = harness
    store.evaluations["exp-1"] = EvaluationReport(
        experiment_id="exp-1",
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        metrics={"correctness_rate": Metric(1.0, 1)},
        unknown_cost_count=0,
        recommendation="candidate",
        manifest={"id": "exp-1", "policy_version": "p1"},
    )

    response = await http.get("/api/v1/swarm/learning/experiments/exp-1")

    assert response.status_code == 200
    assert response.json()["report"]["metrics"]["correctness_rate"] == {"value": 1.0, "sample_size": 1}
