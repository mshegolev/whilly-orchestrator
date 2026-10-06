"""Immutable product change-set state and evidence, independent of transport."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import uuid4


class ChangeSetStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    VERIFYING_REPOS = "VERIFYING_REPOS"
    VERIFYING_INTEGRATION = "VERIFYING_INTEGRATION"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGING = "MERGING"
    MERGED = "MERGED"
    DEPLOYING_STAGE = "DEPLOYING_STAGE"
    ACCEPTING_STAGE = "ACCEPTING_STAGE"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    PARTIAL_MERGE = "PARTIAL_MERGE"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    MANUAL_DECISION_REQUIRED = "MANUAL_DECISION_REQUIRED"


class RepoChangeStatus(StrEnum):
    PLANNED = "PLANNED"
    WORKTREE_READY = "WORKTREE_READY"
    IMPLEMENTING = "IMPLEMENTING"
    LOCAL_VERIFIED = "LOCAL_VERIFIED"
    MR_OPEN = "MR_OPEN"
    PIPELINE_GREEN = "PIPELINE_GREEN"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    ARTIFACT_READY = "ARTIFACT_READY"
    NOT_IMPACTED = "NOT_IMPACTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    REVERT_OPEN = "REVERT_OPEN"
    REVERTED = "REVERTED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class EvidenceOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class TransitionError(ValueError):
    pass


class VersionConflict(RuntimeError):
    pass


class EffectKeyConflict(RuntimeError):
    pass


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label}_required")


def _sha(value: str, *, digest: bool = False) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}" if digest else r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise ValueError("invalid_digest" if digest else "invalid_sha")


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("json_keys_must_be_strings")
        return MappingProxyType({key: freeze(child) for key, child in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(child) for child in value)
    if value is None or type(value) in {str, bool, int, float}:
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError("canonical_json_required")


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw(child) for child in value]
    return value


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(thaw(freeze(value)), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Evidence:
    kind: str
    outcome: EvidenceOutcome
    sha: str | None = None
    command: tuple[str, ...] = ()
    job_id: str | None = None
    exit_code: int | None = None
    absence: str | None = None
    environment: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _text(self.kind, "evidence_kind")
        object.__setattr__(self, "outcome", EvidenceOutcome(self.outcome))
        if self.sha is not None:
            _sha(self.sha)
        if not isinstance(self.command, tuple) or any(not isinstance(arg, str) or not arg for arg in self.command):
            raise ValueError("evidence_command_invalid")
        if self.job_id is not None:
            _text(self.job_id, "job_id")
        if self.absence is not None:
            _text(self.absence, "absence")
        if self.environment is not None:
            _text(self.environment, "environment")
        if self.outcome == EvidenceOutcome.PASSED and self.absence is not None:
            raise ValueError("absence_cannot_be_passed")
        concrete = self.sha is not None and bool(self.command or self.job_id)
        if not concrete and self.absence is None:
            raise ValueError("evidence_command_job_sha_or_named_absence_required")
        if self.command and self.job_id is None and self.exit_code is None:
            raise ValueError("command_exit_code_required")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("exit_code_invalid")
        if self.outcome == EvidenceOutcome.PASSED and self.exit_code not in {None, 0}:
            raise ValueError("nonzero_exit_cannot_be_passed")
        if self.outcome == EvidenceOutcome.UNAVAILABLE and self.absence is None:
            raise ValueError("unavailability_must_be_named")
        if not isinstance(self.details, Mapping):
            raise ValueError("evidence_details_object_required")
        object.__setattr__(self, "details", freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "outcome": self.outcome.value, "sha": self.sha, "command": list(self.command),
                "job_id": self.job_id, "exit_code": self.exit_code, "absence": self.absence,
                "environment": self.environment, "details": thaw(self.details)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Evidence:
        return cls(**{**value, "command": tuple(value.get("command", ()))})


_C_FORWARD = tuple(ChangeSetStatus(value) for value in (
    "DRAFT", "PLANNED", "EXECUTING", "VERIFYING_REPOS", "VERIFYING_INTEGRATION", "READY_TO_MERGE",
    "MERGING", "MERGED", "DEPLOYING_STAGE", "ACCEPTING_STAGE", "DONE"))
_C_PRE = _C_FORWARD[:6]
_C_EDGES = set(zip(_C_FORWARD, _C_FORWARD[1:])) | {
    (source, target) for source in _C_PRE
    for target in (ChangeSetStatus.BLOCKED, ChangeSetStatus.FAILED, ChangeSetStatus.MANUAL_DECISION_REQUIRED)
} | {(ChangeSetStatus.MERGING, ChangeSetStatus.FAILED), (ChangeSetStatus.MERGING, ChangeSetStatus.PARTIAL_MERGE)} | {
    (source, ChangeSetStatus.ROLLING_BACK)
    for source in (ChangeSetStatus.MERGED, ChangeSetStatus.DEPLOYING_STAGE, ChangeSetStatus.ACCEPTING_STAGE,
                   ChangeSetStatus.PARTIAL_MERGE, ChangeSetStatus.ROLLBACK_FAILED)
} | {(ChangeSetStatus.ROLLING_BACK, ChangeSetStatus.ROLLED_BACK),
     (ChangeSetStatus.ROLLING_BACK, ChangeSetStatus.ROLLBACK_FAILED)}
_R_FORWARD = tuple(RepoChangeStatus(value) for value in (
    "PLANNED", "WORKTREE_READY", "IMPLEMENTING", "LOCAL_VERIFIED", "MR_OPEN", "PIPELINE_GREEN",
    "READY_TO_MERGE", "MERGED", "ARTIFACT_READY"))
_R_EDGES = set(zip(_R_FORWARD, _R_FORWARD[1:])) | {
    (source, target) for source in _R_FORWARD[:7] for target in (RepoChangeStatus.BLOCKED, RepoChangeStatus.FAILED)
} | {(RepoChangeStatus.PLANNED, RepoChangeStatus.NOT_IMPACTED),
     (RepoChangeStatus.MERGED, RepoChangeStatus.REVERT_OPEN), (RepoChangeStatus.ARTIFACT_READY, RepoChangeStatus.REVERT_OPEN),
     (RepoChangeStatus.REVERT_OPEN, RepoChangeStatus.REVERTED), (RepoChangeStatus.REVERT_OPEN, RepoChangeStatus.ROLLBACK_FAILED),
     (RepoChangeStatus.ROLLBACK_FAILED, RepoChangeStatus.REVERT_OPEN)}


def _require_passed(evidence: Evidence) -> None:
    if not isinstance(evidence, Evidence) or evidence.outcome != EvidenceOutcome.PASSED:
        raise TransitionError("passed_transition_evidence_required")


@dataclass(frozen=True)
class RepoChange:
    repo_id: str
    base_sha: str
    status: RepoChangeStatus = RepoChangeStatus.PLANNED
    version: int = 1
    mandatory: bool = True
    last_evidence: Evidence | None = None
    resume_status: RepoChangeStatus | None = None

    def __post_init__(self):
        _text(self.repo_id, "repo_id")
        _sha(self.base_sha)
        object.__setattr__(self, "status", RepoChangeStatus(self.status))
        if self.resume_status is not None:
            object.__setattr__(self, "resume_status", RepoChangeStatus(self.resume_status))
        if self.last_evidence is not None and not isinstance(self.last_evidence, Evidence):
            raise ValueError("repo_evidence_invalid")
        if type(self.version) is not int or self.version < 1 or type(self.mandatory) is not bool:
            raise ValueError("repo_version_or_mandatory_invalid")

    def transition(self, target: RepoChangeStatus, evidence: Evidence) -> RepoChange:
        target = RepoChangeStatus(target)
        resume = self.status == RepoChangeStatus.BLOCKED and self.resume_status is not None and target == self.resume_status
        if not resume and (self.status, target) not in _R_EDGES:
            raise TransitionError("repo_transition_not_allowed")
        if target in {RepoChangeStatus.BLOCKED, RepoChangeStatus.FAILED, RepoChangeStatus.ROLLBACK_FAILED}:
            if not isinstance(evidence, Evidence) or evidence.outcome == EvidenceOutcome.PASSED:
                raise TransitionError("failure_evidence_required")
        else:
            _require_passed(evidence)
        if target == RepoChangeStatus.NOT_IMPACTED and evidence.kind != "impact_graph":
            raise TransitionError("impact_graph_evidence_required")
        return replace(self, status=target, version=self.version + 1, last_evidence=evidence,
                       mandatory=False if target == RepoChangeStatus.NOT_IMPACTED else self.mandatory,
                       resume_status=self.status if target == RepoChangeStatus.BLOCKED else None)


@dataclass(frozen=True)
class ProductChangeSet:
    change_id: str
    product_id: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    registry_snapshot: Mapping[str, Any]
    registry_digest: str
    base_shas: Mapping[str, str]
    dependencies: Mapping[str, tuple[str, ...]]
    repo_changes: tuple[RepoChange, ...]
    approval_digest: str | None = None
    status: ChangeSetStatus = ChangeSetStatus.DRAFT
    version: int = 1
    last_evidence: Evidence | None = None
    resume_status: ChangeSetStatus | None = None

    def __post_init__(self):
        for label in ("change_id", "product_id", "goal"):
            _text(getattr(self, label), label)
        if not isinstance(self.acceptance_criteria, tuple) or not self.acceptance_criteria:
            raise ValueError("acceptance_criteria_required")
        for item in self.acceptance_criteria:
            _text(item, "acceptance_criterion")
        object.__setattr__(self, "status", ChangeSetStatus(self.status))
        if self.resume_status is not None:
            object.__setattr__(self, "resume_status", ChangeSetStatus(self.resume_status))
        if not isinstance(self.repo_changes, tuple) or any(not isinstance(repo, RepoChange) for repo in self.repo_changes):
            raise ValueError("immutable_repo_changes_required")
        if self.last_evidence is not None and not isinstance(self.last_evidence, Evidence):
            raise ValueError("change_evidence_invalid")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("change_version_invalid")
        if self.approval_digest is not None:
            _sha(self.approval_digest, digest=True)
        _sha(self.registry_digest, digest=True)
        for name in ("registry_snapshot", "base_shas", "dependencies"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        if canonical_digest(self.registry_snapshot) != self.registry_digest:
            raise ValueError("registry_digest_mismatch")
        projects = set(self.registry_snapshot.get("projects", {}))
        if not projects or projects != set(self.base_shas) or projects != {r.repo_id for r in self.repo_changes}:
            raise ValueError("complete_registry_coverage_required")
        if len(projects) != len(self.repo_changes) or projects != set(self.dependencies):
            raise ValueError("repo_or_dependency_coverage_invalid")
        for sha in self.base_shas.values():
            _sha(sha)
        if any(repo.base_sha != self.base_shas[repo.repo_id] for repo in self.repo_changes):
            raise ValueError("repo_baseline_snapshot_mismatch")
        visited, active = set(), set()

        def visit(repo):
            if repo not in projects:
                raise ValueError("unknown_repository_dependency")
            if repo in active:
                raise ValueError("repository_dependency_cycle")
            if repo in visited:
                return
            active.add(repo)
            for parent in self.dependencies[repo]:
                visit(parent)
            active.remove(repo)
            visited.add(repo)

        for repo in projects:
            visit(repo)

    @classmethod
    def create(cls, *, change_id, product_id, goal, acceptance_criteria, registry_snapshot, base_shas,
               approval_digest=None, dependencies=None) -> ProductChangeSet:
        if not isinstance(acceptance_criteria, (tuple, list)):
            raise ValueError("acceptance_criteria_array_required")
        if not isinstance(registry_snapshot, Mapping) or not isinstance(base_shas, Mapping):
            raise ValueError("registry_and_baselines_object_required")
        projects = registry_snapshot.get("projects", {})
        if not isinstance(projects, Mapping) or any(not isinstance(value, Mapping) for value in projects.values()):
            raise ValueError("registry_projects_object_required")
        dependencies = dependencies if dependencies is not None else {
            repo: tuple(value.get("depends_on", ())) for repo, value in projects.items()
        }
        return cls(change_id, product_id, goal, tuple(acceptance_criteria), registry_snapshot,
                   canonical_digest(registry_snapshot), base_shas, dependencies,
                   tuple(RepoChange(repo, base_shas[repo]) for repo in sorted(base_shas)), approval_digest)

    def transition(self, target: ChangeSetStatus, evidence: Evidence) -> ProductChangeSet:
        target = ChangeSetStatus(target)
        resume = self.status in {ChangeSetStatus.BLOCKED, ChangeSetStatus.MANUAL_DECISION_REQUIRED}
        if resume:
            if self.resume_status is None or target != self.resume_status:
                raise TransitionError("change_resume_boundary_mismatch")
        elif (self.status, target) not in _C_EDGES:
            raise TransitionError("change_transition_not_allowed")
        failures = {ChangeSetStatus.BLOCKED, ChangeSetStatus.FAILED, ChangeSetStatus.MANUAL_DECISION_REQUIRED,
                    ChangeSetStatus.PARTIAL_MERGE, ChangeSetStatus.ROLLBACK_FAILED}
        if target in failures:
            if not isinstance(evidence, Evidence) or evidence.outcome == EvidenceOutcome.PASSED:
                raise TransitionError("failure_evidence_required")
        elif target == ChangeSetStatus.ROLLING_BACK:
            if not isinstance(evidence, Evidence):
                raise TransitionError("compensation_evidence_required")
        else:
            _require_passed(evidence)
        merged = any(repo.status in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY,
                     RepoChangeStatus.REVERT_OPEN, RepoChangeStatus.REVERTED, RepoChangeStatus.ROLLBACK_FAILED}
                     for repo in self.repo_changes)
        if target == ChangeSetStatus.FAILED and merged:
            raise TransitionError("post_merge_failure_requires_compensation")
        if target == ChangeSetStatus.PARTIAL_MERGE and not merged:
            raise TransitionError("partial_merge_requires_observed_merge")
        if target == ChangeSetStatus.ROLLED_BACK and (
            not any(repo.status == RepoChangeStatus.REVERTED for repo in self.repo_changes)
            or any(repo.status in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY,
                                  RepoChangeStatus.REVERT_OPEN, RepoChangeStatus.ROLLBACK_FAILED}
                   for repo in self.repo_changes)
        ):
            raise TransitionError("compensation_incomplete")
        mandatory = tuple(repo for repo in self.repo_changes if repo.mandatory)
        if target == ChangeSetStatus.MERGED and (not mandatory or any(
            repo.status not in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY} for repo in mandatory
        )):
            raise TransitionError("mandatory_merges_incomplete")
        approval_digest = self.approval_digest
        if target == ChangeSetStatus.EXECUTING:
            submitted = evidence.details.get("approval_digest")
            if submitted is not None and approval_digest is not None and submitted != approval_digest:
                raise TransitionError("approval_digest_changed")
            if approval_digest is None:
                if evidence.kind != "approval" or submitted is None:
                    raise TransitionError("approval_digest_required")
                _sha(submitted, digest=True)
                approval_digest = submitted
        if target == ChangeSetStatus.DONE and (
            evidence.kind != "stage_acceptance" or evidence.environment != "stage" or not mandatory
            or any(repo.status != RepoChangeStatus.ARTIFACT_READY for repo in mandatory)
        ):
            raise TransitionError("stage_acceptance_and_artifacts_required")
        return replace(self, status=target, version=self.version + 1, last_evidence=evidence, approval_digest=approval_digest,
                       resume_status=self.status if target in {ChangeSetStatus.BLOCKED,
                       ChangeSetStatus.MANUAL_DECISION_REQUIRED} else None)


@dataclass(frozen=True)
class ExternalEffectReceipt:
    effect_key: str
    change_id: str
    repo_id: str | None
    operation: str
    request_digest: str
    evidence: Evidence

    def __post_init__(self):
        for label in ("effect_key", "change_id", "operation"):
            _text(getattr(self, label), label)
        if self.repo_id is not None:
            _text(self.repo_id, "repo_id")
        _sha(self.request_digest, digest=True)
        if not isinstance(self.evidence, Evidence):
            raise ValueError("effect_evidence_required")


class ChangeSetStorePort(Protocol):
    async def create(self, value: ProductChangeSet) -> ProductChangeSet: ...
    async def get(self, change_id: str) -> ProductChangeSet | None: ...
    async def transition(self, change_id: str, expected_version: int, target: ChangeSetStatus,
                         evidence: Evidence) -> ProductChangeSet: ...
    async def transition_repo(self, change_id: str, repo_id: str, expected_version: int,
                              target: RepoChangeStatus, evidence: Evidence) -> RepoChange: ...
    async def record_effect(self, receipt: ExternalEffectReceipt) -> ExternalEffectReceipt: ...
    async def get_effect(self, effect_key: str) -> ExternalEffectReceipt | None: ...
    async def effects(self, change_id: str) -> tuple[ExternalEffectReceipt, ...]: ...


class ProductChangeSetService:
    def __init__(self, store: ChangeSetStorePort):
        self.store = store

    async def create(self, *, change_id: str | None = None, **values) -> ProductChangeSet:
        identity = "change_" + uuid4().hex if change_id is None else change_id
        return await self.store.create(ProductChangeSet.create(change_id=identity, **values))

    async def transition(self, change_id: str, expected_version: int, target: ChangeSetStatus,
                         evidence: Evidence) -> ProductChangeSet:
        return await self.store.transition(change_id, expected_version, target, evidence)

    async def transition_repo(self, change_id: str, repo_id: str, expected_version: int,
                              target: RepoChangeStatus, evidence: Evidence) -> RepoChange:
        return await self.store.transition_repo(change_id, repo_id, expected_version, target, evidence)
