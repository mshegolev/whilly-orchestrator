"""Execute JQL queries against Jira to fetch issues."""

from __future__ import annotations

import logging
from typing import Any

from whilly.sources.jira import JiraAuth, _jira_get, _jira_rest_path

log = logging.getLogger(__name__)


class JQLExecutionError(RuntimeError):
    """Raised when JQL execution fails."""


def execute_jql(
    jql_filter: str,
    max_results: int = 50,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Execute a JQL query against Jira and return matching issues.

    Args:
        jql_filter: JQL query string (e.g., "project = EINVY AND priority >= High")
        max_results: Maximum number of results to fetch (default 50)
        timeout: HTTP request timeout in seconds

    Returns:
        List of issue dicts with at least: key, summary, project, labels, updated

    Raises:
        JQLExecutionError: if the query execution fails
    """

    try:
        auth = JiraAuth.from_config()
    except Exception as exc:
        raise JQLExecutionError(f"Failed to load Jira credentials: {exc}") from exc

    try:
        path = _jira_rest_path(auth, "search")
        fields = [
            "key",
            "summary",
            "description",
            "project",
            "labels",
            "updated",
            "reporter",
            "assignee",
            "status",
            "priority",
            "issuetype",
            "created",
        ]
        query_parts = [
            f"jql={_urlencode_jql(jql_filter)}",
            f"maxResults={max_results}",
            f"fields={','.join(fields)}",
            "expand=changelog",
        ]
        full_path = f"{path}?{'&'.join(query_parts)}"

        response = _jira_get(auth, full_path, timeout=timeout)

        if not isinstance(response, dict):
            raise JQLExecutionError(f"Unexpected Jira response type: {type(response)}")

        issues = response.get("issues", [])
        if not isinstance(issues, list):
            raise JQLExecutionError(f"'issues' field is not a list: {type(issues)}")

        log.info("JQL query returned %d issues", len(issues))
        return issues

    except JQLExecutionError:
        raise
    except Exception as exc:
        raise JQLExecutionError(f"JQL execution failed: {exc}") from exc


def _urlencode_jql(jql: str) -> str:
    """URL-encode JQL filter for safe transmission in query string."""
    try:
        import urllib.parse

        return urllib.parse.quote(jql, safe="")
    except Exception:
        return jql


def validate_jql(jql_filter: str) -> bool:
    """Validate JQL syntax by attempting a dry-run query.

    Args:
        jql_filter: JQL query to validate

    Returns:
        True if valid, False if invalid

    Note:
        This executes a minimal search to validate syntax.
        For actual validation use Jira's /rest/api/3/jql/validate endpoint.
    """

    try:
        execute_jql(jql_filter, max_results=0)
        return True
    except JQLExecutionError:
        return False
