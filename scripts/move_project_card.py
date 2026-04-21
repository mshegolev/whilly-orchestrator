#!/usr/bin/env python3
"""Move a GitHub Projects v2 card to a named Status via the whilly client.

Usage:
    python3 scripts/move_project_card.py <project_url> <issue_number> "<status_name>" [--repo owner/repo]

Example:
    python3 scripts/move_project_card.py \\
        https://github.com/users/mshegolev/projects/4 \\
        162 "In Progress" --repo mshegolev/whilly-orchestrator

Requires `gh` CLI logged in with the `project` scope:
    gh auth refresh -s project
"""

from __future__ import annotations

import argparse
import sys

from whilly.project_board import ProjectBoardClient


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("project_url")
    p.add_argument("issue_number", type=int)
    p.add_argument("status_name")
    p.add_argument("--repo", default=None, help="Optional owner/repo filter (disambiguates same-numbered issues).")
    args = p.parse_args(argv)

    client = ProjectBoardClient(args.project_url, default_repo=args.repo)
    ok = client.set_issue_status(args.issue_number, args.repo, args.status_name)
    if ok:
        target = f"{args.repo}#" if args.repo else "#"
        print(f"✓ Moved {target}{args.issue_number} → {args.status_name!r}")
        return 0
    print(f"✗ Could not move issue #{args.issue_number} to {args.status_name!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
