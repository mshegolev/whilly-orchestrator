"""``whilly project-map`` command surface.

Phase 2 of the Jira Scheduler integration (TASK-SCH-014).
Lets operators inspect resolved repo targets for Jira issues without
running a full import.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from whilly.project_config.loader import load_project_map
from whilly.project_config.models import ProjectMapConfig
from whilly.project_config.resolver import (
    ProjectMapError,
    find_matching_entry,
    resolve_repositories,
)
from whilly.sources.jira import parse_jira_key

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1


def build_project_map_parser() -> argparse.ArgumentParser:
    """Build the ``whilly project-map ...`` argparse tree."""
    parser = argparse.ArgumentParser(
        prog="whilly project-map",
        description="Inspect Jira project key → Git repository mappings.",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")

    p_show = sub.add_parser("show", help="Show resolved repository for a Jira issue key.")
    p_show.add_argument("jira_key", help="Jira issue key, e.g. ABC-123.")
    p_show.add_argument(
        "--config",
        default=None,
        help="Path to project_map.json/.toml (default: ./project_map.json).",
    )
    p_show.add_argument(
        "--labels",
        default="",
        help="Comma-separated list of issue labels to consider for filtering.",
    )
    p_show.add_argument("--json", action="store_true", help="Output result as JSON.")

    p_list = sub.add_parser("list", help="List all project map entries.")
    p_list.add_argument(
        "--config",
        default=None,
        help="Path to project_map.json/.toml (default: ./project_map.json).",
    )
    p_list.add_argument("--json", action="store_true", help="Output as JSON.")

    return parser


def run_project_map_command(argv: Sequence[str]) -> int:
    """Entry point for ``whilly project-map ...``; returns exit code."""
    parser = build_project_map_parser()
    args = parser.parse_args(list(argv))

    if args.action == "show":
        return _run_show(args)
    if args.action == "list":
        return _run_list(args)
    parser.error(f"unknown action {args.action!r}")
    return EXIT_VALIDATION_ERROR


def _load_project_map(config_path: str | None) -> ProjectMapConfig | None:
    """Load project map from the given path or default location."""
    if config_path is None:
        # Try default locations
        for candidate in [Path("project_map.json"), Path("project_map.toml"), Path("config/project_map.json")]:
            if candidate.exists():
                config_path = str(candidate)
                break
        else:
            print(
                "whilly project-map: no project_map.json or project_map.toml found in current directory.",
                file=sys.stderr,
            )
            print("Use --config to specify a path.", file=sys.stderr)
            return None

    try:
        return load_project_map(config_path)
    except Exception as exc:
        print(f"whilly project-map: failed to load {config_path}: {exc}", file=sys.stderr)
        return None


def _run_show(args: argparse.Namespace) -> int:
    """Show the resolved repository for a Jira issue key."""
    try:
        key = parse_jira_key(args.jira_key)
    except ValueError as exc:
        print(f"whilly project-map show: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    project_map = _load_project_map(args.config)
    if project_map is None:
        return EXIT_VALIDATION_ERROR

    # Parse project key from issue key (e.g., "EINVY-123" → "EINVY")
    project_key = key.split("-")[0] if "-" in key else key
    labels = [label.strip() for label in args.labels.split(",") if label.strip()]

    # Find matching entry
    entry = find_matching_entry(project_key, labels, project_map)

    result: dict[str, Any] = {
        "jira_key": key,
        "project_key": project_key,
        "labels": labels,
    }

    if entry is None:
        result["matched"] = False
        result["fallback_repos"] = list(project_map.fallback_repo_ids)
        result["default_mapping"] = project_map.default_mapping.to_dict() if project_map.default_mapping else None
    else:
        result["matched"] = True
        result["entry"] = entry.to_dict()

    # Try full resolution
    try:
        issue = {
            "key": key,
            "project": {"key": project_key},
            "labels": labels,
        }
        repos = resolve_repositories(issue, project_map)
        result["resolved_repos"] = list(repos)
    except ProjectMapError as exc:
        result["resolved_repos"] = []
        result["resolution_error"] = str(exc)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_show_result(result)

    return EXIT_OK if result.get("resolved_repos") else EXIT_VALIDATION_ERROR


def _print_show_result(result: dict[str, Any]) -> None:
    """Print show result in human-readable format."""
    print(f"Jira key:      {result['jira_key']}")
    print(f"Project key:   {result['project_key']}")
    if result["labels"]:
        print(f"Labels:        {', '.join(result['labels'])}")

    if result["matched"]:
        entry = result["entry"]
        print(f"Matched entry: {entry['jira_project_key']}")
        if entry.get("issue_label_filters"):
            print(f"  Label filters: {', '.join(entry['issue_label_filters'])}")
        if entry.get("default_repo_id"):
            print(f"  Default repo:  {entry['default_repo_id']}")
    else:
        print("Matched entry: (none)")

    if result["resolved_repos"]:
        print(f"Resolved repos: {', '.join(result['resolved_repos'])}")
    elif "resolution_error" in result:
        print(f"Resolution error: {result['resolution_error']}")


def _run_list(args: argparse.Namespace) -> int:
    """List all project map entries."""
    project_map = _load_project_map(args.config)
    if project_map is None:
        return EXIT_VALIDATION_ERROR

    if args.json:
        print(json.dumps(project_map.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"Project map (version: {project_map.version})")
    print(f"Total entries: {len(project_map.mappings)}")
    print()

    for entry in project_map.mappings:
        print(f"- {entry.jira_project_key}")
        if entry.git_repository_ids:
            print(f"    repos: {', '.join(entry.git_repository_ids)}")
        if entry.issue_label_filters:
            print(f"    labels: {', '.join(entry.issue_label_filters)}")
        if entry.default_repo_id:
            print(f"    default: {entry.default_repo_id}")

    if project_map.default_mapping:
        print(f"\nDefault mapping: {project_map.default_mapping.jira_project_key}")
    if project_map.fallback_repo_ids:
        print(f"\nFallback repos: {', '.join(project_map.fallback_repo_ids)}")

    return EXIT_OK
