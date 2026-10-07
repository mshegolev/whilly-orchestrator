"""Stage delivery and acceptance boundaries for merged product changes."""

from __future__ import annotations

import pytest

from tests.unit.test_product_merge import setup as merge_setup
from whilly.swarm.change_set import ChangeSetStatus
from whilly.swarm.product_delivery import (
    ArtifactDigest,
    DeliveryBoundaryError,
    ProductDeliveryCoordinator,
    StageDeploymentReceipt,
)
from whilly.swarm.product_merge import ProductMergeCoordinator


class DeliveryPort:
    def __init__(self):
        self.deliveries = []
        self.observations = []
        self.stale_repo = None
        self.unavailable_repo = None
        self.prod_calls = []

    async def deliver_stage(self, change_id, policy, source_sha, artifacts):
        self.deliveries.append(policy.project_id)
        deployed = "0" * 40 if policy.project_id == self.stale_repo else source_sha
        return StageDeploymentReceipt(
            change_id,
            policy.project_id,
            deployed,
            tuple(artifacts),
            f"stage:{policy.project_id}:1",
        )

    async def observe_stage(self, change_id, policy, source_sha, deployment):
        self.observations.append(policy.project_id)
        if policy.project_id == self.unavailable_repo:
            return {"deployed_sha": source_sha, "observations": {"smoke-stage": "unavailable"}}
        return {"deployed_sha": deployment.deployed_sha, "observations": {"smoke-stage": "passed"}}

    async def deliver_prod(self, *args):
        self.prod_calls.append(args)
        return {"status": "unexpected"}


async def merged_setup(tmp_path):
    snapshot, store, transport, receipts = merge_setup(tmp_path)
    await ProductMergeCoordinator(store, snapshot, transport).merge_all("change-demo")
    artifacts = {
        repo_id: (ArtifactDigest("package", "package", "sha256:" + str(index) * 64),)
        for index, repo_id in enumerate(receipts, 1)
    }
    return snapshot, store, artifacts


async def test_stage_delivery_requires_every_declared_immutable_artifact(tmp_path):
    snapshot, store, artifacts = await merged_setup(tmp_path)
    port = DeliveryPort()
    del artifacts["repo-2"]
    with pytest.raises(DeliveryBoundaryError, match="artifact_set_incomplete"):
        await ProductDeliveryCoordinator(store, snapshot, port).deliver_stage("change-demo", artifacts)
    assert port.deliveries == []
    assert store.change.status == ChangeSetStatus.MERGED


async def test_stage_delivery_acceptance_is_exact_sha_and_idempotent(tmp_path):
    snapshot, store, artifacts = await merged_setup(tmp_path)
    port = DeliveryPort()
    coordinator = ProductDeliveryCoordinator(store, snapshot, port)
    result = await coordinator.deliver_stage("change-demo", artifacts)
    assert result.status == ChangeSetStatus.DONE
    assert port.deliveries == ["repo-1", "repo-2"]
    assert port.observations == ["repo-1", "repo-2"]
    replay = await coordinator.deliver_stage("change-demo", artifacts)
    assert replay.status == ChangeSetStatus.DONE
    assert port.deliveries == ["repo-1", "repo-2"]


@pytest.mark.parametrize("defect", ["stale_sha", "unavailable_probe"])
async def test_stage_failure_enters_compensation_and_never_done(tmp_path, defect):
    snapshot, store, artifacts = await merged_setup(tmp_path)
    port = DeliveryPort()
    if defect == "stale_sha":
        port.stale_repo = "repo-2"
    else:
        port.unavailable_repo = "repo-2"
    result = await ProductDeliveryCoordinator(store, snapshot, port).deliver_stage("change-demo", artifacts)
    assert result.status == ChangeSetStatus.ROLLING_BACK


async def test_prod_delivery_requires_separate_matching_approval(tmp_path):
    snapshot, store, artifacts = await merged_setup(tmp_path)
    port = DeliveryPort()
    coordinator = ProductDeliveryCoordinator(store, snapshot, port)
    with pytest.raises(DeliveryBoundaryError, match="prod_approval_required"):
        await coordinator.deliver_prod("change-demo", artifacts, approval=None)
    await coordinator.deliver_stage("change-demo", artifacts)
    with pytest.raises(DeliveryBoundaryError, match="prod_approval_changed"):
        await coordinator.deliver_prod("change-demo", artifacts, approval="a" * 64)
    assert port.prod_calls == []


async def test_prod_delivery_replay_uses_durable_receipt(tmp_path):
    snapshot, store, artifacts = await merged_setup(tmp_path)
    port = DeliveryPort()
    coordinator = ProductDeliveryCoordinator(store, snapshot, port)
    done = await coordinator.deliver_stage("change-demo", artifacts)
    approval = coordinator.prod_approval_digest(done)
    first = await coordinator.deliver_prod("change-demo", artifacts, approval=approval)
    second = await coordinator.deliver_prod("change-demo", artifacts, approval=approval)
    assert first == second == {"status": "unexpected"}
    assert len(port.prod_calls) == 1


async def test_registry_change_before_stage_effect_fails_closed(tmp_path):
    import json
    from pathlib import Path

    snapshot, store, artifacts = await merged_setup(tmp_path)
    path = Path(snapshot.source_path)
    data = json.loads(path.read_text())
    data["projects"]["repo-1"]["product_policy"]["delivery"]["stage"]["job"] = "changed"
    path.write_text(json.dumps(data))
    port = DeliveryPort()
    result = await ProductDeliveryCoordinator(store, snapshot, port).deliver_stage("change-demo", artifacts)
    assert result.status == ChangeSetStatus.ROLLING_BACK
    assert port.deliveries == [] and port.observations == []
