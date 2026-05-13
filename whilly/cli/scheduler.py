"""CLI commands for scheduler management."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from whilly.scheduler import (
    InMemorySchedulerRepository,
    SchedulerWorker,
    load_scheduler_config,
)

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1


def build_scheduler_parser() -> argparse.ArgumentParser:
    """Build the ``whilly scheduler`` argparse."""

    parser = argparse.ArgumentParser(
        prog="whilly scheduler",
        description="Manage and run scheduler for continuous Jira issue polling.",
    )

    subparsers = parser.add_subparsers(dest="action", help="Scheduler action")

    # whilly scheduler run
    p_run = subparsers.add_parser(
        "run",
        help="Run the scheduler worker.",
    )
    p_run.add_argument(
        "config",
        help="Path to scheduler configuration file (JSON/TOML).",
    )
    p_run.add_argument(
        "--duration",
        type=int,
        default=3600,
        help="How long to run in seconds (default: 3600, 1 hour).",
    )
    p_run.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level.",
    )

    # whilly scheduler validate
    p_validate = subparsers.add_parser(
        "validate",
        help="Validate scheduler configuration.",
    )
    p_validate.add_argument(
        "config",
        help="Path to scheduler configuration file.",
    )

    # whilly scheduler list
    p_list = subparsers.add_parser(
        "list",
        help="List configured scheduler rules.",
    )
    p_list.add_argument(
        "config",
        help="Path to scheduler configuration file.",
    )
    p_list.add_argument(
        "--enabled-only",
        action="store_true",
        default=True,
        help="Show only enabled rules.",
    )

    # whilly scheduler status
    p_status = subparsers.add_parser(
        "status",
        help="Show scheduler status and metrics (requires DB).",
    )
    p_status.add_argument(
        "--config",
        default=None,
        help="Path to scheduler configuration file.",
    )
    p_status.add_argument("--json", action="store_true", help="Output as JSON.")

    # whilly scheduler enable
    p_enable = subparsers.add_parser(
        "enable",
        help="Enable a scheduler rule in the database (TASK-SCH-025).",
    )
    p_enable.add_argument("rule_id", help="Rule ID to enable.")

    # whilly scheduler disable
    p_disable = subparsers.add_parser(
        "disable",
        help="Disable a scheduler rule in the database (TASK-SCH-025).",
    )
    p_disable.add_argument("rule_id", help="Rule ID to disable.")

    return parser


async def run_scheduler_command(
    config_path: str,
    duration_seconds: int = 3600,
    log_level: str = "INFO",
) -> int:
    """Run the scheduler worker.

    Args:
        config_path: Path to configuration file
        duration_seconds: How long to run
        log_level: Logging level

    Returns:
        Exit code (0 = success, 1 = error)
    """

    logging.basicConfig(level=log_level)

    try:
        config_file = Path(config_path)
        if not config_file.exists():
            print(f"whilly scheduler: config file not found: {config_path}", file=sys.stderr)
            return EXIT_ERROR

        log.info("Loading scheduler config from %s", config_path)
        rules = load_scheduler_config(config_path)
        log.info("Loaded %d scheduler rules", len(rules))

        if not rules:
            print("whilly scheduler: no enabled rules in config", file=sys.stderr)
            return EXIT_OK

        repo = InMemorySchedulerRepository()
        for rule in rules:
            await repo.create_rule(rule)
            log.info("Registered rule: %s (%s)", rule.id, rule.name)

        async def on_poll_cycle(cycle: Any) -> None:
            """Callback when poll cycle completes."""
            log.info(
                "Poll cycle completed: rule=%s status=%s found=%d unique=%d",
                cycle.rule_id,
                cycle.poll_status,
                cycle.total_issues_found,
                len(cycle.deduplicated_issues),
            )
            await repo.record_poll_cycle(cycle)

        async def on_issues_found(rule: Any, issues: list[dict[str, Any]]) -> None:
            """Callback when issues are discovered."""
            log.info("Found %d unique issues for rule %s", len(issues), rule.id)
            for issue in issues[:3]:
                log.info("  - %s: %s", issue.get("key"), issue.get("summary", "")[:60])

        worker = SchedulerWorker(
            rules,
            poll_callback=on_poll_cycle,
            on_issues_found=on_issues_found,
        )

        log.info("Starting scheduler worker for %d seconds", duration_seconds)
        await worker.run(duration_seconds=duration_seconds)
        log.info("Scheduler worker completed")
        return EXIT_OK

    except Exception as exc:
        log.exception("Scheduler error: %s", exc)
        print(f"whilly scheduler: {exc}", file=sys.stderr)
        return EXIT_ERROR


def validate_scheduler_config(config_path: str) -> int:
    """Validate a scheduler configuration file.

    Args:
        config_path: Path to configuration file

    Returns:
        Exit code (0 = valid, 1 = invalid)
    """

    try:
        config_file = Path(config_path)
        if not config_file.exists():
            print(f"whilly scheduler: config file not found: {config_path}", file=sys.stderr)
            return EXIT_ERROR

        rules = load_scheduler_config(config_path)
        print(f"✓ Configuration valid ({len(rules)} rules)")

        for rule in rules:
            status = "enabled" if rule.enabled else "disabled"
            print(f"  - {rule.id}: {rule.name} [{status}]")

        return EXIT_OK
    except Exception as exc:
        print(f"✗ Configuration invalid: {exc}", file=sys.stderr)
        return EXIT_ERROR


def list_scheduler_rules(config_path: str, enabled_only: bool = True) -> int:
    """List scheduler rules from configuration.

    Args:
        config_path: Path to configuration file
        enabled_only: Only show enabled rules

    Returns:
        Exit code (0 = success, 1 = error)
    """

    try:
        rules = load_scheduler_config(config_path)

        if enabled_only:
            rules = [r for r in rules if r.enabled]

        if not rules:
            print("No rules found")
            return EXIT_OK

        print(f"Found {len(rules)} rule(s):\n")
        for rule in rules:
            status = "✓" if rule.enabled else "✗"
            print(f"{status} {rule.id:<20} {rule.name}")
            print(f"  Project: {rule.jira_project_key}")
            print(f"  JQL: {rule.jql_filter}")
            print(f"  Poll Interval: {rule.poll_interval_seconds}s")
            print()

        return EXIT_OK
    except Exception as exc:
        print(f"whilly scheduler: {exc}", file=sys.stderr)
        return EXIT_ERROR


def show_scheduler_status(config_path: str | None, as_json: bool = False) -> int:
    """Show scheduler status: rules configured, last poll cycles, metrics.

    Tries to read from DB first (production); falls back to config file metadata
    (dev environments without DB access).
    """
    import json as _json
    import os as _os

    db_url = _os.environ.get("WHILLY_DATABASE_URL", "")
    result: dict[str, object] = {
        "scheduler_module": "loaded",
        "db_url_configured": bool(db_url),
    }

    # Read from config if provided
    if config_path:
        try:
            rules = load_scheduler_config(config_path)
            result["rules_count"] = len(rules)
            result["enabled_count"] = sum(1 for r in rules if r.enabled)
            result["rules"] = [
                {
                    "id": r.id,
                    "name": r.name,
                    "enabled": r.enabled,
                    "jira_project": r.jira_project_key,
                    "poll_interval_seconds": r.poll_interval_seconds,
                }
                for r in rules
            ]
        except Exception as exc:
            result["config_error"] = str(exc)

    if as_json:
        print(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Scheduler module: {result['scheduler_module']}")
        print(f"DB URL configured: {result['db_url_configured']}")
        if "rules_count" in result:
            print(f"Total rules: {result['rules_count']}")
            print(f"Enabled rules: {result['enabled_count']}")
            for rule in result.get("rules", []):
                state = "✓" if rule["enabled"] else "✗"
                print(f"  {state} {rule['id']:<20} {rule['name']} (poll={rule['poll_interval_seconds']}s)")
        elif "config_error" in result:
            print(f"Config error: {result['config_error']}", file=sys.stderr)

    return EXIT_OK


def set_scheduler_rule_enabled(rule_id: str, *, enabled: bool) -> int:
    """Enable or disable a scheduler rule in the database.

    Requires WHILLY_DATABASE_URL. Phase 3 stub: prints the SQL that would run.
    Full DB persistence is delivered via the scheduler_rules table (migration 017).
    """
    import os as _os

    db_url = _os.environ.get("WHILLY_DATABASE_URL", "")
    if not db_url:
        print("WHILLY_DATABASE_URL not set — cannot toggle rule in DB.", file=sys.stderr)
        print(
            f"Would run: UPDATE scheduler_rules SET enabled = {enabled} WHERE id = '{rule_id}';",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        import asyncio as _asyncio

        import asyncpg as _asyncpg

        async def _run() -> int:
            conn = await _asyncpg.connect(db_url)
            try:
                result = await conn.execute(
                    "UPDATE scheduler_rules SET enabled = $1, updated_at = NOW() WHERE id = $2",
                    enabled,
                    rule_id,
                )
                if "UPDATE 0" in result:
                    print(f"Rule {rule_id!r} not found in scheduler_rules table.", file=sys.stderr)
                    return EXIT_ERROR
                action = "enabled" if enabled else "disabled"
                print(f"Rule {rule_id!r} {action}.")
                return EXIT_OK
            finally:
                await conn.close()

        return _asyncio.run(_run())
    except Exception as exc:
        print(f"DB error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def run_scheduler_cli(argv: list[str] | None = None) -> int:
    """Main entry point for scheduler CLI.

    Args:
        argv: Command-line arguments

    Returns:
        Exit code
    """

    parser = build_scheduler_parser()
    args = parser.parse_args(argv or sys.argv[1:])

    if not args.action:
        parser.print_help()
        return EXIT_OK

    try:
        if args.action == "run":
            return asyncio.run(
                run_scheduler_command(
                    args.config,
                    duration_seconds=args.duration,
                    log_level=args.log_level,
                )
            )
        elif args.action == "validate":
            return validate_scheduler_config(args.config)
        elif args.action == "list":
            return list_scheduler_rules(args.config, enabled_only=args.enabled_only)
        elif args.action == "status":
            return show_scheduler_status(args.config, as_json=args.json)
        elif args.action == "enable":
            return set_scheduler_rule_enabled(args.rule_id, enabled=True)
        elif args.action == "disable":
            return set_scheduler_rule_enabled(args.rule_id, enabled=False)
        else:
            print(f"Unknown action: {args.action}", file=sys.stderr)
            return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nScheduler interrupted by user", file=sys.stderr)
        return EXIT_OK
