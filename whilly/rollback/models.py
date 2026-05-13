"""Typed rollback safety-net contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

UTC = timezone.utc

ProtectionStatus = Literal["protected", "unprotected", "unknown"]


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RollbackPoint:
    """Annotated Git tag Whilly can use as a rollback target."""

    name: str
    target_sha: str
    branch: str
    created_at: datetime
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "target_sha": self.target_sha,
            "branch": self.branch,
            "created_at": _format_datetime(self.created_at),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class WorktreeState:
    """Auditable Git worktree state captured before a risky operation."""

    repo_root: Path | str
    branch: str
    head_sha: str
    upstream: str | None
    dirty: bool
    dirty_entries: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_root": str(self.repo_root),
            "branch": self.branch,
            "head_sha": self.head_sha,
            "upstream": self.upstream,
            "dirty": self.dirty,
            "dirty_entries": list(self.dirty_entries),
        }


@dataclass(frozen=True, slots=True)
class ProtectionSignal:
    """Branch protection evidence from a local or remote provider."""

    provider: str = ""
    status: ProtectionStatus = "unknown"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"protected", "unprotected", "unknown"}:
            raise ValueError(f"invalid protection status: {self.status}")

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Structured refusal-first report for push, merge, and restore operations."""

    operation: str
    worktree: WorktreeState
    backup_points: tuple[RollbackPoint, ...]
    protection: ProtectionSignal
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        worktree = self.worktree.to_dict()
        return {
            "operation": self.operation,
            "ok": self.ok,
            "repo_root": worktree["repo_root"],
            "branch": worktree["branch"],
            "head_sha": worktree["head_sha"],
            "upstream": worktree["upstream"],
            "dirty": worktree["dirty"],
            "dirty_entries": worktree["dirty_entries"],
            "backup_points": [point.to_dict() for point in self.backup_points],
            "protection": self.protection.to_dict(),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result of a confirmed or dry-run rollback restore attempt."""

    repo_root: Path | str
    branch: str
    target_ref: str
    target_sha: str
    dry_run: bool
    reset_performed: bool
    preflight: PreflightReport
    message: str

    @property
    def ok(self) -> bool:
        return self.preflight.ok and (self.dry_run or self.reset_performed)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "repo_root": str(self.repo_root),
            "branch": self.branch,
            "target_ref": self.target_ref,
            "target_sha": self.target_sha,
            "dry_run": self.dry_run,
            "reset_performed": self.reset_performed,
            "preflight": self.preflight.to_dict(),
            "message": self.message,
        }
