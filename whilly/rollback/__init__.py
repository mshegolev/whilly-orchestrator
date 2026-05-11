"""Rollback safety-net service exports."""

from __future__ import annotations

from whilly.rollback.git_ops import GitClient, GitCommandResult
from whilly.rollback.models import PreflightReport, ProtectionSignal, RestoreResult, RollbackPoint, WorktreeState
from whilly.rollback.service import (
    RollbackError,
    build_preflight_report,
    confirmation_phrase,
    create_rollback_point,
    list_rollback_points,
    restore_to_ref,
)

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
