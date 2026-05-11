"""Sink adapters: post-processing of completed tasks (PR creation, notifications, ...)."""

from whilly.sinks.github_pr import GitHubPRSink, PRResult, open_pr_for_task
from whilly.sinks.post_complete_pr_hook import (
    AUTO_OPEN_PR_ENV,
    is_auto_open_pr_enabled,
    make_post_complete_hook,
    run_post_complete_pr_hook,
)

__all__ = [
    "AUTO_OPEN_PR_ENV",
    "GitHubPRSink",
    "PRResult",
    "is_auto_open_pr_enabled",
    "make_post_complete_hook",
    "open_pr_for_task",
    "run_post_complete_pr_hook",
]
