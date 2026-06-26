"""Deduplication logic for scheduler issue detection."""

from __future__ import annotations

import hashlib
from typing import Any


class DeduplicationError(ValueError):
    """Raised when deduplication fails."""


def compute_issue_hash(
    issue: dict[str, Any],
    fields_to_hash: tuple[str, ...] = ("key", "summary"),
) -> str:
    """Compute a hash representing an issue's identity.

    Args:
        issue: Jira issue dict with at least the fields in fields_to_hash
        fields_to_hash: Tuple of field names to include in hash computation

    Returns:
        SHA256 hash of concatenated field values

    Raises:
        DeduplicationError: if a required field is missing
    """

    values = []
    for field in fields_to_hash:
        value = issue.get(field)
        if value is None:
            # Raw Jira search results (``execute_jql``) nest most attributes
            # under ``fields`` — only ``key``/``id``/``self`` are top-level. Fall
            # back to ``issue["fields"][field]`` so the default
            # ``("key", "summary")`` hashes correctly against real payloads while
            # already-flattened issue dicts (which expose the field at the top
            # level) keep working unchanged.
            nested = issue.get("fields")
            if isinstance(nested, dict):
                value = nested.get(field)
        if value is None:
            raise DeduplicationError(f"Missing field '{field}' for deduplication")

        if isinstance(value, dict):
            value = value.get("key") or str(value)
        elif isinstance(value, (list, tuple)):
            value = "|".join(str(v) for v in value)
        else:
            value = str(value)

        values.append(value.lower().strip())

    combined = "|".join(values)
    return hashlib.sha256(combined.encode()).hexdigest()


def deduplicate_issues(
    issues: list[dict[str, Any]],
    fields_to_hash: tuple[str, ...] = ("key", "summary"),
    seen_hashes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deduplicate a list of issues based on computed hashes.

    Args:
        issues: List of Jira issues
        fields_to_hash: Fields to include in deduplication
        seen_hashes: Pre-existing hashes to treat as already-seen

    Returns:
        Tuple of (unique_issues, duplicate_issue_keys)

    Raises:
        DeduplicationError: if deduplication fails
    """

    unique_issues = []
    duplicate_keys = []
    hashes_seen = set(seen_hashes or [])

    for issue in issues:
        try:
            issue_hash = compute_issue_hash(issue, fields_to_hash)
        except DeduplicationError:
            continue

        if issue_hash in hashes_seen:
            key = issue.get("key", "?")
            duplicate_keys.append(key)
            continue

        unique_issues.append(issue)
        hashes_seen.add(issue_hash)

    return unique_issues, duplicate_keys


def group_issues_by_hash(
    issues: list[dict[str, Any]],
    fields_to_hash: tuple[str, ...] = ("key", "summary"),
) -> dict[str, list[dict[str, Any]]]:
    """Group issues by their deduplication hash.

    Useful for identifying clusters of similar issues.

    Args:
        issues: List of Jira issues
        fields_to_hash: Fields to include in deduplication

    Returns:
        Dict mapping hash → list of issues with that hash
    """

    groups: dict[str, list[dict[str, Any]]] = {}

    for issue in issues:
        try:
            issue_hash = compute_issue_hash(issue, fields_to_hash)
        except DeduplicationError:
            issue_hash = "ERROR"

        if issue_hash not in groups:
            groups[issue_hash] = []
        groups[issue_hash].append(issue)

    return groups


def filter_by_threshold(
    issues: list[dict[str, Any]],
    min_updated_days_ago: int = 0,
    max_age_days: int | None = None,
) -> list[dict[str, Any]]:
    """Filter issues by update recency threshold.

    Args:
        issues: List of Jira issues with 'updated' field (ISO 8601 string)
        min_updated_days_ago: Only include issues updated at least this many days ago
        max_age_days: Only include issues updated within this many days

    Returns:
        Filtered list of issues

    Note:
        This is a basic temporal filter. More sophisticated filtering
        can use the 'updated' field directly in JQL queries.
    """

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    filtered = []

    for issue in issues:
        updated_str = issue.get("updated")
        if not updated_str:
            filtered.append(issue)
            continue

        try:
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            filtered.append(issue)
            continue

        age = now - updated
        if age < timedelta(days=min_updated_days_ago):
            continue
        if max_age_days and age > timedelta(days=max_age_days):
            continue

        filtered.append(issue)

    return filtered
