"""Coordinator-pinned durable collaboration remains data-only IPC."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.messages import DeliveryPolicy, DeliveryReceipt
from whilly.swarm.mailbox import enqueue, sync_collaboration_mailbox


@pytest.fixture
def service():
    value = AsyncMock()
    value.policy = DeliveryPolicy(4096, 3, 4)
    value.deliver.return_value = []
    value.send.side_effect = lambda principal, envelope: DeliveryReceipt(
        envelope.id,
        "persisted",
        envelope.sender_id,
        envelope.recipient_project,
        envelope.recipient_role,
        envelope.idempotency_key,
    )
    return value


async def sync(service, root):
    await sync_collaboration_mailbox(
        service,
        root,
        principal=Principal("task:trusted", ("default",), ("library",), ("internal",)),
        product_id="default",
        recipient_project="library",
        recipient_role="developer",
        feature_id="feature",
        task_id="trusted",
    )


def request(**extra):
    return {
        "op": "send",
        "recipient_project": "library",
        "recipient_role": "reviewer",
        "kind": "finding",
        "idempotency_key": "stable",
        "correlation_id": "topic",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "payload": {"body": "<script>not executable</script>"},
        "evidence_refs": [],
        **extra,
    }


async def test_host_pins_scope_and_repeat_does_not_resend(service, tmp_path):
    await sync(service, tmp_path)
    nonce = enqueue(tmp_path, request())
    await sync(service, tmp_path)
    envelope = service.send.call_args.args[1]
    assert (envelope.sender_id, envelope.product_id, envelope.feature_id, envelope.task_id) == (
        "task:trusted",
        "default",
        "feature",
        "trusted",
    )
    assert envelope.payload["body"] == "<script>not executable</script>"
    assert json.loads((tmp_path / "receipts" / f"{nonce}.json").read_text())["state"] == "persisted"
    await sync(service, tmp_path)
    assert service.send.await_count == 1


@pytest.mark.parametrize("extra", [{"sender_id": "admin"}, {"product_id": "foreign"}, {"op": "run"}])
async def test_forged_message_cannot_dispatch(service, tmp_path, extra):
    await sync(service, tmp_path)
    nonce = enqueue(tmp_path, request(**extra))
    await sync(service, tmp_path)
    service.send.assert_not_awaited()
    service.ack.assert_not_awaited()
    assert json.loads((tmp_path / "receipts" / f"{nonce}.json").read_text())["state"] == "rejected"


async def test_disabled_policy_does_not_deliver(service, tmp_path):
    service.policy = None
    await sync(service, tmp_path)
    service.deliver.assert_not_awaited()
    assert json.loads((tmp_path / "inbox.json").read_text())["blocker"] == "message_delivery_policy_required"


async def test_ack_scope_is_host_pinned(service, tmp_path):
    await sync(service, tmp_path)
    service.ack.return_value = DeliveryReceipt("message", "acknowledged", "peer", "library", "developer", "key")
    nonce = enqueue(tmp_path, {"op": "ack", "message_id": "message"})
    await sync(service, tmp_path)
    assert service.ack.call_args.kwargs == {
        "product_id": "default",
        "recipient_project": "library",
        "recipient_role": "developer",
    }
    assert json.loads((tmp_path / "receipts" / f"{nonce}.json").read_text()) == asdict(service.ack.return_value)


async def test_legacy_coordinator_has_no_learning_side_effects(monkeypatch, tmp_path):
    from whilly.swarm import runtime

    legacy = AsyncMock()
    monkeypatch.setattr(runtime, "sync_mailbox", legacy)
    coordinator = runtime.Coordinator(SimpleNamespace(store=object(), repo=object()), "session")
    await coordinator._sync_task_mailbox(tmp_path, {"local_id": "local"}, "task")
    legacy.assert_awaited_once()
    assert not (tmp_path / "collaboration").exists()


async def test_crash_before_receipt_keeps_stable_retry_request(service, tmp_path, monkeypatch):
    from whilly.swarm import mailbox

    await sync(service, tmp_path)
    nonce = enqueue(tmp_path, request())
    real_write = mailbox.MailboxDirectory.write_receipt

    def fail_receipt(directory, name, value):
        raise OSError("simulated local receipt crash")

    monkeypatch.setattr(mailbox.MailboxDirectory, "write_receipt", fail_receipt)
    with pytest.raises(OSError, match="receipt crash"):
        await sync(service, tmp_path)
    first = service.send.call_args.args[1]
    assert (tmp_path / "outbox" / f"{nonce}.json").exists()
    monkeypatch.setattr(mailbox.MailboxDirectory, "write_receipt", real_write)
    await sync(service, tmp_path)
    retry = service.send.call_args.args[1]
    assert retry.id == first.id and retry.fingerprint() == first.fingerprint()
    assert not (tmp_path / "outbox" / f"{nonce}.json").exists()


async def test_special_request_remains_for_inspection_on_repeat_tick(service, tmp_path):
    await sync(service, tmp_path)
    target = tmp_path / "target.json"
    target.write_text("payload", encoding="utf-8")
    nonce = "a" * 32
    (tmp_path / "outbox" / f"{nonce}.json").symlink_to(target)

    await sync(service, tmp_path)
    await sync(service, tmp_path)

    assert (tmp_path / "outbox" / f"{nonce}.json").is_symlink()
