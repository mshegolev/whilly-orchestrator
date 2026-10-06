"""Descriptor-safe mailbox transport regression tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

from whilly.adapters.filesystem import swarm_mailbox
from whilly.adapters.filesystem.swarm_mailbox import MailboxDirectory, MailboxIntegrityError
from whilly.swarm.mailbox import enqueue
from whilly.swarm.mailbox import sync_mailbox


def _name(letter: str) -> str:
    return f"{letter * 32}.json"


def test_fifo_and_regular_request_use_production_transport_in_bounded_subprocess(tmp_path: Path):
    child = r"""
import asyncio, json, os, pathlib, sys
from whilly.swarm.mailbox import sync_mailbox

class Store:
    def __init__(self):
        self.sent = 0
    async def send_message(self, *_args, **_kwargs):
        self.sent += 1
        return 7
    async def inbox(self, *_args, **_kwargs):
        return []
    async def mark_delivered(self, *_args, **_kwargs):
        return None

async def main(root):
    root = pathlib.Path(root)
    await sync_mailbox(Store(), root, session_id="session", recipient="worker")
    outbox = root / "outbox"
    fifo_name = "a" * 32 + ".json"
    valid_name = "b" * 32 + ".json"
    os.mkfifo(outbox / fifo_name)
    (outbox / valid_name).write_text(json.dumps({"op": "send", "sender": "worker", "recipient": "peer", "body": "ok"}))
    store = Store()
    await sync_mailbox(store, root, session_id="session", recipient="worker")
    await sync_mailbox(store, root, session_id="session", recipient="worker")
    assert store.sent == 1
    assert json.loads((root / "receipts" / fifo_name).read_text())["status"] == "rejected"
    assert json.loads((root / "receipts" / valid_name).read_text()) == {"status": "delivered", "message_id": 7}
    assert (outbox / fifo_name).exists()
    print("production-fifo-and-regular-ok")

asyncio.run(main(sys.argv[1]))
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.stdout.strip() == "production-fifo-and-regular-ok"


def test_actual_tmp_alias_is_accepted_but_arbitrary_parent_symlink_is_rejected(tmp_path: Path):
    actual_tmp_root = Path(tempfile.mkdtemp(prefix="whilly-mailbox-alias-", dir="/tmp"))
    try:
        with MailboxDirectory(Path("/tmp") / actual_tmp_root.name) as mailbox:
            mailbox.write_state("status.json", {"ok": True})
            assert json.loads(mailbox.read_state("status.json")) == {"ok": True}
    finally:
        shutil.rmtree(actual_tmp_root)

    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "user-parent")
    with pytest.raises(MailboxIntegrityError, match="directory"):
        with MailboxDirectory(tmp_path / "user-parent" / "mailbox"):
            pass


async def test_valid_request_gets_durable_receipt(tmp_path: Path):
    store = _store()
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    request_name = _name("e")
    (tmp_path / "outbox" / request_name).write_text(
        json.dumps({"op": "send", "sender": "worker", "recipient": "peer", "body": "hello"}),
        encoding="utf-8",
    )

    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")

    assert json.loads((tmp_path / "receipts" / request_name).read_text()) == {
        "status": "delivered",
        "message_id": 7,
    }


async def test_directory_swap_during_service_call_writes_only_to_opened_directories(tmp_path: Path):
    store = _store()
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    request_name = _name("f")
    (tmp_path / "outbox" / request_name).write_text(
        json.dumps({"op": "send", "sender": "worker", "recipient": "peer", "body": "hello"}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped_outbox = tmp_path / "outbox-opened"
    swapped_receipts = tmp_path / "receipts-opened"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*_args, **_kwargs):
        os.rename(tmp_path / "outbox", swapped_outbox)
        os.rename(tmp_path / "receipts", swapped_receipts)
        (tmp_path / "outbox").mkdir()
        (tmp_path / "receipts").mkdir()
        entered.set()
        await release.wait()
        return 7

    store.send_message.side_effect = send
    task = asyncio.create_task(sync_mailbox(store, tmp_path, session_id="session", recipient="worker"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    release.set()
    await asyncio.wait_for(task, timeout=1)

    assert not (tmp_path / "receipts" / request_name).exists()
    assert (swapped_receipts / request_name).is_file()
    assert (tmp_path / "outbox").is_dir()
    assert (tmp_path / "receipts").is_dir()


async def test_directory_swap_control_keeps_valid_directory_operational(tmp_path: Path):
    store = _store()
    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")
    request_name = _name("1")
    (tmp_path / "outbox" / request_name).write_text(
        json.dumps({"op": "send", "sender": "worker", "recipient": "peer", "body": "hello"}),
        encoding="utf-8",
    )

    await sync_mailbox(store, tmp_path, session_id="session", recipient="worker")

    assert (tmp_path / "receipts" / request_name).is_file()


def test_ancestor_symlink_is_rejected_without_touching_outside(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    os.symlink(outside, tmp_path / "ancestor")

    with pytest.raises(MailboxIntegrityError, match="directory"):
        with MailboxDirectory(tmp_path / "ancestor" / "mailbox"):
            pass

    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_preopen_ancestor_swap_is_rejected_without_touching_outside(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()
    moved = tmp_path / "parent-original"
    parent.rename(moved)
    os.symlink(outside, parent)

    with pytest.raises(MailboxIntegrityError, match="directory"):
        with MailboxDirectory(parent / "mailbox"):
            pass

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not (outside / "mailbox").exists()


def test_junk_entries_count_toward_bounded_backlog(tmp_path: Path):
    outbox = tmp_path / "outbox"
    outbox.mkdir(parents=True)
    for index in range(100):
        (outbox / f"junk-{index:03d}").write_text("junk", encoding="utf-8")

    with MailboxDirectory(tmp_path) as mailbox:
        assert mailbox.request_names(100) == ()
        assert mailbox.entry_count(100) == 100
    with pytest.raises(ValueError, match="backlog"):
        enqueue(tmp_path, {"op": "send"})


def test_failed_atomic_write_removes_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with MailboxDirectory(tmp_path) as mailbox:
        monkeypatch.setattr(swarm_mailbox.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("fsync")))
        with pytest.raises(OSError, match="fsync"):
            mailbox.write_state("status.json", {"state": "writing"})
    assert list(tmp_path.glob(".status.json.*.tmp")) == []


def _store():
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.inbox.return_value = []
    store.send_message.return_value = 7
    return store
