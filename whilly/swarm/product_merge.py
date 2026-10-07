"""Fail-closed product-wide exact-SHA merge coordination."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from whilly.swarm.change_set import ChangeSetStatus, Evidence, EvidenceOutcome, RepoChangeStatus
from whilly.swarm.gitlab_change_transport import PipelineReceipt, RepoPublicationReceipt
from whilly.swarm.product_registry import ProductRegistrySnapshot


class MergeBarrierError(RuntimeError):
    """Named merge-boundary failure without leaking provider response bodies."""


@dataclass(frozen=True)
class MergeRepositoryBinding:
    repo_id: str
    project_id: int
    source_sha: str
    target_sha: str
    source_branch: str
    mr_iid: int
    pipeline_id: int
    required_jobs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "required_jobs": list(self.required_jobs)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MergeRepositoryBinding:
        return cls(**{**value, "required_jobs": tuple(value["required_jobs"])})


@dataclass(frozen=True)
class MergeBarrierReceipt:
    change_id: str
    change_version: int
    registry_digest: str
    policy_digest: str
    approval_digest: str
    repositories: tuple[MergeRepositoryBinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "repositories": [repo.to_dict() for repo in self.repositories]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MergeBarrierReceipt:
        try:
            return cls(**{**value, "repositories": tuple(
                MergeRepositoryBinding.from_dict(repo) for repo in value["repositories"]
            )})
        except (KeyError, TypeError, ValueError):
            raise MergeBarrierError("merge_barrier_receipt_invalid") from None


def _publication(value: Mapping[str, Any]) -> RepoPublicationReceipt:
    try:
        pipeline = value["pipeline"]
        return RepoPublicationReceipt(
            change_id=value["change_id"],
            repo_id=value["repo_id"],
            source_sha=value["source_sha"],
            target_sha=value["target_sha"],
            source_branch=value["source_branch"],
            mr_iid=value["mr_iid"],
            mr_url=value["mr_url"],
            pipeline=PipelineReceipt(
                pipeline["project_id"], pipeline["sha"], pipeline["pipeline_id"], pipeline["status"],
                tuple(tuple(job) for job in pipeline.get("jobs", ())),
            ),
            registry_digest=value["registry_digest"],
            policy_digest=value["policy_digest"],
            approval_digest=value["approval_digest"],
        )
    except (KeyError, TypeError, ValueError):
        raise MergeBarrierError("publication_receipt_invalid") from None


def _dependency_order(change) -> tuple[str, ...]:
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(repo_id: str) -> None:
        if repo_id in visited:
            return
        for dependency in change.dependencies[repo_id]:
            visit(dependency)
        visited.add(repo_id)
        ordered.append(repo_id)

    for repo in sorted(change.repo_changes, key=lambda item: item.repo_id):
        if repo.mandatory:
            visit(repo.repo_id)
    return tuple(repo_id for repo_id in ordered if next(r for r in change.repo_changes if r.repo_id == repo_id).mandatory)


class ProductMergeCoordinator:
    """Re-read all mutable GitLab evidence before permitting the first merge."""

    def __init__(self, store, snapshot: ProductRegistrySnapshot, transport):
        self.store = store
        self.snapshot = snapshot
        self.transport = transport

    async def verify_barrier(self, change_id: str) -> MergeBarrierReceipt:
        change = await self.store.get(change_id)
        if change is None:
            raise MergeBarrierError("change_set_not_found")
        if change.status != ChangeSetStatus.READY_TO_MERGE:
            raise MergeBarrierError("change_set_not_ready_to_merge")
        if (change.registry_digest != self.snapshot.registry_digest or change.approval_digest is None):
            raise MergeBarrierError("merge_approval_binding_changed")
        try:
            self.snapshot.assert_unchanged()
        except ValueError:
            raise MergeBarrierError("product_registry_changed") from None

        by_id = {repo.repo_id: repo for repo in change.repo_changes}
        mandatory = _dependency_order(change)
        if not mandatory:
            raise MergeBarrierError("mandatory_repositories_required")
        bindings: list[MergeRepositoryBinding] = []
        for repo_id in mandatory:
            repo = by_id[repo_id]
            policy = self.snapshot.projects.get(repo_id)
            if (policy is None or repo.status not in {RepoChangeStatus.PIPELINE_GREEN, RepoChangeStatus.READY_TO_MERGE}
                    or repo.last_evidence is None or repo.last_evidence.outcome != EvidenceOutcome.PASSED
                    or repo.last_evidence.kind != "exact_sha_pipeline"):
                raise MergeBarrierError("mandatory_repository_not_ready")
            receipt = _publication(repo.last_evidence.details)
            if (receipt.change_id != change.change_id or receipt.repo_id != repo_id
                    or receipt.target_sha != repo.base_sha or receipt.source_sha != repo.last_evidence.sha
                    or receipt.registry_digest != change.registry_digest
                    or receipt.policy_digest != self.snapshot.policy_digest
                    or receipt.approval_digest != change.approval_digest
                    or receipt.pipeline.project_id != policy.gitlab_project_id):
                raise MergeBarrierError("publication_binding_changed")
            observed_mr = await self.transport.read_merge_request(policy, receipt)
            if observed_mr != receipt:
                raise MergeBarrierError("merge_request_identity_changed")
            if await self.transport.read_target_sha(policy) != receipt.target_sha:
                raise MergeBarrierError("merge_target_changed")
            required_jobs = tuple(policy.checks["ci"])
            pipeline = await self.transport.read_exact_pipeline(
                policy.gitlab_project_id, receipt.source_sha, required_jobs=required_jobs
            )
            if (not pipeline.green or pipeline.sha != receipt.source_sha or pipeline.project_id != policy.gitlab_project_id
                    or tuple(job[0] for job in pipeline.jobs) != required_jobs):
                raise MergeBarrierError("exact_sha_pipeline_not_green")
            bindings.append(MergeRepositoryBinding(
                repo_id, policy.gitlab_project_id, receipt.source_sha, receipt.target_sha,
                receipt.source_branch, receipt.mr_iid, pipeline.pipeline_id, required_jobs,
            ))
        return MergeBarrierReceipt(
            change.change_id, change.version, change.registry_digest, self.snapshot.policy_digest,
            change.approval_digest, tuple(bindings),
        )

    @staticmethod
    def _barrier_from(change) -> MergeBarrierReceipt:
        if change.last_evidence is None or not isinstance(change.last_evidence.details.get("barrier"), Mapping):
            raise MergeBarrierError("merge_barrier_receipt_missing")
        return MergeBarrierReceipt.from_dict(change.last_evidence.details["barrier"])

    async def _assert_binding(self, change_id: str, barrier: MergeBarrierReceipt):
        current = await self.store.get(change_id)
        if (current is None or current.registry_digest != barrier.registry_digest
                or current.approval_digest != barrier.approval_digest
                or self.snapshot.registry_digest != barrier.registry_digest
                or self.snapshot.policy_digest != barrier.policy_digest):
            raise MergeBarrierError("merge_approval_binding_changed")
        try:
            self.snapshot.assert_unchanged()
        except ValueError:
            raise MergeBarrierError("product_registry_changed") from None
        return current

    async def _compensate(self, change_id: str, barrier: MergeBarrierReceipt):
        change = await self.store.get(change_id)
        if change.status in {ChangeSetStatus.PARTIAL_MERGE, ChangeSetStatus.ROLLBACK_FAILED}:
            evidence = Evidence(
                "compensation_required", EvidenceOutcome.FAILED, absence="merge_sequence_incomplete",
                details={"barrier": barrier.to_dict()},
            )
            change = await self.store.transition(change_id, change.version, ChangeSetStatus.ROLLING_BACK, evidence)
        elif change.status != ChangeSetStatus.ROLLING_BACK:
            raise MergeBarrierError("compensation_state_invalid")
        bindings = {binding.repo_id: binding for binding in barrier.repositories}
        for repo_id in reversed([binding.repo_id for binding in barrier.repositories]):
            change = await self.store.get(change_id)
            repo = next(item for item in change.repo_changes if item.repo_id == repo_id)
            if repo.status == RepoChangeStatus.REVERTED:
                continue
            if repo.status not in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY,
                                   RepoChangeStatus.REVERT_OPEN, RepoChangeStatus.ROLLBACK_FAILED}:
                continue
            binding = bindings[repo_id]
            merge_effect = repo.last_evidence.details.get("merge_effect", {})
            if repo.status in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY,
                               RepoChangeStatus.ROLLBACK_FAILED}:
                opened = Evidence(
                    "revert_mr", EvidenceOutcome.PASSED, sha=binding.source_sha,
                    job_id=f"revert:{binding.mr_iid}",
                    details={"barrier": barrier.to_dict(), "merge_effect": merge_effect},
                )
                repo = await self.store.transition_repo(
                    change_id, repo_id, repo.version, RepoChangeStatus.REVERT_OPEN, opened
                )
            try:
                await self._assert_binding(change_id, barrier)
                result = await self.transport.revert_merge_request(
                    change_id, self.snapshot.projects[repo_id], binding, merge_effect
                )
                reverted = Evidence(
                    "revert_merged", EvidenceOutcome.PASSED,
                    sha=result["revert_commit_sha"], job_id=f"mr:{result['mr_iid']}",
                    details={"barrier": barrier.to_dict(), "revert_effect": result},
                )
                await self.store.transition_repo(
                    change_id, repo_id, repo.version, RepoChangeStatus.REVERTED, reverted
                )
            except Exception:
                failed = Evidence(
                    "revert_failed", EvidenceOutcome.UNAVAILABLE, absence="revert_effect_unavailable",
                    details={"barrier": barrier.to_dict(), "merge_effect": merge_effect},
                )
                current = await self.store.get(change_id)
                current_repo = next(item for item in current.repo_changes if item.repo_id == repo_id)
                if current_repo.status == RepoChangeStatus.REVERT_OPEN:
                    await self.store.transition_repo(
                        change_id, repo_id, current_repo.version, RepoChangeStatus.ROLLBACK_FAILED, failed
                    )
                current = await self.store.get(change_id)
                return await self.store.transition(
                    change_id, current.version, ChangeSetStatus.ROLLBACK_FAILED, failed
                )
        current = await self.store.get(change_id)
        done = Evidence(
            "compensation_complete", EvidenceOutcome.PASSED,
            sha=barrier.repositories[0].source_sha, job_id="product-rollback",
            details={"barrier": barrier.to_dict()},
        )
        return await self.store.transition(change_id, current.version, ChangeSetStatus.ROLLED_BACK, done)

    async def merge_all(self, change_id: str):
        change = await self.store.get(change_id)
        if change is None:
            raise MergeBarrierError("change_set_not_found")
        if change.status in {ChangeSetStatus.MERGED, ChangeSetStatus.ROLLED_BACK}:
            return change
        if change.status in {ChangeSetStatus.PARTIAL_MERGE, ChangeSetStatus.ROLLING_BACK,
                             ChangeSetStatus.ROLLBACK_FAILED}:
            return await self._compensate(change_id, self._barrier_from(change))
        if change.status == ChangeSetStatus.READY_TO_MERGE:
            barrier = await self.verify_barrier(change_id)
            barrier_details = {"barrier": barrier.to_dict()}
            ready = Evidence(
                "product_merge_barrier", EvidenceOutcome.PASSED,
                sha=barrier.repositories[0].source_sha, job_id="product-barrier", details=barrier_details,
            )
            for binding in barrier.repositories:
                change = await self._assert_binding(change_id, barrier)
                repo = next(item for item in change.repo_changes if item.repo_id == binding.repo_id)
                if repo.status == RepoChangeStatus.PIPELINE_GREEN:
                    await self.store.transition_repo(
                        change_id, repo.repo_id, repo.version, RepoChangeStatus.READY_TO_MERGE, ready
                    )
            change = await self.store.get(change_id)
            change = await self.store.transition(change_id, change.version, ChangeSetStatus.MERGING, ready)
        elif change.status == ChangeSetStatus.MERGING:
            barrier = self._barrier_from(change)
        else:
            raise MergeBarrierError("change_set_not_mergeable")

        merged_any = any(repo.status in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY}
                         for repo in change.repo_changes)
        try:
            for binding in barrier.repositories:
                change = await self._assert_binding(change_id, barrier)
                repo = next(item for item in change.repo_changes if item.repo_id == binding.repo_id)
                if repo.status in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY}:
                    merged_any = True
                    continue
                if repo.status != RepoChangeStatus.READY_TO_MERGE:
                    raise MergeBarrierError("repository_not_ready_to_merge")
                if await self.transport.read_target_sha(self.snapshot.projects[repo.repo_id]) != binding.target_sha:
                    raise MergeBarrierError("merge_target_changed")
                result = await self.transport.merge_request(
                    change_id, self.snapshot.projects[repo.repo_id], binding
                )
                merged_any = True
                evidence = Evidence(
                    "merge_observed", EvidenceOutcome.PASSED,
                    sha=result["merge_commit_sha"], job_id=f"mr:{result['mr_iid']}",
                    details={"barrier": barrier.to_dict(), "merge_effect": result},
                )
                await self.store.transition_repo(
                    change_id, repo.repo_id, repo.version, RepoChangeStatus.MERGED, evidence
                )
        except Exception:
            current = await self.store.get(change_id)
            failed = Evidence(
                "merge_sequence_failed", EvidenceOutcome.UNAVAILABLE, absence="merge_or_target_unavailable",
                details={"barrier": barrier.to_dict()},
            )
            target = ChangeSetStatus.PARTIAL_MERGE if merged_any else ChangeSetStatus.FAILED
            current = await self.store.transition(change_id, current.version, target, failed)
            if target == ChangeSetStatus.PARTIAL_MERGE:
                return await self._compensate(change_id, barrier)
            return current
        current = await self.store.get(change_id)
        complete = Evidence(
            "product_merge_complete", EvidenceOutcome.PASSED,
            sha=barrier.repositories[-1].source_sha, job_id="product-merge",
            details={"barrier": barrier.to_dict()},
        )
        return await self.store.transition(change_id, current.version, ChangeSetStatus.MERGED, complete)


__all__ = ["MergeBarrierError", "MergeBarrierReceipt", "MergeRepositoryBinding", "ProductMergeCoordinator"]
