from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.integration import test_swarm_runtime as swarm_fixtures
from whilly.adapters.db.learning_proposals import PostgresProposalStore
from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.proposals import ProposalService, TaskProposal

pytestmark = pytest.mark.integration

swarm_dsn = swarm_fixtures.swarm_dsn
db_pool = swarm_fixtures.db_pool


def _proposal(*, proposal_id: str, product_id: str, outcome: str = "neighbor improvement") -> TaskProposal:
    return TaskProposal(
        id=proposal_id,
        origin_feature_id=f"feature-{product_id}",
        origin_task_id=None,
        target_project="target-project",
        target_module="src/module.py",
        evidence_refs=("message:evidence-1",),
        outcome=outcome,
        contract_impact="none",
        acceptance=("test -f result.txt",),
        dependencies=("task:canonical-task",),
        resource_class="small",
    )


async def _seed(pool, product_id: str) -> None:
    session_id = f"session-{product_id}"
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products (id, name) VALUES ($1, $2)", product_id, "proposal test")
        await conn.execute(
            "INSERT INTO swarm_sessions (id, registry_path) VALUES ($1, $2)", session_id, "/tmp/registry.json"
        )
        await conn.execute(
            """INSERT INTO swarm_product_features (id, product_id, title, intent, session_id)
            VALUES ($1,$2,'feature','proposal test',$3)""",
            f"feature-{product_id}",
            product_id,
            session_id,
        )


async def _service(pool, product_id: str) -> ProposalService:
    await _seed(pool, product_id)
    return ProposalService(
        PostgresProposalStore(pool, actor_host="integration-host"),
        registered_projects={product_id: ("target-project",)},
    )


@pytest.mark.asyncio
async def test_concurrent_duplicate_submission_one_proposal(db_pool) -> None:
    product_id = f"product-{uuid.uuid4().hex}"
    service = await _service(db_pool, product_id)
    proposal = _proposal(proposal_id=f"proposal-{uuid.uuid4().hex}", product_id=product_id)
    principal = Principal("agent", (product_id,), ("target-project",), ("internal",))

    results = await asyncio.gather(
        service.submit(principal, proposal),
        service.submit(principal, proposal),
    )

    assert {result.id for result in results} == {proposal.id}
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM swarm_learning_proposals WHERE product_id=$1", product_id) == 1


@pytest.mark.asyncio
async def test_changed_fingerprint_is_a_distinct_proposal(db_pool) -> None:
    product_id = f"product-{uuid.uuid4().hex}"
    service = await _service(db_pool, product_id)
    principal = Principal("agent", (product_id,), ("target-project",), ("internal",))
    first = await service.submit(principal, _proposal(proposal_id="proposal-a", product_id=product_id))
    second = await service.submit(
        principal,
        _proposal(proposal_id="proposal-b", product_id=product_id, outcome="different improvement"),
    )

    assert first.id == "proposal-a" and second.id == "proposal-b"
    assert second.duplicate_of is None


@pytest.mark.asyncio
async def test_proposal_content_and_event_history_are_immutable(db_pool) -> None:
    product_id = f"product-{uuid.uuid4().hex}"
    service = await _service(db_pool, product_id)
    principal = Principal("agent", (product_id,), ("target-project",), ("internal",))
    store = service.store
    result = await service.submit(principal, _proposal(proposal_id="proposal-history", product_id=product_id))
    await store.transition(product_id, result.id, "proposed", "triaged", actor_id="planner", reason="reviewed")

    with pytest.raises(Exception, match="immutable"):
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE swarm_learning_proposals SET outcome='tampered' WHERE id=$1", result.id)
    with pytest.raises(Exception, match="append-only"):
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM swarm_learning_proposal_events WHERE proposal_id=$1", result.id)
    assert len(await store.events(product_id, result.id)) == 2

    with pytest.raises(ValueError, match="reason"):
        await store.transition(
            product_id, result.id, "triaged", "awaiting_approval", actor_id="planner", reason="r" * 257
        )
    assert (await store.get(product_id, result.id))["status"] == "triaged"
    assert len(await store.events(product_id, result.id)) == 2

    wrong_product = f"product-{uuid.uuid4().hex}"
    assert await store.get(wrong_product, result.id) is None
    assert await store.events(wrong_product, result.id) == ()
    with pytest.raises(KeyError):
        await store.transition(wrong_product, result.id, "triaged", "awaiting_approval", actor_id="planner")

    with pytest.raises(Exception, match="illegal"):
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE swarm_learning_proposals SET status='verified' WHERE product_id=$1 AND id=$2",
                product_id,
                result.id,
            )
    with pytest.raises(Exception, match="missing proposal lifecycle event"):
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE swarm_learning_proposals SET status='awaiting_approval' WHERE product_id=$1 AND id=$2",
                product_id,
                result.id,
            )
    with pytest.raises(Exception, match="inconsistent"):
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO swarm_learning_proposal_events
                (proposal_id, from_state, to_state, actor_id, actor_host)
                VALUES ($1,'proposed','awaiting_approval','planner','integration-host')""",
                result.id,
            )


@pytest.mark.asyncio
async def test_same_id_and_fingerprint_in_another_product_is_not_disclosed(db_pool) -> None:
    first_product = f"product-{uuid.uuid4().hex}"
    second_product = f"product-{uuid.uuid4().hex}"
    first_service = await _service(db_pool, first_product)
    second_service = await _service(db_pool, second_product)
    first_principal = Principal("agent-a", (first_product,), ("target-project",), ("internal",))
    second_principal = Principal("agent-b", (second_product,), ("target-project",), ("internal",))
    first = await first_service.submit(first_principal, _proposal(proposal_id="shared-id", product_id=first_product))

    with pytest.raises(ValueError, match="conflicts with durable state"):
        await second_service.submit(second_principal, _proposal(proposal_id="shared-id", product_id=second_product))
    assert first.id == "shared-id"


@pytest.mark.asyncio
async def test_multiple_legal_transitions_and_events_commit_in_one_transaction(db_pool) -> None:
    product_id = f"product-{uuid.uuid4().hex}"
    service = await _service(db_pool, product_id)
    principal = Principal("agent", (product_id,), ("target-project",), ("internal",))
    result = await service.submit(principal, _proposal(proposal_id="proposal-batch", product_id=product_id))

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE swarm_learning_proposals SET status='triaged' WHERE product_id=$1 AND id=$2",
                product_id,
                result.id,
            )
            await conn.execute(
                """INSERT INTO swarm_learning_proposal_events
                (proposal_id, from_state, to_state, actor_id, actor_host)
                VALUES ($1,'proposed','triaged','planner','integration-host')""",
                result.id,
            )
            await conn.execute(
                "UPDATE swarm_learning_proposals SET status='awaiting_approval' WHERE product_id=$1 AND id=$2",
                product_id,
                result.id,
            )
            await conn.execute(
                """INSERT INTO swarm_learning_proposal_events
                (proposal_id, from_state, to_state, actor_id, actor_host)
                VALUES ($1,'triaged','awaiting_approval','planner','integration-host')""",
                result.id,
            )

    assert (await service.store.get(product_id, result.id))["status"] == "awaiting_approval"
    assert [event.to_state for event in await service.store.events(product_id, result.id)] == [
        "proposed",
        "triaged",
        "awaiting_approval",
    ]
