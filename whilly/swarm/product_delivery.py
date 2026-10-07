"""Durable stage delivery and acceptance boundaries for product changes."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from whilly.swarm.change_set import (
    ChangeSetStatus, Evidence, EvidenceOutcome, ExternalEffectReceipt, RepoChangeStatus, canonical_digest, thaw,
)
from whilly.swarm.product_merge import MergeBarrierReceipt
from whilly.swarm.product_registry import ProductRegistrySnapshot


class DeliveryBoundaryError(RuntimeError):
    """A named fail-closed delivery or acceptance boundary."""


@dataclass(frozen=True)
class ArtifactDigest:
    name: str
    kind: str
    digest: str

    def __post_init__(self):
        if (not self.name or not self.kind
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.digest)):
            raise ValueError("artifact_digest_invalid")

    def to_dict(self) -> dict[str, str]:
        return vars(self).copy()


@dataclass(frozen=True)
class StageDeploymentReceipt:
    change_id: str
    repo_id: str
    deployed_sha: str
    artifacts: tuple[ArtifactDigest, ...]
    job_id: str

    def __post_init__(self):
        if (not self.change_id or not self.repo_id or not self.job_id
                or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", self.deployed_sha)
                or not self.artifacts):
            raise ValueError("stage_deployment_receipt_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "artifacts": [item.to_dict() for item in self.artifacts]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StageDeploymentReceipt:
        try:
            return cls(**{**value, "artifacts": tuple(ArtifactDigest(**item) for item in value["artifacts"])})
        except (KeyError, TypeError, ValueError):
            raise DeliveryBoundaryError("stage_deployment_receipt_invalid") from None


class ProductDeliveryCoordinator:
    def __init__(self, store, snapshot: ProductRegistrySnapshot, port):
        self.store = store
        self.snapshot = snapshot
        self.port = port

    @staticmethod
    def _barrier(change) -> MergeBarrierReceipt:
        value = change.last_evidence.details.get("barrier") if change.last_evidence is not None else None
        if not isinstance(value, Mapping):
            raise DeliveryBoundaryError("merge_barrier_receipt_missing")
        return MergeBarrierReceipt.from_dict(value)

    def _validate_artifacts(self, barrier: MergeBarrierReceipt,
                            artifacts: Mapping[str, tuple[ArtifactDigest, ...]]) -> None:
        required_repos = {binding.repo_id for binding in barrier.repositories}
        if set(artifacts) != required_repos:
            raise DeliveryBoundaryError("artifact_set_incomplete")
        for repo_id in required_repos:
            values = artifacts[repo_id]
            if not isinstance(values, tuple) or not values or any(not isinstance(item, ArtifactDigest) for item in values):
                raise DeliveryBoundaryError("artifact_set_invalid")
            expected = {(item["name"], item["kind"], item["digest"])
                        for item in self.snapshot.projects[repo_id].artifacts}
            observed = {(item.name, item.kind, item.digest.split(":", 1)[0]) for item in values}
            if observed != expected:
                raise DeliveryBoundaryError("artifact_set_incomplete")

    async def _effect(self, *, key: str, change_id: str, repo_id: str, operation: str,
                      request: Mapping[str, Any], execute):
        digest = canonical_digest(request)
        previous = await self.store.get_effect(key)
        if previous is not None:
            if previous.request_digest != digest or previous.evidence.outcome != EvidenceOutcome.PASSED:
                raise DeliveryBoundaryError("delivery_effect_requires_reconciliation")
            return thaw(previous.evidence.details)
        intent_key = key + ":intent"
        if await self.store.get_effect(intent_key) is not None:
            raise DeliveryBoundaryError("delivery_effect_requires_reconciliation")
        owner = uuid.uuid4().hex
        recorded = await self.store.record_effect(ExternalEffectReceipt(
            intent_key, change_id, repo_id, operation + "_intent", digest,
            Evidence(operation + "_intent", EvidenceOutcome.UNAVAILABLE,
                     absence="delivery_effect_in_flight", details={"owner": owner}),
        ))
        if recorded.evidence.details.get("owner") != owner:
            raise DeliveryBoundaryError("delivery_effect_requires_reconciliation")
        try:
            result = await execute()
            details = result.to_dict() if isinstance(result, StageDeploymentReceipt) else result
            if not isinstance(details, Mapping):
                raise DeliveryBoundaryError("delivery_response_invalid")
        except Exception:
            await self.store.record_effect(ExternalEffectReceipt(
                key, change_id, repo_id, operation, digest,
                Evidence(operation, EvidenceOutcome.UNAVAILABLE, absence="delivery_effect_requires_reconciliation"),
            ))
            raise DeliveryBoundaryError("delivery_effect_requires_reconciliation") from None
        await self.store.record_effect(ExternalEffectReceipt(
            key, change_id, repo_id, operation, digest,
            Evidence(operation, EvidenceOutcome.PASSED, sha=request["source_sha"],
                     job_id=str(details.get("job_id", operation)), details=details),
        ))
        return thaw(details)

    async def _rollback_boundary(self, change_id: str, barrier: MergeBarrierReceipt, reason: str):
        current = await self.store.get(change_id)
        evidence = Evidence(
            "stage_delivery_failed", EvidenceOutcome.UNAVAILABLE, absence=reason,
            environment="stage", details={"barrier": barrier.to_dict()},
        )
        return await self.store.transition(change_id, current.version, ChangeSetStatus.ROLLING_BACK, evidence)

    async def deliver_stage(self, change_id: str,
                            artifacts: Mapping[str, tuple[ArtifactDigest, ...]]):
        change = await self.store.get(change_id)
        if change is None:
            raise DeliveryBoundaryError("change_set_not_found")
        if change.status == ChangeSetStatus.DONE:
            return change
        if change.status not in {ChangeSetStatus.MERGED, ChangeSetStatus.DEPLOYING_STAGE,
                                 ChangeSetStatus.ACCEPTING_STAGE}:
            raise DeliveryBoundaryError("change_set_not_stage_deliverable")
        barrier = self._barrier(change)
        self._validate_artifacts(barrier, artifacts)
        if change.status == ChangeSetStatus.MERGED:
            evidence = Evidence(
                "stage_delivery_started", EvidenceOutcome.PASSED, sha=barrier.repositories[0].source_sha,
                job_id="stage-delivery", environment="stage", details={"barrier": barrier.to_dict()},
            )
            change = await self.store.transition(
                change_id, change.version, ChangeSetStatus.DEPLOYING_STAGE, evidence
            )
        deployments: dict[str, StageDeploymentReceipt] = {}
        try:
            for binding in barrier.repositories:
                request = {"change_id": change_id, "repo_id": binding.repo_id,
                           "source_sha": binding.source_sha,
                           "artifacts": [item.to_dict() for item in artifacts[binding.repo_id]]}

                async def execute(binding=binding):
                    return await self.port.deliver_stage(
                        change_id, self.snapshot.projects[binding.repo_id], binding.source_sha,
                        artifacts[binding.repo_id],
                    )

                details = await self._effect(
                    key=f"{change_id}:{binding.repo_id}:{binding.source_sha}:deliver-stage",
                    change_id=change_id, repo_id=binding.repo_id, operation="deliver_stage",
                    request=request, execute=execute,
                )
                deployment = StageDeploymentReceipt.from_dict(details)
                if (deployment.change_id != change_id or deployment.repo_id != binding.repo_id
                        or deployment.deployed_sha != binding.source_sha
                        or deployment.artifacts != artifacts[binding.repo_id]):
                    raise DeliveryBoundaryError("stage_deployment_identity_changed")
                deployments[binding.repo_id] = deployment
                current = await self.store.get(change_id)
                repo = next(item for item in current.repo_changes if item.repo_id == binding.repo_id)
                if repo.status == RepoChangeStatus.MERGED:
                    ready = Evidence(
                        "stage_artifact_ready", EvidenceOutcome.PASSED, sha=binding.source_sha,
                        job_id=deployment.job_id, environment="stage",
                        details={"barrier": barrier.to_dict(), "deployment": deployment.to_dict()},
                    )
                    await self.store.transition_repo(
                        change_id, repo.repo_id, repo.version, RepoChangeStatus.ARTIFACT_READY, ready
                    )
            current = await self.store.get(change_id)
            if current.status == ChangeSetStatus.DEPLOYING_STAGE:
                accepting = Evidence(
                    "stage_delivery_complete", EvidenceOutcome.PASSED,
                    sha=barrier.repositories[-1].source_sha, job_id="stage-acceptance",
                    environment="stage", details={"barrier": barrier.to_dict()},
                )
                await self.store.transition(
                    change_id, current.version, ChangeSetStatus.ACCEPTING_STAGE, accepting
                )
            for binding in barrier.repositories:
                deployment = deployments[binding.repo_id]
                policy = self.snapshot.projects[binding.repo_id]
                request = {"change_id": change_id, "repo_id": binding.repo_id,
                           "source_sha": binding.source_sha, "job_id": deployment.job_id,
                           "observations": list(policy.delivery["stage"]["observations"])}

                async def observe(binding=binding, deployment=deployment, policy=policy):
                    return await self.port.observe_stage(change_id, policy, binding.source_sha, deployment)

                observed = await self._effect(
                    key=f"{change_id}:{binding.repo_id}:{binding.source_sha}:accept-stage",
                    change_id=change_id, repo_id=binding.repo_id, operation="accept_stage",
                    request=request, execute=observe,
                )
                required = tuple(policy.delivery["stage"]["observations"])
                states = observed.get("observations") if isinstance(observed, Mapping) else None
                if (observed.get("deployed_sha") != binding.source_sha or not isinstance(states, Mapping)
                        or any(states.get(name) != "passed" for name in required)):
                    raise DeliveryBoundaryError("stage_acceptance_failed")
        except DeliveryBoundaryError as exc:
            return await self._rollback_boundary(change_id, barrier, str(exc))
        current = await self.store.get(change_id)
        accepted = Evidence(
            "stage_acceptance", EvidenceOutcome.PASSED, sha=barrier.repositories[-1].source_sha,
            job_id="product-stage-acceptance", environment="stage", details={"barrier": barrier.to_dict()},
        )
        return await self.store.transition(change_id, current.version, ChangeSetStatus.DONE, accepted)

    async def deliver_prod(self, change_id: str, artifacts, *, approval: str | None):
        change = await self.store.get(change_id)
        if approval is None:
            raise DeliveryBoundaryError("prod_approval_required")
        if change is None or change.status != ChangeSetStatus.DONE:
            raise DeliveryBoundaryError("stage_acceptance_required")
        expected = canonical_digest({"change_id": change_id, "stage_acceptance": change.last_evidence.to_dict(),
                                     "boundary": "prod"})
        if approval != expected:
            raise DeliveryBoundaryError("prod_approval_changed")
        self._validate_artifacts(self._barrier(change), artifacts)
        return await self.port.deliver_prod(change_id, artifacts, approval)


__all__ = ["ArtifactDigest", "DeliveryBoundaryError", "ProductDeliveryCoordinator", "StageDeploymentReceipt"]
