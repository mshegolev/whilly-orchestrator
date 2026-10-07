from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.messages import DeliveryPolicy, MessageEnvelope, MessageService


class FakeMessageStore:
    def __init__(self) -> None:
        self.messages: dict[str, MessageEnvelope] = {}
        self.by_key: dict[tuple[str, str, str], MessageEnvelope] = {}
        self.receipts = {}
        self.delivery_calls: list[str] = []
        self.task_completed = False
        self.last_principal_products: tuple[str, ...] | None = None
        self._lock = asyncio.Lock()

    async def persist(self, envelope, max_fanout):
        async with self._lock:
            key = (envelope.product_id, envelope.sender_id, envelope.idempotency_key)
            existing = self.by_key.get(key)
            if existing is not None:
                if existing.fingerprint() != envelope.fingerprint():
                    raise ValueError("retry changed message payload under the same idempotency key")
                return self.receipts.get(existing.id) or _receipt(existing)
            group = [
                item
                for item in self.messages.values()
                if (item.product_id, item.sender_id, item.correlation_id)
                == (envelope.product_id, envelope.sender_id, envelope.correlation_id)
            ]
            if len(group) >= max_fanout:
                raise ValueError("message fan-out exceeds policy limit")
            self.messages[envelope.id] = envelope
            self.by_key[key] = envelope
            return _receipt(envelope)

    async def get(self, message_id):
        return self.messages.get(message_id)

    async def list(self, principal, recipient_project, recipient_role, now):
        self.last_principal_products = principal.product_ids
        return [self.messages[item_id] for item_id in self.messages if item_id not in self.receipts]

    async def deliver(self, principal, recipient_project, recipient_role, now):
        self.last_principal_products = principal.product_ids
        result = await self.list(principal, recipient_project, recipient_role, now)
        self.delivery_calls.extend(message.id for message in result)
        return result

    async def acknowledge(self, principal, message_id, recipient_project, recipient_role, now):
        self.last_principal_products = principal.product_ids
        message = self.messages[message_id]
        receipt = _receipt(message, state="acknowledged")
        self.receipts[message_id] = receipt
        return receipt

    async def expire(self, now):
        return 0


def _receipt(message, state="persisted"):
    from whilly.swarm.learning.messages import DeliveryReceipt

    return DeliveryReceipt(
        message_id=message.id,
        state=state,
        sender_id=message.sender_id,
        recipient_project=message.recipient_project,
        recipient_role=message.recipient_role,
        idempotency_key=message.idempotency_key,
    )


def _envelope(**overrides):
    values = {
        "id": "m-1",
        "product_id": "product-a",
        "feature_id": "feature-1",
        "task_id": "task-1",
        "sender_id": "forged-sender",
        "recipient_project": "project-b",
        "recipient_role": "reviewer",
        "kind": "finding",
        "correlation_id": "corr-1",
        "causation_id": None,
        "idempotency_key": "idem-1",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "hop_count": 0,
        "payload": {"body": "safe data"},
        "evidence_refs": ("evidence-1",),
    }
    values.update(overrides)
    return MessageEnvelope(**values)


def _principal(*, actor="host-sender", products=("product-a",), projects=("project-b",)):
    return Principal(actor, products, projects, ("internal",))


def _service(store=None, *, roles=None, policy="default", grants=None, projects=None, sender_grants=None):
    resolved_policy = (
        DeliveryPolicy(max_payload_bytes=4096, max_hops=3, max_fanout=1) if policy == "default" else policy
    )
    return MessageService(
        store or FakeMessageStore(),
        policy=resolved_policy,
        clock=lambda: datetime(2026, 9, 29, tzinfo=timezone.utc),
        registered_projects=projects or {"product-a": ("project-b",)},
        registered_roles=roles or {"project-b": ("reviewer",)},
        recipient_grants=({"host-sender": (("product-a", "project-b", "reviewer"),)} if grants is None else grants),
        sender_grants=(
            {"host-sender": (("product-a", "project-b", "reviewer"),)} if sender_grants is None else sender_grants
        ),
    )


@pytest.mark.asyncio
async def test_sender_is_host_pinned():
    store = FakeMessageStore()
    receipt = await _service(store).send(_principal(), _envelope())

    assert receipt.sender_id == "host-sender"
    assert store.messages["m-1"].sender_id == "host-sender"


@pytest.mark.asyncio
async def test_cross_product_message_denied():
    service = _service()

    with pytest.raises(PermissionError):
        await service.send(_principal(products=("other-product",)), _envelope())


@pytest.mark.asyncio
async def test_send_grant_does_not_require_recipient_inbox_access():
    service = _service(grants={})

    receipt = await service.send(_principal(), _envelope())

    assert receipt.state == "persisted"


@pytest.mark.asyncio
async def test_send_to_registered_role_without_sender_grant_is_denied():
    service = _service(sender_grants={})

    with pytest.raises(PermissionError):
        await service.send(_principal(), _envelope())


@pytest.mark.asyncio
async def test_send_cross_product_destination_grant_is_denied():
    service = _service(
        projects={"product-a": ("project-b",), "product-b": ("project-b",)},
        sender_grants={"host-sender": (("product-b", "project-b", "reviewer"),)},
    )

    with pytest.raises(PermissionError):
        await service.send(_principal(), _envelope(product_id="product-b"))


@pytest.mark.asyncio
async def test_duplicate_delivery_one_inbox_and_retry_is_one_logical_message():
    store = FakeMessageStore()
    service = _service(store)
    await service.send(_principal(), _envelope())

    first = await service.deliver(_principal(), recipient_project="project-b", recipient_role="reviewer")
    retry = await service.deliver(_principal(), recipient_project="project-b", recipient_role="reviewer")

    assert [message.id for message in first] == ["m-1"]
    assert [message.id for message in retry] == ["m-1"]
    assert len(store.messages) == 1
    assert store.delivery_calls == ["m-1", "m-1"]


@pytest.mark.asyncio
async def test_ack_does_not_complete_task():
    store = FakeMessageStore()
    service = _service(store)
    await service.send(_principal(), _envelope())

    receipt = await service.ack(_principal(), "m-1", recipient_project="project-b", recipient_role="reviewer")

    assert receipt.state == "acknowledged"
    assert store.task_completed is False


@pytest.mark.asyncio
async def test_missing_policy_fails_closed_before_store():
    store = FakeMessageStore()
    service = _service(store, policy=None)

    with pytest.raises(PermissionError):
        await service.send(_principal(), _envelope())
    assert store.messages == {}

    recipient = _principal()
    with pytest.raises(PermissionError):
        await service.list(recipient, recipient_project="project-b", recipient_role="reviewer")
    with pytest.raises(PermissionError):
        await service.deliver(recipient, recipient_project="project-b", recipient_role="reviewer")
    with pytest.raises(PermissionError):
        await service.ack(recipient, "m-1", recipient_project="project-b", recipient_role="reviewer")


@pytest.mark.asyncio
async def test_changed_payload_under_same_idempotency_key_is_rejected():
    store = FakeMessageStore()
    service = _service(store)
    await service.send(_principal(), _envelope())

    with pytest.raises(ValueError):
        await service.send(_principal(), _envelope(payload={"body": "changed"}))


@pytest.mark.asyncio
async def test_same_content_retry_with_new_message_id_returns_original_receipt():
    store = FakeMessageStore()
    service = _service(store)
    first = await service.send(_principal(), _envelope())
    retry = await service.send(_principal(), _envelope(id="m-2", expires_at=store.messages["m-1"].expires_at))

    assert first.message_id == retry.message_id == "m-1"
    assert list(store.messages) == ["m-1"]


@pytest.mark.asyncio
async def test_recipient_grants_reject_wrong_role_actor_and_cross_product():
    wrong_role = _service(grants={"other-actor": (("product-a", "project-b", "reviewer"),)})
    with pytest.raises(PermissionError):
        await wrong_role.deliver(_principal(), recipient_project="project-b", recipient_role="reviewer")

    cross_product = _service(grants={"host-sender": (("product-b", "project-b", "reviewer"),)})
    with pytest.raises(PermissionError):
        await cross_product.deliver(_principal(), recipient_project="project-b", recipient_role="reviewer")


@pytest.mark.asyncio
async def test_ambiguous_product_requires_scope_and_store_sees_one_product():
    store = FakeMessageStore()
    service = _service(
        store,
        projects={"product-a": ("project-b",), "product-b": ("project-b",)},
        grants={
            "host-sender": (
                ("product-a", "project-b", "reviewer"),
                ("product-b", "project-b", "reviewer"),
            )
        },
    )
    principal = _principal(products=("product-a", "product-b"))

    with pytest.raises(PermissionError, match="product scope"):
        await service.list(principal, recipient_project="project-b", recipient_role="reviewer")
    await service.list(principal, recipient_project="project-b", recipient_role="reviewer", product_id="product-b")

    assert store.last_principal_products == ("product-b",)


@pytest.mark.asyncio
async def test_concurrent_logical_fanout_is_atomically_bounded():
    store = FakeMessageStore()
    service = _service(store, policy=DeliveryPolicy(max_payload_bytes=4096, max_hops=3, max_fanout=2))
    envelopes = [_envelope(id=f"m-{idx}", idempotency_key=f"idem-{idx}") for idx in range(3)]

    results = await asyncio.gather(*(service.send(_principal(), item) for item in envelopes), return_exceptions=True)

    assert sum(isinstance(result, Exception) for result in results) == 1
    assert len(store.messages) == 2


@pytest.mark.asyncio
async def test_host_cannot_spoof_root_or_causation_hop():
    store = FakeMessageStore()
    service = _service(store, policy=DeliveryPolicy(max_payload_bytes=4096, max_hops=3, max_fanout=2))
    root = await service.send(_principal(), _envelope(hop_count=99))
    assert store.messages[root.message_id].hop_count == 0

    child = await service.send(
        _principal(), _envelope(id="child", causation_id=root.message_id, hop_count=0, idempotency_key="child-idem")
    )
    assert store.messages[child.message_id].hop_count == 1


def test_jsonb_text_is_decoded_before_domain_envelope():
    from whilly.adapters.db.learning_messages import _envelope as db_envelope

    message = _envelope(sender_id="host-sender")
    row = {
        **message.as_dict(),
        "expires_at": message.expires_at,
        "payload": '{"body":"safe data"}',
        "evidence_refs": '["evidence-1"]',
    }

    decoded = db_envelope(row)

    assert decoded.payload == {"body": "safe data"}
    assert decoded.evidence_refs == ("evidence-1",)
