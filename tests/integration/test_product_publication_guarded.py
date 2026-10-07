"""Hermetic publication composition: transport calls are fake and sockets forbidden."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_gitlab_change_transport import SHA, TARGET, request, transport
from tests.unit.test_product_registry import registry, write
from whilly.swarm.change_set import Evidence, EvidenceOutcome, RepoChangeStatus
from whilly.swarm.product_publication import ProductPublicationBackend, publish_feature
from whilly.swarm.product_registry import load_product_registry
from whilly.swarm.product_workflow import WorkflowBlocked


class Store:
    def __init__(self, change):
        self.change, self.receipts = change, {}

    async def get(self, change_id):
        return self.change if change_id == self.change.change_id else None

    async def get_effect(self, key):
        return self.receipts.get(key)

    async def record_effect(self, receipt):
        self.receipts.setdefault(receipt.effect_key, receipt)
        return self.receipts[receipt.effect_key]

    async def transition_repo(self, change_id, repo_id, version, target, evidence):
        repo = self.change.repo_changes[0]
        assert repo.version == version
        repo = repo.transition(target, evidence)
        self.change = replace(self.change, repo_changes=(repo,))
        return repo


def setup(tmp_path):
    from whilly.swarm.change_set import ChangeSetStatus, ProductChangeSet, RepoChange

    data = registry(1)
    data["projects"]["demo"] = data["projects"].pop("repo-1")
    data["roles"]["implementer"]["projects"] = ["demo"]
    data["projects"]["demo"]["product_policy"]["gitlab_project_id"] = 17
    data["projects"]["demo"]["product_policy"]["canonical_remote"] = "https://gitlab.example.com/demo/repo.git"
    snapshot = load_product_registry(write(tmp_path, data))
    value = ProductChangeSet.create(
        change_id="change-demo",
        product_id="demo",
        goal="publish fixture",
        acceptance_criteria=("exact SHA CI",),
        registry_snapshot=snapshot.to_dict(),
        base_shas={"demo": TARGET},
        dependencies={"demo": ()},
        approval_digest="d" * 64,
    )
    evidence = Evidence("local_checks", EvidenceOutcome.PASSED, SHA, ("pytest", "-q"), exit_code=0)
    value = replace(
        value,
        status=ChangeSetStatus.VERIFYING_REPOS,
        repo_changes=(RepoChange("demo", TARGET, RepoChangeStatus.LOCAL_VERIFIED, 4, last_evidence=evidence),),
    )
    store = Store(value)
    adapter, api = transport()
    change = replace(request(), registry_digest=snapshot.registry_digest, policy_digest=snapshot.policy_digest)
    backend = ProductPublicationBackend(snapshot, store, adapter)
    return backend, store, adapter, api, change


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("real network forbidden in publication acceptance")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


async def test_guarded_backend_persists_mr_pipeline_and_never_product_success(tmp_path):
    backend, store, adapter, api, change = setup(tmp_path)
    receipt = await backend.prepare_repo_change(change)
    assert receipt.status == "pipeline_green"
    assert store.change.repo_changes[0].status == RepoChangeStatus.PIPELINE_GREEN
    assert store.change.status.value == "VERIFYING_REPOS"
    assert store.change.repo_changes[0].last_evidence.details["mr_iid"] == 7
    assert store.change.repo_changes[0].last_evidence.details["pipeline"]["pipeline_id"] == 31
    assert len(store.receipts) >= 2
    # Resume after rebuilding the transport: durable effects, no second push/MR.
    restarted, _ = transport()
    resumed = ProductPublicationBackend(backend.snapshot, store, restarted)
    assert await resumed.prepare_repo_change(change) == receipt
    assert restarted.git.pushes == []
    assert not any(method in {"PUT", "DELETE"} for method, _ in api.calls)


@pytest.mark.parametrize("mutation", ["approval", "registry", "sha", "version", "status"])
async def test_current_change_binding_blocks_every_effect(tmp_path, mutation):
    backend, store, adapter, api, change = setup(tmp_path)
    if mutation == "approval":
        store.change = replace(store.change, approval_digest="a" * 64)
    elif mutation == "registry":
        import json
        from pathlib import Path

        path = Path(backend.snapshot.source_path)
        data = json.loads(path.read_text())
        data["projects"]["demo"]["product_policy"]["checks"]["ci"] = ["different-check"]
        path.write_text(json.dumps(data))
    elif mutation == "sha":
        repo = store.change.repo_changes[0]
        store.change = replace(
            store.change, repo_changes=(replace(repo, last_evidence=replace(repo.last_evidence, sha=TARGET)),)
        )
    elif mutation == "version":
        change = replace(change, repo_version=3)
    else:
        store.change = replace(store.change, status="DRAFT")
    with pytest.raises(WorkflowBlocked):
        await backend.prepare_repo_change(change)
    assert adapter.git.pushes == [] and api.calls == []


async def test_default_feature_path_never_invokes_factory_or_reads_registry():
    async def require(feature_id):
        return {"id": feature_id}

    workflow = SimpleNamespace(require=require)
    with pytest.raises(WorkflowBlocked, match="publication_unavailable"):
        await publish_feature(workflow, "feature", transport_factory=lambda: pytest.fail("legacy factory called"))
