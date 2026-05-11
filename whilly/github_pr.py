"""Backward-compatible re-export of the GitHub PR sink API.

The canonical implementation lives in :mod:`whilly.sinks.github_pr`.
This module exists so that ``from whilly.github_pr import open_pr_for_task``
continues to work for code written against the v1 naming convention.
"""

from whilly.sinks.github_pr import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_GH_BIN,
    DEFAULT_GIT_BIN,
    GitHubPRSink,
    PRResult,
    open_pr_for_task,
    render_pr_body,
)

__all__ = [
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_GH_BIN",
    "DEFAULT_GIT_BIN",
    "GitHubPRSink",
    "PRResult",
    "open_pr_for_task",
    "render_pr_body",
]
