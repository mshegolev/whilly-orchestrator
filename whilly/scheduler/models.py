"""ORM models for scheduler rules and poll cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datetime import datetime


@dataclass(frozen=True)
class SchedulerRule:
    """Configuration for a single JQL-based scheduler rule."""

    id: str
    name: str
    description: str = ""
    enabled: bool = True
    jira_project_key: str = ""
    jql_filter: str = ""
    poll_interval_seconds: int = 300
    max_results_per_poll: int = 50
    deduplication_fields: tuple[str, ...] = ("key", "summary")
    plan_config: dict[str, Any] = field(default_factory=dict)
    custom_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for database serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "jira_project_key": self.jira_project_key,
            "jql_filter": self.jql_filter,
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_results_per_poll": self.max_results_per_poll,
            "deduplication_fields": list(self.deduplication_fields),
            "plan_config": dict(self.plan_config),
            "custom_metadata": dict(self.custom_metadata),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class SchedulerPollCycle:
    """Record of a single poll cycle execution."""

    id: int
    rule_id: str
    poll_status: str = "pending"
    total_issues_found: int = 0
    new_issues_created: int = 0
    duplicate_issues_skipped: int = 0
    error_message: str = ""
    jql_results: list[dict[str, Any]] = field(default_factory=list)
    deduplicated_issues: list[dict[str, Any]] = field(default_factory=list)
    created_plans: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for database serialization."""
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "poll_status": self.poll_status,
            "total_issues_found": self.total_issues_found,
            "new_issues_created": self.new_issues_created,
            "duplicate_issues_skipped": self.duplicate_issues_skipped,
            "error_message": self.error_message,
            "jql_results": list(self.jql_results),
            "deduplicated_issues": list(self.deduplicated_issues),
            "created_plans": list(self.created_plans),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
