"""Source adapters: convert external task trackers (GitHub Issues, etc.) into tasks.json."""

from whilly.sources.github_issues import (
    GitHubIssuesSource,
    fetch_github_issues,
    fetch_single_issue,
    parse_issue_ref,
)
from whilly.sources.github_issues_and_project import fetch_issues_and_project

__all__ = [
    "GitHubIssuesSource",
    "fetch_github_issues",
    "fetch_issues_and_project",
    "fetch_single_issue",
    "parse_issue_ref",
]
