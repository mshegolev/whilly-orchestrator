from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.proposals import ProposalResult, TaskProposal


def _proposal(**changes) -> TaskProposal:
    values = {
        "id": "proposal-1",
        "origin_feature_id": "feature-1",
        "origin_task_id": None,
        "target_project": "project-b",
        "target_module": "src/module.py",
        "evidence_refs": ("message:evidence-1",),
        "outcome": "Improve the module",
        "contract_impact": "none",
        "acceptance": ("unit test passes",),
        "dependencies": (),
        "resource_class": "small",
    }
    values.update(changes)
    return TaskProposal(**values)


class MemoryAdmissionStore:
    def __init__(self, *, snapshot=None):
        self.proposal = _proposal()
        self.snapshot = snapshot or {"feature_revision": 3}
        self.accept_calls = 0
        self.reject_calls = 0

    async def validate_origin(self, product_id, feature_id, task_id):
        return None

    async def persist(self, product_id, actor_id, proposal):
        self.proposal = proposal
        return ProposalResult(proposal.id, "proposed")

    async def get(self, product_id, proposal_id):
        return {
            **self.proposal.__dict__,
            "id": proposal_id,
            "product_id": product_id,
            "status": "awaiting_approval",
        }

    async def inspect_admission(self, product_id, proposal_id, *, registry, owner):
        return dict(self.snapshot)

    async def accept_for_planning(self, product_id, proposal_id, *, actor_id, expected_revision, reason, registry):
        self.accept_calls += 1
        return ProposalResult(proposal_id, "awaiting_approval", feature_revision=expected_revision, reason=reason)

    async def reject(self, product_id, proposal_id, *, actor_id, reason):
        self.reject_calls += 1
        return ProposalResult(proposal_id, "rejected", reason=reason)


def _principal(actor="owner"):
    return Principal(actor, ("default",), ("project-b",), ("internal",))


def test_evaluate_returns_named_blockers_and_is_read_only():
    async def scenario():
        store = MemoryAdmissionStore(
            snapshot={
                "feature_revision": 3,
                "protected": True,
                "unknown_dependencies": ("task:missing",),
                "budget_exhausted": True,
            }
        )
        from whilly.swarm.learning.proposals import ProposalService

        service = ProposalService(
            store,
            registered_projects={"default": ("project-b",)},
            admission_port=store,
            owner_verified=True,
        )
        result = await service.evaluate(_principal("specialist"), "proposal-1")
        assert result.status == "blocked"
        assert result.blockers == (
            "protected_module_path",
            "dependency_unknown",
            "feature_call_budget_exhausted",
        )
        assert store.accept_calls == 0

    asyncio.run(scenario())


def test_specialist_cannot_accept_and_accept_is_not_execution():
    async def scenario():
        store = MemoryAdmissionStore()
        from whilly.swarm.learning.proposals import ProposalService

        service = ProposalService(
            store,
            registered_projects={"default": ("project-b",)},
            admission_port=store,
            owner_verified=True,
            actor_id="owner",
        )
        with pytest.raises(PermissionError, match="host owner"):
            await service.accept_for_planning(
                _principal("specialist"), "proposal-1", expected_revision=3, reason="reviewed"
            )
        accepted = await service.accept_for_planning(
            _principal("owner"), "proposal-1", expected_revision=3, reason="reviewed"
        )
        assert accepted.status == "awaiting_approval"
        assert store.accept_calls == 1

    asyncio.run(scenario())


def test_reject_requires_bounded_nonblank_reason():
    async def scenario():
        store = MemoryAdmissionStore()
        from whilly.swarm.learning.proposals import ProposalService

        service = ProposalService(
            store, registered_projects={"default": ("project-b",)}, admission_port=store, owner_verified=True
        )
        with pytest.raises(ValueError, match="reason"):
            await service.reject(_principal(), "proposal-1", reason=" ")
        with pytest.raises(ValueError, match="reason"):
            await service.reject(_principal(), "proposal-1", reason="x" * 257)

    asyncio.run(scenario())


def test_factory_reloads_registry_and_defaults_principal_scope(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"version": 1, "name": "demo", "projects": {}, "roles": {}}))
    loaded = []

    class Registry:
        projects = {"project-b": object()}
        raw = {}

    def load(path, *, check_git=True):
        loaded.append((path, check_git))
        return Registry()

    monkeypatch.setattr("whilly.adapters.db.learning_proposal_admission.load_registry", load)
    from whilly.adapters.db.learning_proposal_admission import build_proposal_service

    service = build_proposal_service(object(), str(registry_path), actor_id="owner", owner=True)
    assert service.registered_projects == {"default": frozenset({"project-b"})}
    assert service.owner_verified is True
    assert loaded == [(str(registry_path), True)]


def test_contract_impact_blocks_even_when_untrusted_spec_claims_checks_passed():
    async def scenario():
        store = MemoryAdmissionStore(
            snapshot={
                "feature_revision": 3,
                "contract_impact": "public API change",
                "producer_verified": True,
                "consumers_verified": True,
                "measured_checks_verified": True,
            }
        )
        from whilly.swarm.learning.proposals import ProposalService

        service = ProposalService(
            store,
            registered_projects={"default": ("project-b",)},
            admission_port=store,
            owner_verified=True,
        )
        result = await service.evaluate(_principal("specialist"), "proposal-1")
        assert result.status == "blocked"
        assert "contract_verification_proof_type_missing" in result.blockers

    asyncio.run(scenario())


def test_builtin_protected_paths_do_not_depend_on_registry_raw_keys():
    from whilly.adapters.db.learning_proposal_admission import PostgresProposalAdmission

    for path in (
        ".git/config",
        ".agents/board.md",
        ".codex/settings",
        ".claude/skills/x",
        "AGENTS.md",
        "src/secrets.yml",
        "ci/policy.yml",
    ):
        assert PostgresProposalAdmission._path_blockers(path, type("Registry", (), {"raw": {}})()) == [
            "protected_module_path"
        ]


def test_reject_does_not_mutate_foreign_target_even_for_owner():
    async def scenario():
        store = MemoryAdmissionStore()
        store.proposal = _proposal(target_project="foreign-project")
        from whilly.swarm.learning.proposals import ProposalService

        service = ProposalService(
            store,
            registered_projects={"default": ("project-b",)},
            admission_port=store,
            owner_verified=True,
            actor_id="owner",
        )
        with pytest.raises(PermissionError, match="not registered"):
            await service.reject(_principal(), "proposal-1", reason="out of scope")
        assert store.reject_calls == 0

    asyncio.run(scenario())
