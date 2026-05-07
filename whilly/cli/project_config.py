"""``whilly project-config`` command surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from whilly.adapters.filesystem.plan_io import PlanParseError, parse_plan_dict
from whilly.core.scheduler import detect_cycles
from whilly.project_config import ProjectConfigError, build_plan_payload, load_project_config

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
EXIT_ENVIRONMENT_ERROR = 2


def build_project_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly project-config",
        description="Validate universal project configs and generate domain-adaptive Whilly plans.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    validate = sub.add_parser("validate", help="Validate a project config file.")
    validate.add_argument("config_file", help="Path to .json or .toml project config.")

    plan = sub.add_parser("plan", help="Generate canonical Whilly plan JSON from a project config.")
    plan.add_argument("config_file", help="Path to .json or .toml project config.")
    plan.add_argument("--plan-id", default=None, help="Override generated plan_id.")
    plan.add_argument("--out", default="-", help="Output plan JSON path, or '-' for stdout. Default: stdout.")
    return parser


def run_project_config_command(argv: Sequence[str]) -> int:
    parser = build_project_config_parser()
    args = parser.parse_args(list(argv))
    if args.action == "validate":
        return _run_validate(args)
    if args.action == "plan":
        return _run_plan(args)
    parser.error(f"unknown action {args.action!r}")
    return EXIT_VALIDATION_ERROR


def _run_validate(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(args.config_file)
        payload = build_plan_payload(config)
        plan, _tasks = parse_plan_dict(payload)
        cycles = detect_cycles(plan)
        if cycles:
            raise ProjectConfigError("generated plan has dependency cycle")
    except ProjectConfigError as exc:
        print(f"whilly project-config validate: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except PlanParseError as exc:
        print(f"whilly project-config validate: generated invalid plan: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(f"whilly project-config validate: OK ({args.config_file})", file=sys.stderr)
    return EXIT_OK


def _run_plan(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(args.config_file)
        payload = build_plan_payload(config, plan_id=args.plan_id)
        plan, _tasks = parse_plan_dict(payload)
        cycles = detect_cycles(plan)
        if cycles:
            raise ProjectConfigError("generated plan has dependency cycle")
    except ProjectConfigError as exc:
        print(f"whilly project-config plan: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except PlanParseError as exc:
        print(f"whilly project-config plan: generated invalid plan: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    _write_json(args.out, payload)
    return EXIT_OK


def _write_json(out: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if out == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"whilly project-config plan: wrote {out_path}", file=sys.stderr)


__all__ = ["build_project_config_parser", "run_project_config_command"]
