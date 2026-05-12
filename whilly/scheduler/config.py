"""Scheduler configuration loading and management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from whilly.scheduler.models import SchedulerRule


class SchedulerConfigError(ValueError):
    """Raised when scheduler configuration is invalid."""


def load_scheduler_config(path: str | Path) -> list[SchedulerRule]:
    """Load scheduler rules from JSON or TOML file.

    Args:
        path: Path to configuration file

    Returns:
        List of SchedulerRule objects

    Raises:
        SchedulerConfigError: if configuration is invalid
    """

    config_path = Path(path)
    try:
        if config_path.suffix.lower() == ".json":
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        elif config_path.suffix.lower() in {".toml", ".tml"}:
            import tomllib

            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        else:
            raise SchedulerConfigError(f"{config_path}: expected .json or .toml config")
    except OSError as exc:
        raise SchedulerConfigError(f"cannot read scheduler config {config_path}: {exc}") from exc
    except (json.JSONDecodeError, Exception) as exc:
        raise SchedulerConfigError(f"scheduler config {config_path} is not valid: {exc}") from exc

    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"{config_path}: top-level config must be an object")

    rules_data = raw.get("rules", [])
    if not isinstance(rules_data, list):
        raise SchedulerConfigError(f"{config_path}: 'rules' must be a list")

    rules = []
    for i, rule_dict in enumerate(rules_data):
        try:
            rule = _rule_from_dict(rule_dict, f"{config_path}:rules[{i}]")
            rules.append(rule)
        except SchedulerConfigError:
            raise

    return rules


def _rule_from_dict(data: dict[str, Any], source: str) -> SchedulerRule:
    """Convert a dict to a SchedulerRule, validating required fields."""

    rule_id = data.get("id", "").strip()
    if not rule_id:
        raise SchedulerConfigError(f"{source}: 'id' is required and must be non-empty")

    name = data.get("name", "").strip()
    if not name:
        raise SchedulerConfigError(f"{source}: 'name' is required and must be non-empty")

    jira_project = data.get("jira_project_key", "").strip()
    if not jira_project:
        raise SchedulerConfigError(f"{source}: 'jira_project_key' is required and must be non-empty")

    jql = data.get("jql_filter", "").strip()
    if not jql:
        raise SchedulerConfigError(f"{source}: 'jql_filter' is required and must be non-empty")

    try:
        dedup_fields = tuple(data.get("deduplication_fields", ["key", "summary"]))
        if not dedup_fields:
            dedup_fields = ("key", "summary")

        poll_interval = int(data.get("poll_interval_seconds", 300))
        if poll_interval <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        max_results = int(data.get("max_results_per_poll", 50))
        if max_results <= 0:
            raise ValueError("max_results_per_poll must be positive")

        plan_config = data.get("plan_config", {})
        if not isinstance(plan_config, dict):
            plan_config = {}

        custom_metadata = data.get("custom_metadata", {})
        if not isinstance(custom_metadata, dict):
            custom_metadata = {}

        return SchedulerRule(
            id=rule_id,
            name=name,
            description=data.get("description", "").strip(),
            enabled=data.get("enabled", True),
            jira_project_key=jira_project,
            jql_filter=jql,
            poll_interval_seconds=poll_interval,
            max_results_per_poll=max_results,
            deduplication_fields=dedup_fields,
            plan_config=plan_config,
            custom_metadata=custom_metadata,
        )
    except (ValueError, TypeError) as exc:
        raise SchedulerConfigError(f"{source}: invalid configuration: {exc}") from exc
