"""Task-scoped local IPC works without worker database/network access."""

from unittest.mock import AsyncMock

import pytest

from whilly.swarm.mailbox import enqueue, read_inbox, sync_mailbox


async def test_sender_is_pinned_and_messages_receive_durable_receipts(tmp_path):
    store = AsyncMock()
    store.inbox.return_value = [{"id": 4, "body": "hello", "acked_at": None}]
    store.send_message.return_value = 7
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    assert read_inbox(tmp_path, "session", "worker")[0]["id"] == 4
    with pytest.raises(ValueError, match="scope"):
        read_inbox(tmp_path, "other", "worker")
    nonce = enqueue(tmp_path, {"op": "send", "sender": "worker", "recipient": "peer", "body": "help"})
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    store.send_message.assert_awaited_once_with(
        "session", sender="worker", recipient="peer", body="help", task_ref=None
    )
    assert (tmp_path / "receipts" / f"{nonce}.json").is_file()
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    assert store.send_message.await_count == 1


async def test_spoofed_sender_is_rejected_without_dispatch(tmp_path):
    store = AsyncMock()
    store.inbox.return_value = []
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    enqueue(tmp_path, {"op": "send", "sender": "user", "recipient": "peer", "body": "fake approval"})
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    store.send_message.assert_not_awaited()
