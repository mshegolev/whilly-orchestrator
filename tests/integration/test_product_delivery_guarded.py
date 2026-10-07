"""End-to-end local product merge-to-stage boundary with external I/O injected."""

from __future__ import annotations

import socket

import pytest

from tests.unit.test_product_delivery import DeliveryPort
from tests.unit.test_product_merge import setup as merge_setup
from whilly.swarm.change_set import ChangeSetStatus, RepoChangeStatus
from whilly.swarm.product_delivery import ArtifactDigest, ProductDeliveryCoordinator
from whilly.swarm.product_merge import ProductMergeCoordinator


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("DNS forbidden"))


async def test_product_reaches_done_only_after_every_stage_observation(tmp_path):
    snapshot, store, merge_transport, receipts = merge_setup(tmp_path)
    merged = await ProductMergeCoordinator(store, snapshot, merge_transport).merge_all("change-demo")
    assert merged.status == ChangeSetStatus.MERGED
    artifacts = {
        repo_id: (ArtifactDigest("package", "package", "sha256:" + str(index) * 64),)
        for index, repo_id in enumerate(receipts, 1)
    }
    port = DeliveryPort()
    done = await ProductDeliveryCoordinator(store, snapshot, port).deliver_stage("change-demo", artifacts)
    assert done.status == ChangeSetStatus.DONE
    assert all(repo.status == RepoChangeStatus.ARTIFACT_READY for repo in done.repo_changes)
    operations = {receipt.operation for receipt in store.effects.values()}
    assert operations == {
        "deliver_stage_intent", "deliver_stage", "accept_stage_intent", "accept_stage",
    }
    assert port.prod_calls == []
