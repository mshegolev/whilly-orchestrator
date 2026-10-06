"""Offline full-cycle acceptance for the guarded swarm boundary."""

from __future__ import annotations

import pytest

from tests.integration.test_product_workflow_runtime import planned
from whilly.swarm.product_workflow import ProductWorkflow
from whilly.swarm.runtime import SwarmService

pytest_plugins = ("tests.integration.test_product_workflow_runtime",)
pytestmark = pytest.mark.integration


async def test_local_result_packet_uses_host_evidence_not_worker_receipt(
    db_pool, product_registry, tmp_path, monkeypatch
):
    path, _ = product_registry
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await planned(workflow, path, tmp_path, monkeypatch, description="FORGED_RECEIPT")
    await workflow.products.approve(feature["id"], revision=feature["revision"], digest=feature["approval_digest"])

    result = await workflow.execute(feature["id"], 2)
    assert result["status"] == "review"

    service = SwarmService(db_pool)
    report = await service.report(feature["session_id"])
    attempt = report["attempts"][0]
    packet = {
        "candidate_path": attempt["worktree"],
        "base_sha": attempt["base_sha"],
        "head_sha": attempt["result"]["head_sha"],
        "policy_fingerprints": sorted({item["evidence"]["policy_digest"] for item in attempt["verification"]}),
        "verification": attempt["verification"],
        "reviewer_identity": "reviewer-cheap",
    }

    assert packet["candidate_path"] != "/etc/passwd"
    assert len(packet["base_sha"]) == 40
    assert len(packet["head_sha"]) == 40
    assert packet["head_sha"] != "e" * 40
    assert packet["policy_fingerprints"]
    assert all(item["passed"] for item in packet["verification"])
    assert attempt["review_verdict"] == "approve"
