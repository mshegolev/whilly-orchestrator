"""Fail-closed product-wide exact-SHA merge coordination."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from whilly.swarm.change_set import ChangeSetStatus, EvidenceOutcome, RepoChangeStatus
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


@dataclass(frozen=True)
class MergeBarrierReceipt:
    change_id: str
    change_version: int
    registry_digest: str
    policy_digest: str
    approval_digest: str
    repositories: tuple[MergeRepositoryBinding, ...]


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


__all__ = ["MergeBarrierError", "MergeBarrierReceipt", "MergeRepositoryBinding", "ProductMergeCoordinator"]
