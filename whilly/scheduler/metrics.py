"""Metrics collection and reporting for scheduler."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class PollMetrics:
    """Metrics for a single poll cycle."""

    rule_id: str
    success: bool
    duration_seconds: float
    issues_found: int = 0
    issues_unique: int = 0
    issues_duplicated: int = 0
    api_requests: int = 0
    error_message: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "rule_id": self.rule_id,
            "success": self.success,
            "duration_seconds": self.duration_seconds,
            "issues_found": self.issues_found,
            "issues_unique": self.issues_unique,
            "issues_duplicated": self.issues_duplicated,
            "api_requests": self.api_requests,
            "error_message": self.error_message,
            "timestamp": self.timestamp.isoformat(),
        }


class MetricsCollector:
    """Collect and aggregate scheduler metrics."""

    def __init__(self, output_dir: Path | None = None) -> None:
        """Initialize metrics collector.

        Args:
            output_dir: Directory for metrics output (optional)
        """
        self.output_dir = output_dir
        self.metrics: list[PollMetrics] = []
        self.start_time = datetime.now(timezone.utc)

    def record_poll(self, metrics: PollMetrics) -> None:
        """Record metrics for a poll cycle.

        Args:
            metrics: PollMetrics object
        """
        self.metrics.append(metrics)
        log.info(
            "Poll metrics: rule=%s success=%s duration=%.2fs issues=%d/%d",
            metrics.rule_id,
            metrics.success,
            metrics.duration_seconds,
            metrics.issues_found,
            metrics.issues_unique,
        )

    def get_summary(self) -> dict[str, Any]:
        """Get summary statistics.

        Returns:
            Dictionary with summary metrics
        """
        total_polls = len(self.metrics)
        successful_polls = sum(1 for m in self.metrics if m.success)
        failed_polls = total_polls - successful_polls

        total_duration = sum(m.duration_seconds for m in self.metrics)
        avg_duration = total_duration / total_polls if total_polls > 0 else 0

        total_issues = sum(m.issues_found for m in self.metrics)
        total_unique = sum(m.issues_unique for m in self.metrics)
        total_duplicated = sum(m.issues_duplicated for m in self.metrics)

        return {
            "total_polls": total_polls,
            "successful_polls": successful_polls,
            "failed_polls": failed_polls,
            "success_rate": successful_polls / total_polls if total_polls > 0 else 0,
            "total_duration_seconds": total_duration,
            "average_duration_seconds": avg_duration,
            "total_issues_found": total_issues,
            "total_issues_unique": total_unique,
            "total_issues_duplicated": total_duplicated,
            "collection_duration": (datetime.now(timezone.utc) - self.start_time).total_seconds(),
        }

    def get_rule_summary(self, rule_id: str) -> dict[str, Any]:
        """Get summary for a specific rule.

        Args:
            rule_id: Rule ID

        Returns:
            Dictionary with rule-specific metrics
        """
        rule_metrics = [m for m in self.metrics if m.rule_id == rule_id]

        if not rule_metrics:
            return {"rule_id": rule_id, "polls": 0}

        total_polls = len(rule_metrics)
        successful_polls = sum(1 for m in rule_metrics if m.success)

        return {
            "rule_id": rule_id,
            "polls": total_polls,
            "successful": successful_polls,
            "failed": total_polls - successful_polls,
            "success_rate": successful_polls / total_polls if total_polls > 0 else 0,
            "total_issues": sum(m.issues_found for m in rule_metrics),
            "unique_issues": sum(m.issues_unique for m in rule_metrics),
        }

    def export_json(self, path: Path) -> None:
        """Export metrics to JSON file.

        Args:
            path: Path to write JSON file
        """
        data = {
            "summary": self.get_summary(),
            "metrics": [m.to_dict() for m in self.metrics],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        log.info("Exported %d metrics to %s", len(self.metrics), path)

    def export_jsonl(self, path: Path) -> None:
        """Export metrics to JSONL file (one metric per line).

        Args:
            path: Path to write JSONL file
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for metric in self.metrics:
                f.write(json.dumps(metric.to_dict()) + "\n")
        log.info("Exported %d metrics to %s", len(self.metrics), path)
