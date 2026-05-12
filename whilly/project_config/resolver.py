"""Resolve Jira issues to Git repositories using project maps."""

from __future__ import annotations

from typing import Any

from whilly.project_config.models import ProjectMapConfig, ProjectMapEntry


class ProjectMapError(ValueError):
    """Raised when project map resolution fails."""


def resolve_repositories(
    issue: dict[str, Any],
    project_map: ProjectMapConfig,
) -> tuple[str, ...]:
    """Resolve Git repositories for a Jira issue using the project map.

    Args:
        issue: Jira issue dict with at least 'key', 'project', 'labels' fields
        project_map: ProjectMapConfig to use for resolution

    Returns:
        Tuple of git repository IDs

    Raises:
        ProjectMapError: if resolution fails
    """

    issue_key = _get_required(issue, "key", str)
    project_key = _get_required_nested(issue, ["project", "key"], str)
    labels = _get_optional_list(issue, "labels", str)

    entry = find_matching_entry(project_key, labels, project_map)
    if entry is None:
        if project_map.default_mapping:
            entry = project_map.default_mapping
        elif project_map.fallback_repo_ids:
            return project_map.fallback_repo_ids
        else:
            raise ProjectMapError(
                f"No mapping found for {issue_key} in project {project_key} "
                f"and no default mapping or fallback repos configured"
            )

    repos = entry.git_repository_ids
    if not repos and entry.git_repository_paths:
        repos = tuple(entry.git_repository_paths)

    if not repos:
        if project_map.fallback_repo_ids:
            repos = project_map.fallback_repo_ids
        else:
            raise ProjectMapError(
                f"No repositories configured for {issue_key} in {project_key} and no fallback repos available"
            )

    return repos


def find_matching_entry(
    jira_project_key: str,
    issue_labels: list[str] | None,
    project_map: ProjectMapConfig,
) -> ProjectMapEntry | None:
    """Find a project map entry matching the given Jira project and labels.

    Args:
        jira_project_key: Jira project key (e.g., "EINVY")
        issue_labels: List of issue labels to filter against
        project_map: ProjectMapConfig to search

    Returns:
        Matching ProjectMapEntry or None if no match found
    """

    labels_set = set(issue_labels or [])

    for entry in project_map.mappings:
        if entry.jira_project_key.upper() != jira_project_key.upper():
            continue

        if entry.issue_label_filters:
            filters_set = set(entry.issue_label_filters)
            if not labels_set & filters_set:
                continue

        return entry

    return None


def match_label_filters(
    issue_labels: list[str] | None,
    filter_labels: tuple[str, ...],
) -> bool:
    """Check if issue labels match filter labels (any match = True).

    Args:
        issue_labels: List of labels on the issue
        filter_labels: Tuple of filter labels

    Returns:
        True if any filter label matches an issue label, False otherwise
    """

    if not filter_labels:
        return True

    labels_set = set(issue_labels or [])
    return bool(labels_set & set(filter_labels))


def _get_required(data: dict[str, Any], key: str, expected_type: type) -> Any:
    """Get a required field from a dict and validate its type."""
    value = data.get(key)
    if value is None:
        raise ProjectMapError(f"required field '{key}' not found")
    if not isinstance(value, expected_type):
        raise ProjectMapError(f"field '{key}' expected {expected_type.__name__}, got {type(value).__name__}")
    return value


def _get_required_nested(data: dict[str, Any], path: list[str], expected_type: type) -> Any:
    """Get a required nested field from a dict and validate its type."""
    current = data
    for key in path[:-1]:
        if not isinstance(current, dict):
            raise ProjectMapError(f"cannot navigate path {'.'.join(path)}")
        current = current.get(key)
        if current is None:
            raise ProjectMapError(f"required field '{'.'.join(path)}' not found")

    final_key = path[-1]
    if not isinstance(current, dict):
        raise ProjectMapError(f"cannot access '{final_key}' on non-dict")
    value = current.get(final_key)
    if value is None:
        raise ProjectMapError(f"required field '{'.'.join(path)}' not found")
    if not isinstance(value, expected_type):
        raise ProjectMapError(f"field '{'.'.join(path)}' expected {expected_type.__name__}, got {type(value).__name__}")
    return value


def _get_optional_list(data: dict[str, Any], key: str, expected_element_type: type) -> list[Any]:
    """Get an optional list field from a dict and validate element types."""
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ProjectMapError(f"field '{key}' expected list, got {type(value).__name__}")

    result = []
    for i, item in enumerate(value):
        if not isinstance(item, expected_element_type):
            raise ProjectMapError(
                f"field '{key}[{i}]' expected {expected_element_type.__name__}, got {type(item).__name__}"
            )
        result.append(item)
    return result
