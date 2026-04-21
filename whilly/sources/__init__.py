"""Source adapters: convert external task trackers (GitHub Issues, etc.) into tasks.json."""

from whilly.sources.github_issues import (
    GitHubIssuesSource,
    fetch_github_issues,
    fetch_single_issue,
    parse_issue_ref,
)
from whilly.sources.github_issues_and_project import fetch_issues_and_project
from whilly.sources.jira import fetch_single_jira_issue, parse_jira_key

__all__ = [
    "GitHubIssuesSource",
    "fetch_github_issues",
    "fetch_issues_and_project",
    "fetch_single_issue",
    "fetch_single_jira_issue",
    "parse_issue_ref",
    "parse_jira_key",
]
