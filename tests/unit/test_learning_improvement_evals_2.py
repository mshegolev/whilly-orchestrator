from __future__ import annotations

import pytest

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.experiments import (
    DecisionBlocked,
    EvaluationReport,
    ExportPolicy,
    ExperimentDecisionService,
    Metric,
)


def _report() -> EvaluationReport:
    return EvaluationReport(
        experiment_id="exp-1",
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        metrics={"correctness_rate": Metric(1.0, 4)},
        unknown_cost_count=0,
        recommendation="candidate",
        manifest={"id": "exp-1", "policy_version": "p1"},
    )


class MemoryDecisionStore:
    def __init__(self) -> None:
        self.report = None
        self.decisions = []

    async def save_evaluation(self, report: EvaluationReport, *, proposer_id: str, policy_version: str) -> None:
        self.report = report
        self.proposer_id = proposer_id
        self.policy_version = policy_version

    async def save_decision(self, receipt) -> None:
        self.decisions.append(receipt)


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def write(self, report: EvaluationReport, policy: ExportPolicy):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return "external-ref"


@pytest.mark.asyncio
async def test_export_disabled_no_network() -> None:
    sink = RecordingSink()
    service = ExperimentDecisionService(MemoryDecisionStore(), sink)

    receipt = await service.persist_and_export(
        _report(), ExportPolicy(enabled=False, include_raw=False), proposer_id="agent-1", policy_version="p1"
    )

    assert receipt.outcome == "local_only"
    assert sink.calls == 0


@pytest.mark.asyncio
async def test_agent_cannot_accept_own_change() -> None:
    service = ExperimentDecisionService(MemoryDecisionStore(), RecordingSink())
    agent = Principal("agent-1", ("default",), ("project-a",), ("internal",))

    with pytest.raises(DecisionBlocked, match="owner_required"):
        await service.record_decision(agent, "exp-1", "accept", "looks good", proposer_id="agent-1", owner=False)


@pytest.mark.asyncio
async def test_policy_change_requires_fresh_owner_decision() -> None:
    service = ExperimentDecisionService(MemoryDecisionStore(), RecordingSink())
    owner = Principal("owner-1", ("default",), ("project-a",), ("internal",))

    with pytest.raises(DecisionBlocked, match="policy_version_changed"):
        await service.record_decision(
            owner,
            "exp-1",
            "accept",
            "bounded improvement",
            proposer_id="agent-1",
            owner=True,
            approved_policy_version="p1",
            current_policy_version="p2",
        )


@pytest.mark.asyncio
async def test_failed_export_preserves_local_result() -> None:
    store = MemoryDecisionStore()
    receipt = await ExperimentDecisionService(store, RecordingSink(fail=True)).persist_and_export(
        _report(), ExportPolicy(enabled=True, include_raw=False), proposer_id="agent-1", policy_version="p1"
    )

    assert store.report == _report()
    assert store.proposer_id == "agent-1"
    assert store.policy_version == "p1"
    assert receipt.outcome == "export_failed"
    assert receipt.blocker == "optional_export_unavailable"


@pytest.mark.asyncio
async def test_rollback_does_not_bypass_permissions() -> None:
    service = ExperimentDecisionService(MemoryDecisionStore(), RecordingSink())
    owner = Principal("owner-1", ("default",), ("project-a",), ("internal",))

    receipt = await service.record_decision(
        owner,
        "exp-1",
        "rollback",
        "candidate regressed",
        proposer_id="agent-1",
        owner=True,
        rollback_artifact="sha256:baseline",
    )

    assert receipt.rollback_reference == "sha256:baseline"
    assert receipt.execution_authorized is False
