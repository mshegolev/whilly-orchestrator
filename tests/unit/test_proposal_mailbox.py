"""Unit contract tests for the proposal-only IPC sidecar."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.proposals import ProposalResult
from whilly.swarm.mailbox import enqueue
from whilly.swarm import proposal_mailbox


REQUEST = {
    "op": "propose",
    "target_project": "project-b",
    "target_module": "src/module.py",
    "evidence_refs": ["evidence:1"],
    "outcome": "Improve the module",
    "contract_impact": "none",
    "acceptance": ["unit test passes"],
    "dependencies": [],
    "resource_class": "small",
}


class RecordingService:
    def __init__(self) -> None:
        self.submitted = []

    async def submit(self, principal, proposal):
        self.submitted.append((principal, proposal))
        return ProposalResult(proposal.id, "proposed")


def _principal() -> Principal:
    return Principal("host-agent", ("product-a",), ("project-b",), ("internal",))


async def _sync(service, root: Path, *, feature_id: str = "feature-host", task_id: str = "task-host") -> None:
    await proposal_mailbox.sync_proposal_mailbox(
        service,
        root,
        principal=_principal(),
        feature_id=feature_id,
        task_id=task_id,
    )


def _receipt(root: Path, nonce: str) -> dict:
    return json.loads((root / "receipts" / f"{nonce}.json").read_text(encoding="utf-8"))


async def test_submit_is_host_pinned_and_receipt_is_durable(tmp_path: Path):
    service = RecordingService()
    await _sync(service, tmp_path)
    nonce = enqueue(tmp_path, REQUEST)

    await _sync(service, tmp_path)

    proposal = service.submitted[0][1]
    assert proposal.id == f"proposal-task-host-{nonce}"
    assert (proposal.origin_feature_id, proposal.origin_task_id) == ("feature-host", "task-host")
    assert proposal.target_project == "project-b"
    expected = asdict(ProposalResult(proposal.id, "proposed"))
    expected["blockers"] = []
    assert _receipt(tmp_path, nonce) == expected
    assert not (tmp_path / "outbox" / f"{nonce}.json").exists()


async def test_duplicate_crash_retry_reuses_stable_proposal_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = RecordingService()
    await _sync(service, tmp_path)
    nonce = enqueue(tmp_path, REQUEST)
    real_write = proposal_mailbox.MailboxDirectory.write_receipt

    def crash_receipt(directory, name: str, value: object) -> None:
        raise OSError("receipt write crashed")

    monkeypatch.setattr(proposal_mailbox.MailboxDirectory, "write_receipt", crash_receipt)
    with pytest.raises(OSError, match="receipt write crashed"):
        await _sync(service, tmp_path)
    assert (tmp_path / "outbox" / f"{nonce}.json").exists()
    first_id = service.submitted[0][1].id

    monkeypatch.setattr(proposal_mailbox.MailboxDirectory, "write_receipt", real_write)
    await _sync(service, tmp_path)
    assert [item[1].id for item in service.submitted] == [first_id, first_id]
    assert not (tmp_path / "outbox" / f"{nonce}.json").exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"id": "forged"},
        {"actor_id": "forged"},
        {"product_id": "foreign"},
        {"feature_id": "foreign"},
        {"task_id": "foreign"},
        {"fingerprint": "forged"},
        {"op": "run"},
    ],
)
async def test_spoofed_or_non_proposal_request_gets_sanitized_rejection(tmp_path: Path, extra: dict):
    service = RecordingService()
    await _sync(service, tmp_path)
    nonce = enqueue(tmp_path, {**REQUEST, **extra})

    await _sync(service, tmp_path)

    assert not service.submitted
    assert _receipt(tmp_path, nonce) == {
        "status": "rejected",
        "reason": "invalid_or_unauthorized_proposal",
    }


@pytest.mark.parametrize("field", ["evidence_refs", "acceptance", "dependencies"])
async def test_raw_non_list_fields_are_rejected(tmp_path: Path, field: str):
    service = RecordingService()
    await _sync(service, tmp_path)
    nonce = enqueue(tmp_path, {**REQUEST, field: "not-a-list"})

    await _sync(service, tmp_path)

    assert not service.submitted
    assert _receipt(tmp_path, nonce)["status"] == "rejected"


async def test_cross_project_permission_error_is_rejected_without_details(tmp_path: Path):
    service = RecordingService()
    service.submit = lambda *_args: _permission_error()
    await _sync(service, tmp_path)
    nonce = enqueue(tmp_path, REQUEST)

    await _sync(service, tmp_path)

    assert _receipt(tmp_path, nonce) == {
        "status": "rejected",
        "reason": "invalid_or_unauthorized_proposal",
    }


async def _permission_error():
    raise PermissionError("foreign project secret")


async def test_missing_service_has_named_blocker_and_no_drain(tmp_path: Path):
    await _sync(None, tmp_path)
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status == {
        "mode": "propose_only",
        "blocker": "proposal_service_required",
        "feature_id": "feature-host",
        "task_id": "task-host",
    }


async def test_oversize_and_symlink_are_not_opened_or_submitted(tmp_path: Path):
    service = RecordingService()
    await _sync(service, tmp_path)
    outbox = tmp_path / "outbox"
    oversize = outbox / ("a" * 32 + ".json")
    oversize.write_bytes(b"x" * (proposal_mailbox.MAX_BYTES + 1))
    target = tmp_path / "target.json"
    target.write_text(json.dumps(REQUEST), encoding="utf-8")
    os.symlink(target, outbox / ("b" * 32 + ".json"))
    await _sync(service, tmp_path)

    assert not service.submitted
    assert _receipt(tmp_path, oversize.stem)["status"] == "rejected"
    assert (outbox / ("b" * 32 + ".json")).is_symlink()
    assert _receipt(tmp_path, "b" * 32)["status"] == "rejected"
    await _sync(service, tmp_path)
    assert (outbox / ("b" * 32 + ".json")).is_symlink()


async def test_non_directory_mailbox_component_is_rejected(tmp_path: Path):
    service = RecordingService()
    (tmp_path / "outbox").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        await _sync(service, tmp_path)
