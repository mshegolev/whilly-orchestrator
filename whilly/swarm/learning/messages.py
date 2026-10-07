"""Validated, immutable durable message contracts and authorization policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from .domain import Principal

_KINDS = frozenset({"question", "answer", "finding", "contract_change", "task_proposal", "receipt"})
_STATES = frozenset({"persisted", "delivered", "acknowledged", "rejected", "expired"})


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("payload must contain only JSON-compatible values")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class MessageEnvelope:
    """One bounded message addressed to exactly one project and role."""

    id: str
    product_id: str
    feature_id: str | None
    task_id: str | None
    sender_id: str
    recipient_project: str
    recipient_role: str
    kind: str
    correlation_id: str | None
    causation_id: str | None
    idempotency_key: str
    expires_at: datetime
    hop_count: int
    payload: Mapping[str, Any]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("id", "product_id", "sender_id", "recipient_project", "recipient_role", "idempotency_key"):
            _text(getattr(self, name), name)
        for name in ("feature_id", "task_id", "correlation_id", "causation_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if self.kind not in _KINDS:
            raise ValueError("invalid message kind")
        if not isinstance(self.hop_count, int) or isinstance(self.hop_count, bool) or self.hop_count < 0:
            raise ValueError("hop_count must be a non-negative integer")
        _aware(self.expires_at, "expires_at")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        if not isinstance(self.evidence_refs, tuple) or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.evidence_refs
        ):
            raise ValueError("evidence_refs must contain non-empty strings")
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))

    def fingerprint(self) -> str:
        """Return retry identity for content, excluding the generated message id."""
        content = self.as_dict()
        content.pop("id")
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "feature_id": self.feature_id,
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "recipient_project": self.recipient_project,
            "recipient_role": self.recipient_role,
            "kind": self.kind,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "expires_at": self.expires_at.isoformat(),
            "hop_count": self.hop_count,
            "payload": _jsonable(self.payload),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class DeliveryPolicy:
    max_payload_bytes: int
    max_hops: int
    max_fanout: int

    def __post_init__(self) -> None:
        for name in ("max_payload_bytes", "max_hops", "max_fanout"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: str
    state: str
    sender_id: str
    recipient_project: str
    recipient_role: str
    idempotency_key: str
    reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("message_id", "sender_id", "recipient_project", "recipient_role", "idempotency_key"):
            _text(getattr(self, name), name)
        if self.state not in _STATES:
            raise ValueError("invalid receipt state")
        if self.reason is not None:
            _text(self.reason, "reason")


@dataclass(frozen=True)
class RecipientGrant:
    """Host-configured immutable authority for one actor/product/project/role."""

    product_id: str
    project_id: str
    role: str

    def __post_init__(self) -> None:
        _text(self.product_id, "product_id")
        _text(self.project_id, "project_id")
        _text(self.role, "role")


class MessageStore(Protocol):
    async def persist(self, envelope: MessageEnvelope, max_fanout: int) -> DeliveryReceipt: ...

    async def get(self, message_id: str) -> MessageEnvelope | None: ...

    async def list(
        self, principal: Principal, recipient_project: str, recipient_role: str, now: datetime
    ) -> list[MessageEnvelope]: ...

    async def deliver(
        self, principal: Principal, recipient_project: str, recipient_role: str, now: datetime
    ) -> list[MessageEnvelope]: ...

    async def acknowledge(
        self, principal: Principal, message_id: str, recipient_project: str, recipient_role: str, now: datetime
    ) -> DeliveryReceipt: ...

    async def expire(self, now: datetime) -> int: ...


class MessageService:
    """Authorize operations before delegating durable state to a store."""

    def __init__(
        self,
        store: MessageStore,
        policy: DeliveryPolicy | None,
        clock: Callable[[], datetime],
        registered_projects: Mapping[str, Sequence[str]],
        registered_roles: Mapping[str, Sequence[str]],
        recipient_grants: Mapping[str, Sequence[RecipientGrant | tuple[str, str, str]]] | None = None,
        sender_grants: Mapping[str, Sequence[RecipientGrant | tuple[str, str, str]]] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.clock = clock
        self.registered_projects = {product: frozenset(projects) for product, projects in registered_projects.items()}
        self.registered_roles = {project: frozenset(roles) for project, roles in registered_roles.items()}
        self.recipient_grants = _freeze_grants(recipient_grants)
        self.sender_grants = _freeze_grants(sender_grants)

    async def send(self, principal: Principal, envelope: MessageEnvelope) -> DeliveryReceipt:
        self._require_policy()
        if envelope.product_id not in principal.product_ids:
            raise PermissionError("message product is not authorized")
        if envelope.product_id not in self.registered_projects:
            raise PermissionError("message product is not registered")
        if envelope.recipient_project not in principal.project_ids:
            raise PermissionError("message recipient project is not authorized")
        self._authorize_sender_destination(principal, envelope)
        if envelope.correlation_id is None:
            raise ValueError("correlation_id is required for bounded fan-out")
        if envelope.expires_at <= self.clock():
            raise ValueError("message is already expired")
        canonical = (
            envelope if envelope.sender_id == principal.actor_id else replace(envelope, sender_id=principal.actor_id)
        )
        if canonical.causation_id is None:
            canonical = replace(canonical, hop_count=0)
        else:
            parent = await self.store.get(canonical.causation_id)
            if parent is None or parent.product_id != canonical.product_id:
                raise PermissionError("message causation parent is not authorized")
            canonical = replace(canonical, hop_count=parent.hop_count + 1)
        self._check_bounds(canonical)
        return await self.store.persist(canonical, self._require_policy().max_fanout)

    async def list(
        self, principal: Principal, *, recipient_project: str, recipient_role: str, product_id: str | None = None
    ) -> list[MessageEnvelope]:
        self._require_policy()
        restricted, _ = self._authorize_recipient(principal, recipient_project, recipient_role, product_id)
        return await self.store.list(restricted, recipient_project, recipient_role, self.clock())

    async def deliver(
        self,
        principal: Principal,
        *,
        recipient_project: str,
        recipient_role: str,
        product_id: str | None = None,
    ) -> list[MessageEnvelope]:
        self._require_policy()
        restricted, _ = self._authorize_recipient(principal, recipient_project, recipient_role, product_id)
        return await self.store.deliver(restricted, recipient_project, recipient_role, self.clock())

    async def ack(
        self,
        principal: Principal,
        message_id: str,
        *,
        recipient_project: str | None = None,
        recipient_role: str | None = None,
        product_id: str | None = None,
    ) -> DeliveryReceipt:
        self._require_policy()
        recipient_project, recipient_role = self._resolve_recipient(principal, recipient_project, recipient_role)
        restricted, _ = self._authorize_recipient(principal, recipient_project, recipient_role, product_id)
        return await self.store.acknowledge(restricted, message_id, recipient_project, recipient_role, self.clock())

    async def expire(self) -> int:
        self._require_policy()
        return await self.store.expire(self.clock())

    def _require_policy(self) -> DeliveryPolicy:
        if self.policy is None:
            raise PermissionError("message delivery policy is not configured")
        return self.policy

    def _check_bounds(self, envelope: MessageEnvelope) -> None:
        policy = self._require_policy()
        payload_bytes = len(json.dumps(envelope.as_dict()["payload"], separators=(",", ":")).encode())
        if payload_bytes > policy.max_payload_bytes:
            raise ValueError("message payload exceeds policy limit")
        if envelope.hop_count > policy.max_hops:
            raise ValueError("message hop count exceeds policy limit")

    def _check_recipient(self, product_id: str, project: str, role: str) -> None:
        if project not in self.registered_projects[product_id]:
            raise PermissionError("message recipient project is not registered")
        if role not in self.registered_roles.get(project, ()):
            raise PermissionError("message recipient role is not registered")

    def _authorize_sender_destination(self, principal: Principal, envelope: MessageEnvelope) -> None:
        if any(
            grant.product_id == envelope.product_id
            and grant.project_id == envelope.recipient_project
            and grant.role == envelope.recipient_role
            and grant.product_id in principal.product_ids
            and grant.project_id in principal.project_ids
            and grant.product_id in self.registered_projects
            and grant.project_id in self.registered_projects[grant.product_id]
            and grant.role in self.registered_roles.get(grant.project_id, ())
            for grant in self.sender_grants.get(principal.actor_id, ())
        ):
            return
        raise PermissionError("sender destination grant is not authorized")

    def _authorize_recipient(
        self, principal: Principal, project: str, role: str, product_id: str | None
    ) -> tuple[Principal, str]:
        candidates = [
            grant
            for grant in self.recipient_grants.get(principal.actor_id, ())
            if grant.project_id == project
            and grant.role == role
            and grant.product_id in principal.product_ids
            and grant.project_id in principal.project_ids
            and grant.product_id in self.registered_projects
            and project in self.registered_projects[grant.product_id]
            and role in self.registered_roles.get(project, ())
            and (product_id is None or grant.product_id == product_id)
        ]
        products = {grant.product_id for grant in candidates}
        if not products:
            raise PermissionError("recipient grant is not authorized")
        if len(products) > 1:
            raise PermissionError("recipient product scope must be supplied by host")
        selected_product = next(iter(products))
        restricted = Principal(principal.actor_id, (selected_product,), (project,), principal.classifications)
        return restricted, selected_product

    def _resolve_recipient(self, principal: Principal, project: str | None, role: str | None) -> tuple[str, str]:
        grants = self.recipient_grants.get(principal.actor_id, ())
        if project is None:
            projects = {
                grant.project_id
                for grant in grants
                if grant.project_id in principal.project_ids and grant.product_id in principal.product_ids
            }
            if len(projects) != 1:
                raise PermissionError("recipient project must be supplied by host")
            project = next(iter(projects))
        if role is None:
            roles = {
                grant.role
                for grant in grants
                if grant.project_id == project
                and grant.project_id in principal.project_ids
                and grant.product_id in principal.product_ids
            }
            if len(roles) != 1:
                raise PermissionError("recipient role must be supplied by host")
            role = next(iter(roles))
        return project, role


def _freeze_grants(
    grants: Mapping[str, Sequence[RecipientGrant | tuple[str, str, str]]] | None,
) -> Mapping[str, frozenset[RecipientGrant]]:
    return MappingProxyType(
        {
            actor: frozenset(
                grant if isinstance(grant, RecipientGrant) else RecipientGrant(*grant) for grant in actor_grants
            )
            for actor, actor_grants in (grants or {}).items()
        }
    )
