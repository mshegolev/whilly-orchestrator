"""Pure proposal contracts and lifecycle policy for cross-project learning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import Principal

PROPOSAL_STATES = {
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

_TRANSITIONS = {
    "proposed": frozenset({"triaged", "rejected", "cancelled"}),
    "triaged": frozenset({"awaiting_approval", "eligible", "rejected", "cancelled"}),
    "awaiting_approval": frozenset({"eligible", "rejected", "cancelled"}),
    "eligible": frozenset({"queued", "blocked", "rejected", "cancelled"}),
    "queued": frozenset({"running", "blocked", "cancelled"}),
    "running": frozenset({"verified", "blocked", "cancelled"}),
    "verified": frozenset(),
    "blocked": frozenset({"triaged", "cancelled"}),
    "rejected": frozenset(),
    "cancelled": frozenset(),
}

_TEXT_LIMIT = 8_000
_SHORT_LIMIT = 256
_MAX_ITEMS = 64
_MAX_EVIDENCE_ITEMS = 32
_MAX_MODULE_LENGTH = 512
_DEPENDENCY_PREFIXES = ("proposal:", "task:")


def _text(value: str, name: str, *, limit: int = _SHORT_LIMIT) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")


def _items(values: tuple[str, ...], name: str, *, limit: int = _MAX_ITEMS) -> None:
    if not isinstance(values, tuple) or not values or len(values) > limit:
        raise ValueError(f"{name} must contain 1..{limit} items")
    for value in values:
        _text(value, name, limit=_SHORT_LIMIT)


def _relative_module(value: str) -> None:
    _text(value, "target_module", limit=_MAX_MODULE_LENGTH)
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise ValueError("target_module must be a safe relative path")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise ValueError("target_module must be a safe relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("target_module must be a safe relative path")


def proposal_fingerprint(
    *,
    target_project: str,
    target_module: str,
    evidence_refs: tuple[str, ...],
    outcome: str,
    contract_impact: str,
    acceptance: tuple[str, ...],
    dependencies: tuple[str, ...],
    resource_class: str,
) -> str:
    """Compute semantic deduplication identity, excluding proposal and origin IDs."""

    content = {
        "target_project": target_project,
        "target_module": target_module,
        "evidence_refs": evidence_refs,
        "outcome": outcome,
        "contract_impact": contract_impact,
        "acceptance": acceptance,
        "dependencies": dependencies,
        "resource_class": resource_class,
    }
    encoded = json.dumps(_jsonable(content), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def can_transition(current: str, target: str) -> bool:
    """Return whether one explicit proposal lifecycle transition is legal."""

    return current in PROPOSAL_STATES and target in _TRANSITIONS.get(current, frozenset())


def transition_state(current: str, target: str) -> str:
    """Validate and return a lifecycle state without performing any side effect."""

    if not can_transition(current, target):
        raise ValueError(f"illegal proposal transition: {current} -> {target}")
    return target


@dataclass(frozen=True)
class TaskProposal:
    id: str
    origin_feature_id: str
    origin_task_id: str | None
    target_project: str
    target_module: str
    evidence_refs: tuple[str, ...]
    outcome: str
    contract_impact: str
    acceptance: tuple[str, ...]
    dependencies: tuple[str, ...]
    resource_class: str
    fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in ("id", "origin_feature_id", "target_project", "outcome", "contract_impact", "resource_class"):
            _text(
                getattr(self, name), name, limit=_TEXT_LIMIT if name in {"outcome", "contract_impact"} else _SHORT_LIMIT
            )
        if self.origin_task_id is not None:
            _text(self.origin_task_id, "origin_task_id")
        _relative_module(self.target_module)
        _items(self.evidence_refs, "evidence_refs", limit=_MAX_EVIDENCE_ITEMS)
        _items(self.acceptance, "acceptance")
        if self.dependencies:
            _items(self.dependencies, "dependencies")
            if any(
                not any(value.startswith(prefix) and value[len(prefix) :].strip() for prefix in _DEPENDENCY_PREFIXES)
                for value in self.dependencies
            ):
                raise ValueError("dependencies must use proposal: or task: references")
        elif not isinstance(self.dependencies, tuple):
            raise ValueError("dependencies must be a tuple")
        if self.contract_impact != "none" and not self.contract_impact.strip():
            raise ValueError("contract_impact must be non-empty")
        computed = proposal_fingerprint(**self.content_dict())
        if self.fingerprint and self.fingerprint != computed:
            raise ValueError("fingerprint does not match proposal content")
        object.__setattr__(self, "fingerprint", computed)

    def content_dict(self) -> dict[str, Any]:
        return {
            "target_project": self.target_project,
            "target_module": self.target_module,
            "evidence_refs": self.evidence_refs,
            "outcome": self.outcome,
            "contract_impact": self.contract_impact,
            "acceptance": self.acceptance,
            "dependencies": self.dependencies,
            "resource_class": self.resource_class,
        }

    @classmethod
    def content_fingerprint(cls, **content: Any) -> str:
        return proposal_fingerprint(**content)


@dataclass(frozen=True)
class ProposalResult:
    id: str
    status: str
    duplicate_of: str | None = None
    reason: str | None = None
    blockers: tuple[str, ...] = ()
    feature_revision: int | None = None

    def __post_init__(self) -> None:
        _text(self.id, "id")
        if self.status not in PROPOSAL_STATES:
            raise ValueError("invalid proposal status")
        if self.duplicate_of is not None:
            _text(self.duplicate_of, "duplicate_of")
        if self.reason is not None:
            _text(self.reason, "reason")
        if not isinstance(self.blockers, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.blockers
        ):
            raise ValueError("blockers must contain non-empty strings")
        if self.feature_revision is not None and (
            not isinstance(self.feature_revision, int) or self.feature_revision < 0
        ):
            raise ValueError("feature_revision must be a non-negative integer")


@dataclass(frozen=True)
class ProposalEvent:
    proposal_id: str
    from_state: str | None
    to_state: str
    actor_id: str
    actor_host: str
    reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("proposal_id", "actor_id", "actor_host"):
            _text(getattr(self, name), name)
        if self.from_state is not None and self.from_state not in PROPOSAL_STATES:
            raise ValueError("invalid previous proposal state")
        if self.to_state not in PROPOSAL_STATES:
            raise ValueError("invalid proposal state")
        if self.reason is not None:
            _text(self.reason, "reason")


class ProposalStore(Protocol):
    async def validate_origin(self, product_id: str, feature_id: str, task_id: str | None) -> None: ...

    async def persist(self, product_id: str, actor_id: str, proposal: TaskProposal) -> ProposalResult: ...

    async def get(self, product_id: str, proposal_id: str) -> Mapping[str, Any] | None: ...


class ProposalAdmissionPort(Protocol):
    async def inspect_admission(
        self, product_id: str, proposal_id: str, *, registry: Any, owner: bool
    ) -> Mapping[str, Any]: ...

    async def accept_for_planning(
        self,
        product_id: str,
        proposal_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        reason: str,
        registry: Any,
    ) -> ProposalResult: ...

    async def reject(self, product_id: str, proposal_id: str, *, actor_id: str, reason: str) -> ProposalResult: ...


class ProposalService:
    """Submit proposals only; lifecycle transitions remain host-controlled store primitives."""

    def __init__(
        self,
        store: ProposalStore,
        *,
        registered_projects: Mapping[str, Sequence[str]],
        trusted_service_products: Mapping[str, str] | None = None,
        admission_port: ProposalAdmissionPort | None = None,
        owner_verified: bool = False,
        actor_id: str | None = None,
        registry: Any = None,
    ) -> None:
        self.store = store
        self.registered_projects = {product: frozenset(projects) for product, projects in registered_projects.items()}
        self.trusted_service_products = dict(trusted_service_products or {})
        self.admission_port = admission_port
        self.owner_verified = owner_verified
        self.actor_id = actor_id
        self.registry = registry

    async def submit(self, principal: Principal, proposal: TaskProposal) -> ProposalResult:
        self._check_actor(principal)
        product_id = self._product_for(principal)
        if product_id not in self.registered_projects:
            raise PermissionError("proposal product is not registered")
        if proposal.target_project not in self.registered_projects[product_id]:
            raise PermissionError("proposal target project is not registered")
        if proposal.target_project not in principal.project_ids:
            raise PermissionError("proposal target project is not authorized")
        await self.store.validate_origin(product_id, proposal.origin_feature_id, proposal.origin_task_id)
        return await self.store.persist(product_id, principal.actor_id, proposal)

    async def evaluate(self, principal: Principal, proposal_id: str) -> ProposalResult:
        self._check_actor(principal)
        product_id = self._product_for(principal)
        row = await self.store.get(product_id, proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        proposal = _proposal_from_row(row)
        blockers: list[str] = []
        if proposal.target_project not in self.registered_projects.get(product_id, ()):
            blockers.append("target_project_not_registered")
        if proposal.target_project not in principal.project_ids:
            blockers.append("target_project_out_of_scope")
        if blockers:
            return _evaluation_result(row, blockers)
        if self.admission_port is None:
            blockers.append("admission_port_missing")
            return _evaluation_result(row, blockers)
        snapshot = await self.admission_port.inspect_admission(
            product_id, proposal_id, registry=self.registry, owner=self.owner_verified
        )
        blockers.extend(_snapshot_blockers(snapshot))
        return _evaluation_result(row, blockers, feature_revision=snapshot.get("feature_revision"))

    async def accept_for_planning(
        self, principal: Principal, proposal_id: str, *, expected_revision: int, reason: str
    ) -> ProposalResult:
        self._check_actor(principal)
        if not self.owner_verified:
            raise PermissionError("host owner grant is required")
        _decision_reason(reason)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if self.admission_port is None:
            return ProposalResult(
                proposal_id, "blocked", reason="admission_port_missing", blockers=("admission_port_missing",)
            )
        evaluated = await self.evaluate(principal, proposal_id)
        if evaluated.status != "eligible":
            return evaluated
        product_id = self._product_for(principal)
        return await self.admission_port.accept_for_planning(
            product_id,
            proposal_id,
            actor_id=principal.actor_id,
            expected_revision=expected_revision,
            reason=reason,
            registry=self.registry,
        )

    async def reject(self, principal: Principal, proposal_id: str, *, reason: str) -> ProposalResult:
        self._check_actor(principal)
        if not self.owner_verified:
            raise PermissionError("host owner grant is required")
        _decision_reason(reason)
        if self.admission_port is None:
            return ProposalResult(
                proposal_id, "blocked", reason="admission_port_missing", blockers=("admission_port_missing",)
            )
        product_id = self._product_for(principal)
        row = await self.store.get(product_id, proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        proposal = _proposal_from_row(row)
        if proposal.target_project not in self.registered_projects.get(product_id, ()):
            raise PermissionError("proposal target project is not registered")
        if proposal.target_project not in principal.project_ids:
            raise PermissionError("proposal target project is not authorized")
        return await self.admission_port.reject(product_id, proposal_id, actor_id=principal.actor_id, reason=reason)

    def _check_actor(self, principal: Principal) -> None:
        if self.actor_id is not None and principal.actor_id != self.actor_id:
            raise PermissionError("host owner principal is not host-verified")

    def _product_for(self, principal: Principal) -> str:
        trusted = self.trusted_service_products.get(principal.actor_id)
        if trusted is not None:
            _text(trusted, "trusted service product")
            return trusted
        if len(principal.product_ids) != 1:
            raise PermissionError("proposal submitter must have exactly one product")
        return principal.product_ids[0]


def _decision_reason(reason: str) -> None:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise ValueError("reason must be a non-empty string of at most 256 characters")


def _proposal_from_row(row: Mapping[str, Any]) -> TaskProposal:
    return TaskProposal(
        id=row["id"],
        origin_feature_id=row["origin_feature_id"],
        origin_task_id=row.get("origin_task_id"),
        target_project=row["target_project"],
        target_module=row["target_module"],
        evidence_refs=tuple(row["evidence_refs"]),
        outcome=row["outcome"],
        contract_impact=row["contract_impact"],
        acceptance=tuple(row["acceptance"]),
        dependencies=tuple(row["dependencies"]),
        resource_class=row["resource_class"],
        fingerprint=row.get("fingerprint", ""),
    )


def _snapshot_blockers(snapshot: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if snapshot.get("protected"):
        blockers.append("protected_module_path")
    if snapshot.get("existing_active_work"):
        blockers.append("existing_active_work")
    if snapshot.get("existing_active_project_work"):
        blockers.append("existing_active_project_work")
    for dependency in snapshot.get("unknown_dependencies", ()):
        blockers.append("dependency_unknown")
    for dependency in snapshot.get("foreign_dependencies", ()):
        blockers.append("dependency_foreign")
    if snapshot.get("dependency_cycle"):
        blockers.append("dependency_cycle")
    if snapshot.get("contract_impact") not in (None, "none"):
        blockers.append("contract_verification_proof_type_missing")
    if snapshot.get("feature_status") == "running":
        blockers.append("feature_running")
    if snapshot.get("budget_unknown"):
        blockers.append("feature_budget_unknown")
    if snapshot.get("budget_exhausted"):
        blockers.append("feature_call_budget_exhausted")
    if snapshot.get("elapsed_budget_exhausted"):
        blockers.append("feature_time_budget_exhausted")
    blockers.extend(str(item) for item in snapshot.get("blockers", ()) if str(item) not in blockers)
    return blockers


def _evaluation_result(row: Mapping[str, Any], blockers: list[str], *, feature_revision: Any = None) -> ProposalResult:
    revision = (
        feature_revision if isinstance(feature_revision, int) and not isinstance(feature_revision, bool) else None
    )
    status = "blocked" if blockers else "eligible"
    return ProposalResult(
        row["id"], status, reason=blockers[0] if blockers else None, blockers=tuple(blockers), feature_revision=revision
    )
