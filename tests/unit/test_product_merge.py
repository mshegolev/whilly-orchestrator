"""Product-wide merge barrier contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.test_product_registry import registry, write
from whilly.swarm.change_set import (
    ChangeSetStatus, Evidence, EvidenceOutcome, ProductChangeSet, RepoChange, RepoChangeStatus, canonical_digest, thaw,
)
from whilly.swarm.gitlab_change_transport import PipelineReceipt, RepoPublicationReceipt
from whilly.swarm.product_merge import MergeBarrierError, ProductMergeCoordinator
from whilly.swarm.product_registry import load_product_registry


class Store:
    def __init__(self, change):
        self.change = change

    async def get(self, change_id):
        return self.change if change_id == self.change.change_id else None


class Transport:
    def __init__(self, receipts, *, targets=None, pipelines=None):
        self.receipts = receipts
        self.targets = targets or {repo_id: receipt.target_sha for repo_id, receipt in receipts.items()}
        self.pipelines = pipelines or {repo_id: receipt.pipeline for repo_id, receipt in receipts.items()}
        self.calls = []

    async def read_merge_request(self, policy, receipt):
        self.calls.append(("mr", policy.project_id, receipt.source_sha))
        return receipt

    async def read_target_sha(self, policy):
        self.calls.append(("target", policy.project_id))
        return self.targets[policy.project_id]

    async def read_exact_pipeline(self, project_id, sha, *, required_jobs=()):
        self.calls.append(("pipeline", project_id, sha, tuple(required_jobs)))
        repo_id = next(repo for repo, receipt in self.receipts.items() if receipt.pipeline.project_id == project_id)
        return self.pipelines[repo_id]


def setup(tmp_path, count=2):
    data = registry(count)
    snapshot = load_product_registry(write(tmp_path, data))
    bases = {repo_id: chr(96 + index) * 40 for index, repo_id in enumerate(snapshot.projects, 1)}
    change = ProductChangeSet.create(
        change_id="change-demo",
        product_id="demo",
        goal="merge all components",
        acceptance_criteria=("all exact SHA pipelines pass",),
        registry_snapshot=snapshot.to_dict(),
        base_shas=bases,
        approval_digest="d" * 64,
    )
    receipts = {}
    repos = []
    for index, repo in enumerate(change.repo_changes, 1):
        source = chr(100 + index) * 40
        policy = snapshot.projects[repo.repo_id]
        pipeline = PipelineReceipt(policy.gitlab_project_id, source, 100 + index, "success", (
            ("test", "success", 200 + index), ("build", "success", 300 + index),
        ))
        receipt = RepoPublicationReceipt(
            change.change_id, repo.repo_id, source, repo.base_sha, f"whilly/{change.change_id}", index,
            f"https://gitlab.example.com/demo/repo-{index}/-/merge_requests/{index}", pipeline,
            snapshot.registry_digest, snapshot.policy_digest, change.approval_digest,
        )
        receipts[repo.repo_id] = receipt
        evidence = Evidence("exact_sha_pipeline", EvidenceOutcome.PASSED, sha=source,
                            job_id=f"pipeline:{pipeline.pipeline_id}", details=receipt.to_dict())
        repos.append(RepoChange(repo.repo_id, repo.base_sha, RepoChangeStatus.PIPELINE_GREEN, 7,
                                last_evidence=evidence))
    change = replace(change, status=ChangeSetStatus.READY_TO_MERGE, version=8, repo_changes=tuple(repos))
    return snapshot, Store(change), Transport(receipts), receipts


async def test_barrier_requires_every_repo_and_returns_dependency_order(tmp_path):
    snapshot, store, transport, receipts = setup(tmp_path)
    barrier = await ProductMergeCoordinator(store, snapshot, transport).verify_barrier("change-demo")
    assert [repo.repo_id for repo in barrier.repositories] == ["repo-1", "repo-2"]
    assert [repo.source_sha for repo in barrier.repositories] == [receipts[r].source_sha for r in ("repo-1", "repo-2")]
    assert barrier.approval_digest == "d" * 64
    assert len([call for call in transport.calls if call[0] == "pipeline"]) == 2


@pytest.mark.parametrize("defect", ["missing_repo", "old_pipeline", "target_drift", "approval", "registry", "jobs"])
async def test_barrier_fails_closed_for_incomplete_or_stale_product_evidence(tmp_path, defect):
    snapshot, store, transport, receipts = setup(tmp_path)
    if defect == "missing_repo":
        missing = replace(store.change.repo_changes[1], last_evidence=None)
        store.change = replace(store.change, repo_changes=(store.change.repo_changes[0], missing))
    elif defect == "old_pipeline":
        receipt = receipts["repo-2"]
        transport.pipelines["repo-2"] = replace(receipt.pipeline, sha="c" * 40)
    elif defect == "target_drift":
        transport.targets["repo-2"] = "f" * 40
    elif defect == "approval":
        store.change = replace(store.change, approval_digest="a" * 64)
    elif defect == "registry":
        changed = thaw(store.change.registry_snapshot)
        changed["name"] = "different product"
        store.change = replace(store.change, registry_snapshot=changed, registry_digest=canonical_digest(changed))
    else:
        receipt = receipts["repo-2"]
        transport.pipelines["repo-2"] = replace(receipt.pipeline, jobs=(("test", "success", 201),))
    with pytest.raises(MergeBarrierError):
        await ProductMergeCoordinator(store, snapshot, transport).verify_barrier("change-demo")


async def test_barrier_rejects_mr_identity_change(tmp_path):
    snapshot, store, transport, receipts = setup(tmp_path)

    async def changed(_policy, receipt):
        return replace(receipt, source_sha="f" * 40)

    transport.read_merge_request = changed
    with pytest.raises(MergeBarrierError, match="merge_request_identity_changed"):
        await ProductMergeCoordinator(store, snapshot, transport).verify_barrier("change-demo")
