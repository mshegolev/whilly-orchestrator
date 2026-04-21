"""Unified GitHub Issues + Project v2 board source.

Combines a repository's issues with a Project v2 board so the project's
``Status`` field (Todo / In Progress / Review / …) drives which issues become
whilly tasks, while the issue body still supplies description, acceptance
criteria, and priority labels.

Each project item in the target statuses is mapped back to its backing issue
(``projectV2Item.content`` in the GraphQL response); issues outside the project
are ignored, draft items without a backing issue are skipped with a warning.

CLI entry point: ``whilly --from-issues-project <project_url> --repo owner/repo``
Programmatic entry point: :func:`fetch_issues_and_project`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from whilly.github_projects import GitHubProjectsConverter
from whilly.sources.github_issues import (
    GitHubIssuesSource,
    _run_gh,
    merge_into_plan,
    FetchStats,
)

log = logging.getLogger("whilly")


DEFAULT_STATUSES: set[str] = {"Todo"}


def _fetch_issue_json(repo: str, number: int, timeout: int = 30) -> dict | None:
    """Fetch a single issue by number via ``gh issue view``. Returns None on failure."""
    args = [
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,title,body,labels,url,createdAt,updatedAt,state",
    ]
    proc = _run_gh(args, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        log.warning("gh issue view %s#%d failed: %s", repo, number, err)
        return None
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        log.warning("gh returned non-JSON for issue %d: %s", number, exc)
        return None


def _project_status_to_priority_label(status: str, existing_labels: list[dict]) -> list[dict]:
    """Synthesise a priority:* label from project Status unless one is already set."""
    names = {(label.get("name") or "").lower() for label in existing_labels}
    if any(n.startswith("priority:") for n in names):
        return existing_labels

    mapping = {
        "In Progress": "priority:high",
        "Review": "priority:high",
        "Todo": "priority:medium",
        "Ready": "priority:medium",
        "Backlog": "priority:low",
    }
    synthetic = mapping.get(status)
    if not synthetic:
        return existing_labels
    return [*existing_labels, {"name": synthetic}]


def fetch_issues_and_project(
    repo: str,
    project_url: str,
    target_statuses: Iterable[str] | None = None,
    out_path: str | Path = "tasks.json",
    limit: int = 100,
    timeout: int = 30,
) -> tuple[Path, FetchStats]:
    """Fetch project items filtered by status, enrich with issue content, write plan.

    Args:
        repo: ``'owner/repo'``.
        project_url: Full GitHub Project v2 board URL (users/orgs/repo variants).
        target_statuses: Project Status values to include. Default ``{"Todo"}``.
        out_path: Plan file to upsert into (merge is idempotent).
        limit: Maximum issues to materialise (project fetch also page-limits).
        timeout: ``gh`` subprocess timeout per call.

    Returns:
        ``(plan_path, stats)`` mirroring :func:`fetch_github_issues`.
    """
    if "/" not in repo:
        raise ValueError(f"repo must be 'owner/repo', got {repo!r}")
    owner, repo_name = repo.split("/", 1)
    statuses = set(target_statuses) if target_statuses else set(DEFAULT_STATUSES)

    log.info("Fetching project board: %s (statuses=%s)", project_url, sorted(statuses))
    converter = GitHubProjectsConverter()
    project_items = converter.fetch_project_items(project_url, filter_statuses=statuses, include_updated_at=True)
    log.info("Project returned %d items in target statuses", len(project_items))

    enriched_issues: list[dict] = []
    for item in project_items[:limit]:
        if not item.issue_number:
            log.info("Skipping draft project item without backing issue: %s", item.title)
            continue
        issue = _fetch_issue_json(repo, item.issue_number, timeout=timeout)
        if issue is None:
            continue
        if issue.get("state") and issue["state"].upper() != "OPEN":
            log.info("Skipping #%s — not open (state=%s)", issue.get("number"), issue.get("state"))
            continue
        issue["labels"] = _project_status_to_priority_label(item.status, issue.get("labels") or [])
        issue["_project_status"] = item.status
        issue["_project_item_id"] = item.id
        enriched_issues.append(issue)

    log.info("Converting %d issues to whilly tasks", len(enriched_issues))

    source = GitHubIssuesSource(owner=owner, repo=repo_name, label=",".join(sorted(statuses)) or "project", limit=limit)
    plan_path = Path(out_path).resolve()
    stats = merge_into_plan(enriched_issues, source, plan_path)

    log.info(
        "GitHub issues+project source: %d issues materialised (new=%d, updated=%d, closed_externally=%d)",
        stats.total_open,
        stats.new,
        stats.updated,
        stats.closed_externally,
    )
    for warning in stats.secret_warnings:
        log.warning("Secret-like pattern in issue body: %s", warning)

    return plan_path, stats


__all__ = ["fetch_issues_and_project", "DEFAULT_STATUSES"]
