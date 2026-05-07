"""``whilly compliance`` command surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from whilly.compliance import DEFAULT_DOC_ROOT, build_compliance_report, render_markdown

EXIT_OK = 0
EXIT_USER_ERROR = 1


def build_compliance_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly compliance",
        description="Generate deterministic repository compliance reports against the target Whilly docs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="Inspect the repository and write a compliance report.")
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--out", required=True, help="Output report path.")
    report.add_argument("--repo", default=".", help="Repository root to inspect. Default: current directory.")
    report.add_argument(
        "--docs",
        default=str(DEFAULT_DOC_ROOT),
        help=f"Target documentation archive directory. Default: {DEFAULT_DOC_ROOT}",
    )
    return parser


def run_compliance_command(argv: Sequence[str]) -> int:
    parser = build_compliance_parser()
    args = parser.parse_args(list(argv))
    if args.command == "report":
        return _run_report(args)
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USER_ERROR


def _run_report(args: argparse.Namespace) -> int:
    try:
        report = build_compliance_report(repo_root=Path(args.repo), doc_root=Path(args.docs))
        if args.format == "json":
            text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        else:
            text = render_markdown(report)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"whilly compliance report: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"whilly compliance report: wrote {args.out}", file=sys.stderr)
    return EXIT_OK


__all__ = ["build_compliance_parser", "run_compliance_command"]
