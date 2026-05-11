"""CLI for creating Whilly bug/idea feedback issues on GitHub."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from whilly.feedback import (
    FeedbackKind,
    GitHubIssueResult,
    build_feedback_body,
    create_github_issue,
    default_labels,
    default_repo,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly feedback",
        description="Create a GitHub issue with a Whilly bug or idea report.",
    )
    parser.add_argument("--kind", choices=("bug", "idea"), default="bug", help="Feedback kind. Default: bug.")
    parser.add_argument("--repo", default=None, help="GitHub repo as owner/repo. Defaults to WHILLY_FEEDBACK_REPO.")
    parser.add_argument("--title", required=True, help="Issue title.")
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--body", default="", help="Inline report body.")
    body.add_argument("--body-file", default=None, help="Path to markdown report body.")
    parser.add_argument("--command", default="", help="Command that failed or inspired the report.")
    parser.add_argument("--label", action="append", default=[], help="Extra label. Can be passed more than once.")
    parser.add_argument("--dry-run", action="store_true", help="Print the gh command without creating an issue.")
    return parser


def _read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    return str(args.body or "")


def run_feedback_command(
    argv: Sequence[str],
    *,
    creator: Callable[..., GitHubIssueResult] = create_github_issue,
    stdout: object | None = None,
    stderr: object | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    parser = _build_parser()
    args = parser.parse_args(list(argv))

    kind = FeedbackKind(args.kind)
    labels = (*default_labels(kind), *tuple(args.label))
    body = build_feedback_body(
        kind=kind,
        title=args.title,
        message=_read_body(args),
        command=args.command,
    )
    result = creator(
        repo=args.repo or default_repo(),
        title=args.title,
        body=body,
        labels=labels,
        dry_run=bool(args.dry_run),
    )
    if result.ok:
        if result.dry_run:
            out.write("Would create GitHub issue:\n")
            out.write(" ".join(result.command) + "\n")
        else:
            out.write(f"Created GitHub issue: {result.issue_url}\n")
        out.flush()
        return 0

    err.write(f"whilly feedback: failed to create GitHub issue: {result.reason}\n")
    err.flush()
    return result.returncode or 1
