"""Sink adapters: post-processing of completed tasks (PR creation, notifications, ...)."""

from whilly.sinks.github_pr import GitHubPRSink, PRResult, open_pr_for_task

__all__ = ["GitHubPRSink", "PRResult", "open_pr_for_task"]
