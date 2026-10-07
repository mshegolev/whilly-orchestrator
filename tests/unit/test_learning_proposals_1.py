from __future__ import annotations

import asyncio

import pytest

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.proposals import (
    PROPOSAL_STATES,
    ProposalEvent,
    ProposalResult,
    ProposalService,
    TaskProposal,
    can_transition,
)


def _principal(*, products=("product-a",), projects=("project-b",), actor="agent-1") -> Principal:
    return Principal(actor, products, projects, ("internal",))


def _proposal(**changes) -> TaskProposal:
    values = {
        "id": "proposal-1",
        "origin_feature_id": "feature-1",
        "origin_task_id": "task-1",
        "target_project": "project-b",
        "target_module": "src/module.py",
        "evidence_refs": ("message:m-1",),
        "outcome": "Improve the module contract",
        "contract_impact": "none",
        "acceptance": ("unit test passes",),
        "dependencies": ("task:task-1",),
        "resource_class": "small",
    }
    values.update(changes)
    if "fingerprint" not in changes:
        values.pop("fingerprint", None)
    return TaskProposal(**values)


class MemoryProposalStore:
    def __init__(self) -> None:
        self.records: dict[str, tuple[str, TaskProposal, str]] = {}
        self.events: dict[str, list[ProposalEvent]] = {}
        self.persist_calls = 0
        self.validated: list[tuple[str, str, str | None]] = []
        self._lock = asyncio.Lock()

    async def validate_origin(self, product_id, feature_id, task_id):
        self.validated.append((product_id, feature_id, task_id))
        if feature_id != "feature-1" or task_id not in {"task-1", None}:
            raise ValueError("origin does not belong to product")

    async def persist(self, product_id, actor_id, proposal):
        async with self._lock:
            self.persist_calls += 1
            for existing_product, existing, status in self.records.values():
                if (
                    existing_product,
                    existing.target_project,
                    existing.target_module,
                    existing.fingerprint,
                ) == (product_id, proposal.target_project, proposal.target_module, proposal.fingerprint):
                    return ProposalResult(existing.id, status, existing.id, "duplicate proposal")
            if proposal.id in self.records:
                raise ValueError("proposal id already exists with different content")
            self.records[proposal.id] = (product_id, proposal, "proposed")
            self.events[proposal.id] = [
                ProposalEvent(
                    proposal_id=proposal.id,
                    from_state=None,
                    to_state="proposed",
                    actor_id=actor_id,
                    actor_host="test-host",
                    reason=None,
                )
            ]
            return ProposalResult(proposal.id, "proposed")

    async def transition(self, proposal_id, expected_state, to_state, *, actor_id, reason=None):
        product_id, proposal, state = self.records[proposal_id]
        if state != expected_state:
            raise ValueError("proposal state changed")
        if not can_transition(state, to_state):
            raise ValueError("illegal proposal transition")
        self.records[proposal_id] = product_id, proposal, to_state
        self.events[proposal_id].append(ProposalEvent(proposal_id, state, to_state, actor_id, "test-host", reason))

    async def get(self, proposal_id):
        return self.records.get(proposal_id)

    async def list(self, product_id, *, target_project=None):
        return [record for product, record, _ in self.records.values() if product == product_id]

    async def events_for(self, proposal_id):
        return tuple(self.events[proposal_id])


def _service(store=None, *, trusted_service_products=None):
    return ProposalService(
        store or MemoryProposalStore(),
        registered_projects={"product-a": ("project-b",)},
        trusted_service_products=trusted_service_products,
    )


def test_duplicate_submission_one_proposal():
    async def scenario():
        store = MemoryProposalStore()
        service = _service(store)
        first = await service.submit(_principal(), _proposal())
        second = await service.submit(_principal(), _proposal(id="proposal-2"))
        assert first.id == second.id == "proposal-1"
        assert second.duplicate_of == "proposal-1"
        assert store.persist_calls == 2

    asyncio.run(scenario())


def test_unknown_project_rejected():
    async def scenario():
        with pytest.raises(PermissionError, match="registered"):
            await _service().submit(_principal(), _proposal(target_project="unknown"))

    asyncio.run(scenario())


def test_evidence_required():
    with pytest.raises(ValueError, match="evidence_refs"):
        _proposal(evidence_refs=())


def test_proposal_is_not_execution():
    async def scenario():
        result = await _service().submit(_principal(), _proposal())
        assert result.status == "proposed"
        assert result.status not in {"queued", "running", "verified"}

    asyncio.run(scenario())


def test_product_scope_and_host_pinned_origin_are_checked():
    async def scenario():
        store = MemoryProposalStore()
        await _service(store).submit(_principal(), _proposal())
        assert store.validated == [("product-a", "feature-1", "task-1")]
        with pytest.raises(PermissionError, match="exactly one product"):
            await _service().submit(_principal(products=("product-a", "product-b")), _proposal())

    asyncio.run(scenario())


def test_trusted_service_product_is_explicit():
    async def scenario():
        result = await _service(trusted_service_products={"planner": "product-a"}).submit(
            _principal(actor="planner", products=("product-a", "product-b")), _proposal()
        )
        assert result.status == "proposed"

    asyncio.run(scenario())


def test_fingerprint_is_deterministic_and_supplied_value_is_verified():
    proposal = _proposal()
    assert proposal.fingerprint == TaskProposal.content_fingerprint(**proposal.content_dict())
    with pytest.raises(ValueError, match="fingerprint"):
        _proposal(fingerprint="agent-chosen-collision")


def test_target_module_is_relative_and_bounded():
    with pytest.raises(ValueError, match="target_module"):
        _proposal(target_module="../secret.py")
    with pytest.raises(ValueError, match="target_module"):
        _proposal(target_module="/absolute.py")
    with pytest.raises(ValueError, match="target_module"):
        _proposal(target_module="C:/absolute.py")
    with pytest.raises(ValueError, match="target_module"):
        _proposal(target_module="src/\x00module.py")


def test_dependency_references_require_nonempty_typed_suffixes():
    with pytest.raises(ValueError, match="dependencies"):
        _proposal(dependencies=("proposal:",))
    with pytest.raises(ValueError, match="dependencies"):
        _proposal(dependencies=("task:   ",))
    with pytest.raises(ValueError, match="dependencies"):
        _proposal(dependencies=("feature:feature-1",))


def test_lifecycle_has_explicit_legal_transitions_and_immutable_events():
    assert PROPOSAL_STATES == {
        "proposed",
        "triaged",
        "awaiting_approval",
        "eligible",
        "queued",
        "running",
        "verified",
        "blocked",
        "rejected",
        "cancelled",
    }
    assert can_transition("proposed", "triaged")
    assert can_transition("triaged", "awaiting_approval")
    assert can_transition("awaiting_approval", "eligible")
    assert can_transition("eligible", "queued")
    assert can_transition("proposed", "running") is False
    event = ProposalEvent("p", None, "proposed", "actor", "host", None)
    with pytest.raises(Exception):
        event.reason = "mutated"
    with pytest.raises(ValueError, match="reason"):
        ProposalEvent("p", "proposed", "triaged", "actor", "host", "r" * 257)
