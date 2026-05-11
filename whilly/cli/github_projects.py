"""GitHub Projects v2 sync CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from whilly.github_projects import GitHubProjectsConverter, SyncConfig

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 3


def build_github_projects_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly github-projects",
        description="Sync GitHub Projects v2 Todo items and status changes.",
    )
    parser.add_argument(
        "--state-file",
        default=".whilly_project_sync_state.json",
        help="Path to the Project sync state file.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    sync_todo = subcommands.add_parser("sync-todo", help="Create Issues/tasks from Project Todo items.")
    sync_todo.add_argument("project_url", help="GitHub Projects v2 URL.")
    sync_todo.add_argument("--repo", required=True, help="Target repository as owner/name.")
    sync_todo.add_argument("--output", default="tasks-from-project.json", help="Output plan path.")
    sync_todo.add_argument(
        "--existing-only",
        action="store_true",
        help="Record existing Issue items only; do not convert draft Project items into Issues.",
    )

    from_project = subcommands.add_parser("from-project", help="Convert project items to Issues/tasks.")
    from_project.add_argument("project_url", help="GitHub Projects v2 URL.")
    from_project.add_argument("--repo", required=True, help="Target repository as owner/name.")
    from_project.add_argument("--output", default="tasks-from-project.json", help="Output plan path.")
    from_project.add_argument("--label", default="whilly:ready", help="Label for created Issues.")

    watch = subcommands.add_parser("watch", help="Continuously watch Todo items.")
    watch.add_argument("project_url", help="GitHub Projects v2 URL.")
    watch.add_argument("--repo", required=True, help="Target repository as owner/name.")
    watch.add_argument("--output", default="tasks-from-project.json", help="Output plan path.")
    watch.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")

    sync_status = subcommands.add_parser("sync-status", help="Move a synced Project item to a status.")
    sync_status.add_argument("issue_number", type=int, help="GitHub Issue number recorded in sync state.")
    sync_status.add_argument("status", help="Project Status value, for example 'In Progress' or 'Done'.")

    subcommands.add_parser("status", help="Print local sync state.")
    subcommands.add_parser("reset-state", help="Reset local sync state.")
    return parser


def run_github_projects_command(argv: Sequence[str]) -> int:
    parser = build_github_projects_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code)

    sync_config = SyncConfig(sync_state_file=args.state_file)
    if getattr(args, "command", None) == "watch":
        sync_config.watch_interval = args.interval

    try:
        if args.command == "status":
            converter = GitHubProjectsConverter(sync_config=sync_config, check_gh_cli=False)
            sys.stdout.write(json.dumps(converter.get_sync_status(), indent=2, sort_keys=True) + "\n")
            return EXIT_OK
        if args.command == "reset-state":
            converter = GitHubProjectsConverter(sync_config=sync_config, check_gh_cli=False)
            converter.reset_sync_state()
            return EXIT_OK

        repo_owner: str | None = None
        repo_name: str | None = None
        if args.command in {"sync-todo", "from-project", "watch"}:
            repo_owner, repo_name = _parse_repo(args.repo)
        converter = GitHubProjectsConverter(sync_config=sync_config)
        if args.command == "sync-todo":
            stats = converter.sync_todo_items(
                args.project_url,
                repo_owner,
                repo_name,
                output_file=args.output,
                create_draft_issues=not args.existing_only,
            )
            sys.stdout.write(json.dumps(stats, indent=2, sort_keys=True) + "\n")
            return EXIT_OK
        if args.command == "from-project":
            converter.project_to_whilly_tasks(
                args.project_url,
                repo_owner,
                repo_name,
                output_file=args.output,
                label=args.label,
            )
            return EXIT_OK
        if args.command == "watch":
            converter.watch_project(args.project_url, repo_owner, repo_name, output_file=args.output)
            return EXIT_OK
        if args.command == "sync-status":
            return EXIT_OK if converter.sync_status_changes(args.issue_number, args.status) else EXIT_RUNTIME
    except RuntimeError as exc:
        sys.stderr.write(f"whilly github-projects: {exc}\n")
        return EXIT_RUNTIME

    parser.error(f"unknown command {args.command!r}")
    return EXIT_USAGE


def _parse_repo(repo_spec: str) -> tuple[str, str]:
    if "/" not in repo_spec:
        raise RuntimeError("--repo must be owner/name")
    owner, repo = repo_spec.split("/", 1)
    if not owner or not repo:
        raise RuntimeError("--repo must be owner/name")
    return owner, repo
