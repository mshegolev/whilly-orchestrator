from __future__ import annotations

import uuid
import json
import asyncio

import pytest

from tests.integration import test_swarm_runtime as swarm_fixtures
from whilly.adapters.db.learning_proposal_admission import PostgresProposalAdmission
from whilly.adapters.db.learning_proposals import PostgresProposalStore
from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.proposals import ProposalService, TaskProposal
from whilly.swarm.registry import load_registry

pytestmark = pytest.mark.integration

swarm_dsn = swarm_fixtures.swarm_dsn
db_pool = swarm_fixtures.db_pool
ecosystem = swarm_fixtures.ecosystem


def _proposal(
    feature_id: str,
    proposal_id: str,
    *,
    target_module="src/module.py",
    contract_impact="none",
    dependencies=(),
    outcome=None,
):
    return TaskProposal(
        id=proposal_id,
        origin_feature_id=feature_id,
        origin_task_id=None,
        target_project="demo-lib",
        target_module=target_module,
        evidence_refs=("message:evidence-1",),
        outcome=outcome or f"Improve the module for {feature_id}",
        contract_impact=contract_impact,
        acceptance=("test -f result.txt",),
        dependencies=dependencies,
        resource_class="small",
    )


async def _service(
    db_pool,
    ecosystem,
    *,
    feature_status="planned",
    budget=None,
    target_module="src/module.py",
    contract_impact="none",
    dependencies=(),
    proposal_id=None,
):
    suffix = uuid.uuid4().hex
    feature_id = f"feature-{suffix}"
    session_id = f"session-{suffix}"
    proposal_id = proposal_id or f"proposal-{suffix}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO swarm_products (id, name) VALUES ('default', 'admission test') ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO swarm_sessions (id, registry_path) VALUES ($1, $2)", session_id, str(ecosystem["registry"])
        )
        await conn.execute(
            """INSERT INTO swarm_product_features
               (id, product_id, title, intent, session_id, status, revision, spec, budget, approval_digest, approved_digest)
               VALUES ($1,'default','feature','proposal',$2,$3,0,'{"summary":"old"}'::jsonb,
                       $4::jsonb,'old','old')""",
            feature_id,
            session_id,
            feature_status,
            json.dumps(budget or {"max_calls": 10, "max_elapsed_seconds": 100}),
        )
    registry = load_registry(ecosystem["registry"])
    store = PostgresProposalStore(db_pool, actor_host="integration-host")
    admission = PostgresProposalAdmission(db_pool, actor_host="integration-host")
    service = ProposalService(
        store,
        registered_projects={"default": ("demo-lib",)},
        admission_port=admission,
        owner_verified=True,
        actor_id="owner",
        registry=registry,
    )
    principal = Principal("owner", ("default",), ("demo-lib",), ("internal",))
    submitted = await service.submit(
        principal,
        _proposal(
            feature_id,
            proposal_id,
            target_module=target_module,
            contract_impact=contract_impact,
            dependencies=dependencies,
        ),
    )
    assert submitted.id == proposal_id
    assert submitted.duplicate_of is None
    return service, principal, feature_id, proposal_id


@pytest.mark.asyncio
async def test_accept_for_planning_persists_spec_revision_and_decision_history(db_pool, ecosystem):
    service, principal, feature_id, proposal_id = await _service(db_pool, ecosystem)
    result = await service.accept_for_planning(principal, proposal_id, expected_revision=0, reason="reviewed")
    assert result.status == "awaiting_approval"
    async with db_pool.acquire() as conn:
        before_reject = await conn.fetchrow(
            "SELECT revision,spec,approval_digest,approved_digest FROM swarm_product_features WHERE id=$1", feature_id
        )
    retraction = await service.reject(principal, proposal_id, reason="retract")
    assert retraction.status == "blocked"
    assert retraction.reason == "proposal_already_in_plan"
    assert (await service.store.get("default", proposal_id))["status"] == "awaiting_approval"
    async with db_pool.acquire() as conn:
        feature = await conn.fetchrow(
            "SELECT revision,status,spec,approval_digest,approved_digest FROM swarm_product_features WHERE id=$1",
            feature_id,
        )
        states = await conn.fetch(
            "SELECT from_state,to_state,reason FROM swarm_learning_proposal_events WHERE proposal_id=$1 ORDER BY event_id",
            proposal_id,
        )
        after_reject = await conn.fetchrow(
            "SELECT revision,spec,approval_digest,approved_digest FROM swarm_product_features WHERE id=$1", feature_id
        )
    assert feature["revision"] == 1 and feature["status"] == "draft"
    assert feature["approval_digest"] is None and feature["approved_digest"] is None
    spec = json.loads(feature["spec"]) if isinstance(feature["spec"], str) else feature["spec"]
    assert spec["previous_spec"] == {"summary": "old"}
    accepted = spec["accepted_proposals"][0]
    assert accepted["id"] == proposal_id
    assert accepted["target_project"] == "demo-lib"
    assert accepted["target_module"] == "src/module.py"
    assert accepted["contract_impact"] == "none"
    assert accepted["resource_class"] == "small"
    assert [row["to_state"] for row in states] == ["proposed", "triaged", "awaiting_approval"]
    assert states[-1]["reason"] == "reviewed"
    assert dict(after_reject) == dict(before_reject)


@pytest.mark.asyncio
async def test_stale_and_running_feature_are_blocked_without_revision_change(db_pool, ecosystem):
    service, principal, feature_id, proposal_id = await _service(db_pool, ecosystem)
    stale = await service.accept_for_planning(principal, proposal_id, expected_revision=99, reason="stale")
    assert stale.status == "blocked" and stale.reason == "stale_feature_revision"
    running_service, running_principal, running_feature, running_proposal = await _service(
        db_pool, ecosystem, feature_status="running"
    )
    blocked = await running_service.accept_for_planning(
        running_principal, running_proposal, expected_revision=0, reason="running"
    )
    assert blocked.status == "blocked" and blocked.reason == "feature_running"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT revision FROM swarm_product_features WHERE id=$1", feature_id) == 0
        assert await conn.fetchval("SELECT revision FROM swarm_product_features WHERE id=$1", running_feature) == 0


@pytest.mark.asyncio
async def test_accept_retry_is_idempotent(db_pool, ecosystem):
    service, principal, feature_id, proposal_id = await _service(db_pool, ecosystem)
    first = await service.accept_for_planning(principal, proposal_id, expected_revision=0, reason="approved")
    second = await service.accept_for_planning(principal, proposal_id, expected_revision=0, reason="retry")
    assert first.status == second.status == "awaiting_approval"
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT revision FROM swarm_product_features WHERE id=$1", feature_id) == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM swarm_learning_proposal_events WHERE proposal_id=$1", proposal_id)
            == 3
        )


@pytest.mark.asyncio
async def test_adapter_policy_blocks_protected_and_contract_changes(db_pool, ecosystem):
    service, principal, _, proposal_id = await _service(
        db_pool,
        ecosystem,
        target_module=".git/config",
        contract_impact="public API change",
    )
    result = await service.evaluate(principal, proposal_id)
    assert result.status == "blocked"
    assert "protected_module_path" in result.blockers
    assert "contract_verification_proof_type_missing" in result.blockers


@pytest.mark.asyncio
async def test_adapter_policy_blocks_cycle_and_invalid_budget(db_pool, ecosystem):
    proposal_id = f"proposal-{uuid.uuid4().hex}"
    service, principal, _, _ = await _service(
        db_pool,
        ecosystem,
        budget={"max_calls": 0, "max_elapsed_seconds": 100},
        dependencies=(f"proposal:{proposal_id}",),
        proposal_id=proposal_id,
    )
    result = await service.evaluate(principal, proposal_id)
    assert result.status == "blocked"
    assert "feature_budget_unknown" in result.blockers
    assert "dependency_cycle" in result.blockers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget", [{"max_calls": 1001, "max_elapsed_seconds": 100}, {"max_calls": 10, "max_elapsed_seconds": 86401}]
)
async def test_adapter_uses_canonical_budget_bounds(db_pool, ecosystem, budget):
    service, principal, _, proposal_id = await _service(db_pool, ecosystem, budget=budget)
    result = await service.evaluate(principal, proposal_id)
    assert result.status == "blocked"
    assert result.reason == "feature_budget_unknown"


@pytest.mark.asyncio
async def test_concurrent_accept_retry_has_one_revision(db_pool, ecosystem):
    service, principal, feature_id, proposal_id = await _service(db_pool, ecosystem)
    results = await asyncio.gather(
        service.accept_for_planning(principal, proposal_id, expected_revision=0, reason="one"),
        service.accept_for_planning(principal, proposal_id, expected_revision=0, reason="two"),
    )
    assert {result.status for result in results} == {"awaiting_approval"}
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT revision FROM swarm_product_features WHERE id=$1", feature_id) == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM swarm_learning_proposal_events WHERE proposal_id=$1", proposal_id)
            == 3
        )


@pytest.mark.asyncio
async def test_accept_transaction_rolls_back_on_event_failure(db_pool, ecosystem, monkeypatch):
    service, principal, feature_id, proposal_id = await _service(db_pool, ecosystem)
    import whilly.adapters.db.learning_proposal_admission as admission_module

    async def fail_transition(*args, **kwargs):
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(admission_module, "_transition", fail_transition)
    with pytest.raises(RuntimeError, match="injected"):
        await service.accept_for_planning(principal, proposal_id, expected_revision=0, reason="rollback")
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT revision FROM swarm_product_features WHERE id=$1", feature_id) == 0
        assert await conn.fetchval("SELECT status FROM swarm_learning_proposals WHERE id=$1", proposal_id) == "proposed"
