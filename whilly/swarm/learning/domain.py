"""Pure, immutable value objects for shared learning memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_KINDS = frozenset({"fact", "hypothesis", "decision", "observation"})
_STATUSES = frozenset({"candidate", "verified", "stale", "superseded", "retracted"})
_SOURCE_STATUSES = frozenset({"verified", "changed", "unavailable"})


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class Principal:
    actor_id: str
    product_ids: tuple[str, ...]
    project_ids: tuple[str, ...]
    classifications: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.actor_id, "actor_id")
        for field in (self.product_ids, self.project_ids, self.classifications):
            if not isinstance(field, tuple) or any(not isinstance(v, str) or not v.strip() for v in field):
                raise ValueError("grant collections must contain non-empty strings")


@dataclass(frozen=True)
class KnowledgeRevision:
    id: str
    product_id: str
    project_id: str | None
    kind: str
    body: str
    source_uri: str
    source_sha: str | None
    evidence_hash: str
    observed_at: datetime
    verified_at: datetime | None
    expires_at: datetime | None
    classification: str
    status: str
    author_id: str
    verifier_id: str | None
    policy_version: str
    supersedes: str | None = None
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "id",
            "product_id",
            "kind",
            "source_uri",
            "evidence_hash",
            "classification",
            "status",
            "author_id",
            "policy_version",
        ):
            _text(getattr(self, name), name)
        if self.project_id is not None:
            _text(self.project_id, "project_id")
        if not isinstance(self.body, str) or not self.body or len(self.body) > 16000:
            raise ValueError("body must be between 1 and 16000 characters")
        if self.source_sha is not None:
            _text(self.source_sha, "source_sha")
        if self.verifier_id is not None:
            _text(self.verifier_id, "verifier_id")
        if self.supersedes is not None:
            _text(self.supersedes, "supersedes")
        if self.kind not in _KINDS or self.status not in _STATUSES:
            raise ValueError("invalid kind or status")
        if not isinstance(self.conflicts, tuple) or any(
            not isinstance(v, str) or not v.strip() for v in self.conflicts
        ):
            raise ValueError("conflicts must contain non-empty strings")
        for name in ("observed_at", "verified_at", "expires_at"):
            _aware(getattr(self, name), name)


@dataclass(frozen=True)
class SourceCheck:
    status: str
    revision: str | None

    def __post_init__(self) -> None:
        if self.status not in _SOURCE_STATUSES:
            raise ValueError("invalid source check status")
        if self.revision is not None:
            _text(self.revision, "revision")


@dataclass(frozen=True)
class ContextPackage:
    items: tuple[KnowledgeRevision, ...]
    omissions: tuple[str, ...]
    conflicts: tuple[str, ...]
    revision_manifest: tuple[str, ...]
