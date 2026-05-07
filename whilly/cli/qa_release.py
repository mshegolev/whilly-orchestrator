"""``whilly qa-release`` command surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from whilly.qa_release import build_test_plan, collect_release_context
from whilly.qa_release.autotest_writer import write_autotest_suite
from whilly.qa_release.models import qa_test_plan_from_dict, release_context_from_dict

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2


def build_qa_release_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly qa-release",
        description="Collect QA release-verification context, build test plans, and scaffold autotests.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser(
        "collect",
        help="Fetch a Jira release ticket, linked issues, remote links, and repo hints.",
    )
    collect.add_argument("jira_ref", help="Jira key or browse URL, e.g. PROJ-123.")
    collect.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Linked Jira issue depth to fetch. Current collector supports 0 or 1. Default: 1.",
    )
    collect.add_argument("--timeout", type=int, default=15, help="Per Jira HTTP request timeout in seconds.")
    collect.add_argument("--out", default="-", help="Output JSON path, or '-' for stdout. Default: stdout.")

    plan = sub.add_parser(
        "plan",
        help="Convert release-context JSON into an actionable QA test-plan JSON.",
    )
    plan.add_argument("context_json", help="Path to release-context JSON produced by `qa-release collect`.")
    plan.add_argument("--out", default="-", help="Output test-plan JSON path, or '-' for stdout. Default: stdout.")

    scaffold = sub.add_parser(
        "scaffold-tests",
        help="Generate/update a pytest release contract suite in the test monorepo.",
    )
    scaffold.add_argument("test_plan_json", help="Path to QA test-plan JSON produced by `qa-release plan`.")
    scaffold.add_argument("--repo", required=True, help="Path to the autotest monorepo root.")
    scaffold.add_argument("--suite", required=True, help="ETL suite name, e.g. sales_etl.")
    scaffold.add_argument(
        "--out",
        default=None,
        help="Output path. Relative paths are resolved under --repo. Defaults to bigdata_tests/<suite>/tests/...",
    )
    scaffold.add_argument("--force", action="store_true", help="Overwrite a non-generated existing test file.")
    return parser


def run_qa_release_command(argv: Sequence[str]) -> int:
    parser = build_qa_release_parser()
    args = parser.parse_args(list(argv))
    if args.command == "collect":
        return _run_collect(args)
    if args.command == "plan":
        return _run_plan(args)
    if args.command == "scaffold-tests":
        return _run_scaffold_tests(args)
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USER_ERROR


def _run_collect(args: argparse.Namespace) -> int:
    if args.depth < 0 or args.depth > 1:
        print("whilly qa-release collect: --depth currently supports only 0 or 1", file=sys.stderr)
        return EXIT_USER_ERROR
    try:
        context = collect_release_context(args.jira_ref, depth=args.depth, timeout=args.timeout)
    except RuntimeError as exc:
        print(f"whilly qa-release collect: {exc}", file=sys.stderr)
        return EXIT_ENV_ERROR if "unconfigured" in str(exc).lower() else EXIT_USER_ERROR
    except ValueError as exc:
        print(f"whilly qa-release collect: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    payload = json.dumps(context.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out == "-":
        sys.stdout.write(payload)
        sys.stdout.flush()
    else:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        print(f"whilly qa-release collect: wrote {out_path}", file=sys.stderr)
    return EXIT_OK


def _run_plan(args: argparse.Namespace) -> int:
    try:
        context = release_context_from_dict(_read_json(Path(args.context_json)))
        plan = build_test_plan(context)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"whilly qa-release plan: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    _write_json_arg(args.out, plan.to_dict())
    return EXIT_OK


def _run_scaffold_tests(args: argparse.Namespace) -> int:
    try:
        plan = qa_test_plan_from_dict(_read_json(Path(args.test_plan_json)))
        path = write_autotest_suite(
            plan,
            repo_root=args.repo,
            suite=args.suite,
            out_path=args.out,
            force=args.force,
        )
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError, TypeError) as exc:
        print(f"whilly qa-release scaffold-tests: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"whilly qa-release scaffold-tests: wrote {path}", file=sys.stderr)
    return EXIT_OK


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _write_json_arg(out: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if out == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"whilly qa-release: wrote {out_path}", file=sys.stderr)


__all__ = ["build_qa_release_parser", "run_qa_release_command"]
