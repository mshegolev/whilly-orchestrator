"""``whilly rollback`` command surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from whilly.rollback.git_ops import GitClient, RollbackError
from whilly.rollback.models import PreflightReport, RestoreResult, RollbackPoint
from whilly.rollback.service import (
    build_preflight_report,
    confirmation_phrase,
    create_rollback_point,
    list_rollback_points,
    restore_to_ref,
)

__all__ = ["EXIT_BLOCKED", "EXIT_OK", "EXIT_USAGE", "build_rollback_parser", "run_rollback_command"]

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2


def build_rollback_parser() -> argparse.ArgumentParser:
    """Build the rollback subcommand parser."""
    parser = argparse.ArgumentParser(
        prog="whilly rollback",
        description="Create rollback points, run Git preflight checks, and restore with exact confirmation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create an annotated Whilly rollback point at HEAD.")
    create.add_argument("--repo", default=".", help="Git repository path.")
    create.add_argument("--operation", choices=("manual", "push", "merge", "restore"), default="manual")
    create.add_argument("--message", default=None, help="Annotated tag message.")
    create.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    list_parser = subparsers.add_parser("list", help="List Whilly rollback points.")
    list_parser.add_argument("--repo", default=".", help="Git repository path.")
    list_parser.add_argument("--branch", default=None, help="Only show points for this branch.")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    preflight = subparsers.add_parser("preflight", help="Run rollback safety preflight checks.")
    preflight.add_argument("operation", choices=("push", "merge", "restore"))
    preflight.add_argument("--repo", default=".", help="Git repository path.")
    preflight.add_argument("--target", default=None, help="Target branch or ref.")
    preflight.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    restore = subparsers.add_parser("restore", help="Restore to a tag or ref with exact confirmation.")
    restore.add_argument("target", help="Rollback tag or Git ref to restore.")
    restore.add_argument("--repo", default=".", help="Git repository path.")
    restore.add_argument("--dry-run", action="store_true", help="Show restore evidence without resetting HEAD.")
    restore.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    restore.add_argument("--confirm", default=None, help="Exact confirmation phrase.")

    return parser


def run_rollback_command(argv: Sequence[str]) -> int:
    """Run ``whilly rollback`` with ``argv`` excluding the top-level command name."""
    parser = build_rollback_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    try:
        if args.command == "create":
            return _run_create(args)
        if args.command == "list":
            return _run_list(args)
        if args.command == "preflight":
            return _run_preflight(args)
        if args.command == "restore":
            return _run_restore(args)
    except RollbackError as exc:
        print(f"rollback {args.command}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    parser.print_usage(sys.stderr)
    return EXIT_USAGE


def _run_create(args: argparse.Namespace) -> int:
    point = create_rollback_point(args.repo, operation=args.operation, message=args.message)
    if args.json:
        _print_json(point.to_dict())
    else:
        _print_rollback_point(point)
    return EXIT_OK


def _run_list(args: argparse.Namespace) -> int:
    points = list_rollback_points(args.repo, branch=args.branch)
    if args.json:
        _print_json([point.to_dict() for point in points])
    else:
        _print_rollback_points(points)
    return EXIT_OK


def _run_preflight(args: argparse.Namespace) -> int:
    report = build_preflight_report(args.repo, operation=args.operation, target_ref=args.target)
    if args.json:
        _print_json(report.to_dict())
    else:
        _print_preflight_report(report)
    return EXIT_OK if report.ok else EXIT_BLOCKED


def _run_restore(args: argparse.Namespace) -> int:
    try:
        report = build_preflight_report(args.repo, operation="restore", target_ref=args.target)
        target_sha = _resolve_target_sha(report, args.target)
        expected = confirmation_phrase(report, target_sha)
    except RollbackError as exc:
        print(f"rollback restore: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run:
        return _run_restore_dry_run(args, expected)

    confirm = args.confirm
    if confirm is None:
        if not sys.stdin.isatty():
            print("rollback restore: confirmation required", file=sys.stderr)
            return EXIT_BLOCKED
        confirm = input(f"Type '{expected}' to continue: ")

    try:
        result = restore_to_ref(args.repo, _peel_target_ref(args.target), confirm=confirm)
    except RollbackError as exc:
        print(f"rollback restore: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.json:
        _print_json(_restore_payload(result, expected, args.target))
    else:
        _print_restore_result(result, expected, args.target)
    return EXIT_OK


def _run_restore_dry_run(args: argparse.Namespace, expected: str) -> int:
    try:
        result = restore_to_ref(args.repo, _peel_target_ref(args.target), confirm=expected, dry_run=True)
    except RollbackError as exc:
        print(f"rollback restore: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.json:
        _print_json(_restore_payload(result, expected, args.target))
    else:
        _print_restore_result(result, expected, args.target)
    return EXIT_OK


def _resolve_target_sha(report: PreflightReport, target: str) -> str:
    return GitClient(report.worktree.repo_root).require("rev-parse", _peel_target_ref(target)).strip()


def _peel_target_ref(target: str) -> str:
    return target if target.endswith("^{}") else f"{target}^{{}}"


def _restore_payload(result: RestoreResult, expected: str, requested_target: str) -> dict[str, object]:
    payload = result.to_dict()
    payload["confirmation_phrase"] = expected
    payload["target_ref"] = requested_target
    return payload


def _print_json(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True))


def _print_rollback_point(point: RollbackPoint) -> None:
    print(f"created rollback point: {point.name}")
    print(f"branch: {point.branch}")
    print(f"HEAD: {point.target_sha}")
    print(f"operation/message: {point.message or ''}")


def _print_rollback_points(points: Sequence[RollbackPoint]) -> None:
    if not points:
        print("no rollback points found")
        return
    for point in points:
        print(f"{point.name}")
        print(f"  branch: {point.branch}")
        print(f"  HEAD: {point.target_sha}")
        print(f"  message: {point.message or ''}")


def _print_preflight_report(report: PreflightReport) -> None:
    print(f"operation: {report.operation}")
    print(f"branch: {report.worktree.branch or '(detached)'}")
    print(f"HEAD: {report.worktree.head_sha}")
    print(f"dirty: {'yes' if report.worktree.dirty else 'no'}")
    print(f"backup points: {len(report.backup_points)}")
    print(f"protection: {report.protection.status} {report.protection.reason}".rstrip())
    _print_lines("blockers", report.blockers)
    _print_lines("warnings", report.warnings)


def _print_restore_result(result: RestoreResult, expected: str, requested_target: str) -> None:
    print(f"operation: restore")
    print(f"branch: {result.branch or '(detached)'}")
    print(f"HEAD: {result.preflight.worktree.head_sha}")
    print(f"target: {requested_target} ({result.target_sha})")
    print(f"dirty: {'yes' if result.preflight.worktree.dirty else 'no'}")
    print(f"backup points: {len(result.preflight.backup_points)}")
    print(f"confirmation phrase: {expected}")
    print(f"reset performed: {'yes' if result.reset_performed else 'no'}")
    print(f"message: {result.message}")
    _print_lines("blockers", result.preflight.blockers)
    _print_lines("warnings", result.preflight.warnings)


def _print_lines(label: str, values: Sequence[str]) -> None:
    print(f"{label}:")
    if not values:
        print("  none")
        return
    for value in values:
        print(f"  - {value}")
