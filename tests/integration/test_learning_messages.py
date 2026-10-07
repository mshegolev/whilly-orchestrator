"""Disposable-PostgreSQL coverage for the durable learning message store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401,F811
from whilly.adapters.db.learning_messages import PostgresMessageStore
from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.messages import DeliveryPolicy, MessageEnvelope, MessageService

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_offline_recipient_expires_after_service_restart(db_pool):  # noqa: F811
    product = f"expiry-product-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    principal = Principal("host", (product,), ("project-b",), ("internal",))
    grants = {"host": ((product, "project-b", "reviewer"),)}
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products (id,name) VALUES ($1,'expiry test')", product)
    try:

        def service():
            return MessageService(
                PostgresMessageStore(db_pool),
                DeliveryPolicy(4096, 3, 2),
                lambda: now,
                {product: ("project-b",)},
                {"project-b": ("reviewer",)},
                recipient_grants=grants,
                sender_grants=grants,
            )

        receipt = await service().send(principal, _envelope(product, expires_at=now + timedelta(seconds=1)))
        assert receipt.state == "persisted"
        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT delivered_at FROM swarm_learning_message_outbox WHERE message_id=$1", receipt.message_id
                )
                is None
            )
        now += timedelta(seconds=2)
        restarted = service()
        assert await restarted.deliver(principal, recipient_project="project-b", recipient_role="reviewer") == []
        assert (
            await restarted.ack(principal, receipt.message_id, recipient_project="project-b", recipient_role="reviewer")
        ).state == "expired"
        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT state FROM swarm_learning_messages WHERE id=$1", receipt.message_id)
                == "expired"
            )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM swarm_products WHERE id=$1", product)


def _envelope(product: str, **overrides) -> MessageEnvelope:
    values = {
        "id": f"message-{uuid4().hex}",
        "product_id": product,
        "feature_id": None,
        "task_id": None,
        "sender_id": "untrusted-payload-sender",
        "recipient_project": "project-b",
        "recipient_role": "reviewer",
        "kind": "finding",
        "correlation_id": "correlation-1",
        "causation_id": None,
        "idempotency_key": f"idempotency-{uuid4().hex}",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "hop_count": 0,
        "payload": {"body": "durable finding"},
        "evidence_refs": (),
    }
    values.update(overrides)
    return MessageEnvelope(**values)


@pytest.mark.asyncio
async def test_postgres_outbox_inbox_retry_and_ack_are_atomic(db_pool):  # noqa: F811
    product = f"message-product-{uuid4().hex}"
    principal = Principal("host-sender", (product,), ("project-b",), ("internal",))
    store = PostgresMessageStore(db_pool)
    service = MessageService(
        store,
        DeliveryPolicy(max_payload_bytes=4096, max_hops=3, max_fanout=1),
        lambda: datetime.now(timezone.utc),
        {product: ("project-b",)},
        {"project-b": ("reviewer",)},
        {"host-sender": ((product, "project-b", "reviewer"),)},
        {"host-sender": ((product, "project-b", "reviewer"),)},
    )
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products (id, name) VALUES ($1, $2)", product, "message test")
    try:
        envelope = _envelope(product, id="message-fixed", idempotency_key="idem-fixed")
        await service.send(principal, envelope)
        await service.send(principal, envelope)

        first = await service.deliver(principal, recipient_project="project-b", recipient_role="reviewer")
        retry = await service.deliver(principal, recipient_project="project-b", recipient_role="reviewer")
        receipt = await service.ack(
            principal, "message-fixed", recipient_project="project-b", recipient_role="reviewer"
        )

        assert [item.id for item in first] == ["message-fixed"]
        assert [item.id for item in retry] == ["message-fixed"]
        assert receipt.state == "acknowledged"
        async with db_pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM swarm_learning_messages WHERE id=$1", "message-fixed") == 1
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM swarm_learning_message_outbox WHERE message_id=$1", "message-fixed"
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM swarm_learning_message_inbox WHERE message_id=$1", "message-fixed"
                )
                == 1
            )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM swarm_products WHERE id=$1", product)


@pytest.mark.asyncio
async def test_postgres_retry_with_changed_payload_is_rejected(db_pool):  # noqa: F811
    product = f"message-product-{uuid4().hex}"
    principal = Principal("host-sender", (product,), ("project-b",), ("internal",))
    store = PostgresMessageStore(db_pool)
    service = MessageService(
        store,
        DeliveryPolicy(max_payload_bytes=4096, max_hops=3, max_fanout=1),
        lambda: datetime.now(timezone.utc),
        {product: ("project-b",)},
        {"project-b": ("reviewer",)},
        {"host-sender": ((product, "project-b", "reviewer"),)},
        {"host-sender": ((product, "project-b", "reviewer"),)},
    )
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products (id, name) VALUES ($1, $2)", product, "message test")
    try:
        await service.send(principal, _envelope(product, id="message-fixed", idempotency_key="idem-fixed"))
        with pytest.raises(ValueError):
            await service.send(
                principal,
                _envelope(product, id="message-other", idempotency_key="idem-fixed", payload={"body": "changed"}),
            )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM swarm_products WHERE id=$1", product)


@pytest.mark.asyncio
async def test_postgres_concurrent_fanout_limit_is_atomic(db_pool):  # noqa: F811
    product = f"message-product-{uuid4().hex}"
    principal = Principal("host-sender", (product,), ("project-b",), ("internal",))
    store = PostgresMessageStore(db_pool)
    service = MessageService(
        store,
        DeliveryPolicy(max_payload_bytes=4096, max_hops=3, max_fanout=2),
        lambda: datetime.now(timezone.utc),
        {product: ("project-b",)},
        {"project-b": ("reviewer",)},
        {"host-sender": ((product, "project-b", "reviewer"),)},
        {"host-sender": ((product, "project-b", "reviewer"),)},
    )
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products (id, name) VALUES ($1, $2)", product, "message test")
    try:
        results = await asyncio.gather(
            *(
                service.send(
                    principal,
                    _envelope(product, id=f"message-{index}", idempotency_key=f"idem-{index}"),
                )
                for index in range(3)
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, ValueError) for result in results) == 1
        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM swarm_learning_messages WHERE product_id=$1 AND sender_id=$2 AND correlation_id=$3",
                    product,
                    "host-sender",
                    "correlation-1",
                )
                == 2
            )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM swarm_products WHERE id=$1", product)
