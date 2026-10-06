"""Controller-run PostgreSQL integration tests for L1.1 shared memory."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401,F811
from whilly.adapters.db.learning_memory import PostgresMemoryStore
from whilly.swarm.learning.domain import KnowledgeRevision, Principal

pytestmark = pytest.mark.integration


def _revision(product: str, *, project: str | None = "project-a", revision_id: str | None = None, **changes) -> KnowledgeRevision:
    values = dict(
        id=revision_id or str(uuid4()), product_id=product, project_id=project, kind="fact", body="neutral test body",
        source_uri="https://example.test/neutral", source_sha=None, evidence_hash="evidence",
        observed_at=datetime.now(timezone.utc), verified_at=None, expires_at=None, classification="internal",
        status="candidate", author_id="actor-a", verifier_id=None, policy_version="v1",
    )
    values.update(changes)
    return KnowledgeRevision(**values)


@pytest.fixture
async def product(db_pool):
    product_id = f"test-product-{uuid4()}"
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products (id, name) VALUES ($1, $2)", product_id, "neutral test product")
    try:
        yield product_id
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM swarm_learning_payloads WHERE revision_id IN "
                "(SELECT id FROM swarm_learning_revisions WHERE product_id=$1)",
                product_id,
            )
            await conn.execute("DELETE FROM swarm_learning_revisions WHERE product_id=$1", product_id)
            await conn.execute("DELETE FROM swarm_products WHERE id=$1", product_id)


def _principal(*, projects=("project-a",), products=(), classifications=("internal",)) -> Principal:
    return Principal("actor-a", tuple(products), tuple(projects), tuple(classifications))


async def test_product_project_classification_isolation_and_unauthorized_redact(db_pool, product):
    store = PostgresMemoryStore(db_pool)
    allowed = _principal(products=(product,))
    await store.append(_revision(product, project="project-a"))
    unauthorized_revision = _revision(product, project="project-b")
    await store.append(unauthorized_revision)
    await store.append(_revision(product, project="project-a", classification="restricted"))
    assert len(await store.visible(allowed, product, ("project-a",))) == 1
    assert await store.redact(allowed, unauthorized_revision.id) is False
    project_b_view = await store.visible(_principal(products=(product,), projects=("project-b",)), product, ("project-b",))
    assert [item.body for item in project_b_view] == [unauthorized_revision.body]
    assert [item.source_uri for item in project_b_view] == [unauthorized_revision.source_uri]
    assert len(await store.visible(allowed, product, ("project-a",))) == 1  # classification grant excludes restricted


async def test_redaction_hides_payload_and_successor_reference(db_pool, product):
    store = PostgresMemoryStore(db_pool)
    principal = _principal(products=(product,))
    original = _revision(product, revision_id=str(uuid4()))
    successor = _revision(product, supersedes=original.id)
    await store.append(original)
    await store.append(successor)
    assert [item.id for item in await store.visible(principal, product, ("project-a",))] == [successor.id]
    assert await store.redact(principal, original.id) is True
    assert await store.visible(principal, product, ("project-a",)) == []
    with pytest.raises(ValueError):
        await store.append(_revision(product, supersedes=original.id))


async def test_concurrent_successors_and_duplicate_id_are_safe(db_pool, product):
    store = PostgresMemoryStore(db_pool)
    original = _revision(product, revision_id=str(uuid4()))
    await store.append(original)
    successors = [_revision(product, supersedes=original.id) for _ in range(2)]
    results = await asyncio.gather(*(store.append(item) for item in successors), return_exceptions=True)
    assert sum(isinstance(result, KnowledgeRevision) for result in results) == 2
    with pytest.raises(ValueError):
        await store.append(original)


async def test_product_wide_and_conflicts_only_reference(db_pool, product):
    store = PostgresMemoryStore(db_pool)
    principal = _principal(products=(product,), projects=())
    first = _revision(product, project=None)
    second = _revision(product, project=None, conflicts=(first.id,))
    await store.append(first)
    await store.append(second)
    assert len(await store.visible(principal, product, ())) == 2
