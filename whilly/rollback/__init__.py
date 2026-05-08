"""Rollback safety-net service exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from whilly.rollback.git_ops import GitClient, GitCommandResult
from whilly.rollback.models import PreflightReport, ProtectionSignal, RestoreResult, RollbackPoint, WorktreeState

if TYPE_CHECKING:
    from whilly.rollback.service import (
        RollbackError,
        build_preflight_report,
        confirmation_phrase,
        create_rollback_point,
        list_rollback_points,
        restore_to_ref,
    )

_SERVICE_EXPORTS = {
    "RollbackError",
    "build_preflight_report",
    "confirmation_phrase",
    "create_rollback_point",
    "list_rollback_points",
    "restore_to_ref",
}

__all__ = [
    "GitClient",
    "GitCommandResult",
    "PreflightReport",
    "ProtectionSignal",
    "RestoreResult",
    "RollbackError",
    "RollbackPoint",
    "WorktreeState",
    "build_preflight_report",
    "confirmation_phrase",
    "create_rollback_point",
    "list_rollback_points",
    "restore_to_ref",
]


def __getattr__(name: str) -> Any:
    if name in _SERVICE_EXPORTS:
        from whilly.rollback import service

        return getattr(service, name)
    raise AttributeError(name)
