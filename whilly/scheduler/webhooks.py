"""Jira webhook handling for event-driven issue intake."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class JiraWebhookEvent:
    """Parsed Jira webhook event."""

    event_type: str
    issue_key: str
    project_key: str
    summary: str
    description: str | None = None
    status: str | None = None
    assignee: str | None = None
    reporter: str | None = None
    timestamp: str | None = None
    raw_event: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_jira_payload(cls, payload: dict[str, Any]) -> JiraWebhookEvent:
        """Parse Jira webhook payload.

        Args:
            payload: Raw Jira webhook payload

        Returns:
            Parsed JiraWebhookEvent

        Raises:
            ValueError: If payload is invalid
        """
        webhookEvent = payload.get("webhookEvent", "unknown")
        issue_data = payload.get("issue", {})
        fields = issue_data.get("fields", {})

        if not issue_data.get("key"):
            raise ValueError("Missing issue key in webhook payload")

        project_key = fields.get("project", {}).get("key", "")
        if not project_key:
            raise ValueError("Missing project key in webhook payload")

        event = cls(
            event_type=webhookEvent,
            issue_key=issue_data["key"],
            project_key=project_key,
            summary=fields.get("summary", ""),
            description=fields.get("description"),
            status=(fields.get("status") or {}).get("name"),
            assignee=(fields.get("assignee") or {}).get("displayName"),
            reporter=(fields.get("reporter") or {}).get("displayName"),
            timestamp=payload.get("timestamp"),
            raw_event=payload,
        )

        return event

    def matches_rule(self, jql_filter: str) -> bool:
        """Check if event matches a JQL rule.

        Args:
            jql_filter: JQL filter string

        Returns:
            True if event matches (simplified check)
        """
        jql_lower = jql_filter.lower()

        if "project" in jql_lower:
            parts = jql_filter.split("=", 1)
            if len(parts) > 1:
                required_project = parts[1].split("AND")[0].strip().strip("\"'")
                if self.project_key.upper() != required_project.upper():
                    return False

        if "status" in jql_lower and self.status:
            match = False
            clauses = jql_filter.split("AND")
            for clause in clauses:
                if "status" in clause.lower():
                    parts = clause.split("=", 1)
                    if len(parts) > 1:
                        status_val = parts[1].strip().strip("\"'")
                        if self.status.upper() == status_val.upper():
                            match = True
                            break
            if not match:
                return False

        return True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "event_type": self.event_type,
            "issue_key": self.issue_key,
            "project_key": self.project_key,
            "summary": self.summary,
            "description": self.description,
            "status": self.status,
            "assignee": self.assignee,
            "reporter": self.reporter,
            "timestamp": self.timestamp,
        }


class WebhookEventHandler:
    """Handler for Jira webhook events."""

    def __init__(self) -> None:
        """Initialize webhook handler."""
        self._callbacks: dict[str, list[Callable[[JiraWebhookEvent], None]]] = {}

    def register_callback(
        self,
        event_type: str,
        callback: Callable[[JiraWebhookEvent], None],
    ) -> None:
        """Register callback for event type.

        Args:
            event_type: Jira webhook event type (e.g., 'jira:issue_created')
            callback: Async callback function
        """
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)
        log.info("Registered callback for event: %s", event_type)

    async def handle_event(self, payload: dict[str, Any]) -> None:
        """Handle incoming webhook event.

        Args:
            payload: Raw Jira webhook payload
        """
        try:
            event = JiraWebhookEvent.from_jira_payload(payload)
            log.info("Processing webhook event: %s for issue %s", event.event_type, event.issue_key)

            callbacks = self._callbacks.get(event.event_type, [])
            for callback in callbacks:
                try:
                    result = callback(event)
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    log.error("Callback error for %s: %s", event.event_type, exc)
        except ValueError as exc:
            log.error("Invalid webhook payload: %s", exc)
        except Exception as exc:
            log.exception("Webhook handling error: %s", exc)

    def list_event_types(self) -> list[str]:
        """List registered event types.

        Returns:
            List of event type strings
        """
        return sorted(self._callbacks.keys())


def create_webhook_json_payload(
    issue_key: str,
    project_key: str,
    summary: str,
    status: str = "Open",
    event_type: str = "jira:issue_created",
) -> str:
    """Create a sample Jira webhook payload for testing.

    Args:
        issue_key: Issue key (e.g., 'EINVY-123')
        project_key: Project key (e.g., 'EINVY')
        summary: Issue summary
        status: Issue status
        event_type: Webhook event type

    Returns:
        JSON string of webhook payload
    """
    payload = {
        "timestamp": 1234567890,
        "webhookEvent": event_type,
        "issue": {
            "key": issue_key,
            "fields": {
                "summary": summary,
                "status": {"name": status},
                "project": {"key": project_key},
                "assignee": None,
                "reporter": {"displayName": "System"},
                "description": f"Issue {issue_key}: {summary}",
            },
        },
    }
    return json.dumps(payload)
