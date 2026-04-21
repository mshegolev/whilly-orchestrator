#!/usr/bin/env python3
"""Bulk-add open repo issues to a GitHub Projects v2 board.

Useful once after enabling board sync — brings a backlog of issues onto the
board so whilly has columns to move them across. Idempotent: issues already
on the board are skipped.

Usage:
    python3 scripts/populate_board.py \\
        --project https://github.com/users/you/projects/4 \\
        --repo you/your-repo \\
        [--label whilly:ready] \\
        [--limit 200] \\
        [--dry-run]

Requires the `gh` CLI authenticated with the `project` scope:
    gh auth refresh -s project
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from whilly.gh_utils import gh_subprocess_env


def _gh_api(query: str, **variables: object) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args.extend(["-F", f"{key}={value}"])
    proc = subprocess.run(args, capture_output=True, text=True, env=gh_subprocess_env())
    if proc.returncode != 0:
        raise SystemExit(f"gh graphql failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def _parse_project_url(url: str) -> tuple[str, str, int]:
    import re

    for pattern, owner_type in (
        (r"github\.com/users/([^/]+)/projects/(\d+)", "user"),
        (r"github\.com/orgs/([^/]+)/projects/(\d+)", "organization"),
    ):
        m = re.search(pattern, url)
        if m:
            return m.group(1), owner_type, int(m.group(2))
    raise SystemExit(f"Unsupported project URL: {url!r}")


def fetch_project_state(project_url: str) -> tuple[str, set[tuple[str, int]]]:
    """Return (project_id, set of (repo, issue_number) already on the board).

    Paginates the Projects v2 ``items`` connection (GitHub caps each page at 100)
    so the return is a complete snapshot of issue-backed items on the board.
    """
    owner, owner_type, number = _parse_project_url(project_url)
    existing: set[tuple[str, int]] = set()
    project_id: str | None = None
    cursor: str | None = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        data = _gh_api(
            f"query($owner: String!, $number: Int!) {{"
            f"  {owner_type}(login: $owner) {{"
            f"    projectV2(number: $number) {{"
            f"      id"
            f"      items(first: 100{after}) {{"
            f"        pageInfo {{ hasNextPage endCursor }}"
            f"        nodes {{ content {{ __typename ... on Issue {{ number repository {{ nameWithOwner }} }} }} }}"
            f"      }}"
            f"    }}"
            f"  }}"
            f"}}",
            owner=owner,
            number=number,
        )
        project = data["data"][owner_type]["projectV2"]
        project_id = project["id"]
        for node in project["items"]["nodes"]:
            content = node.get("content") or {}
            if content.get("__typename") == "Issue":
                existing.add((content["repository"]["nameWithOwner"], content["number"]))
        page = project["items"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    assert project_id is not None
    return project_id, existing


def fetch_repo_issues(repo: str, label: str | None, limit: int) -> list[dict]:
    """Return a list of ``{id, number, title}`` for open issues matching the filter.

    Paginates through ``issues`` in 100-item chunks (GitHub cap) and stops once
    ``limit`` total issues have been collected.
    """
    owner, name = repo.split("/", 1)
    label_filter = f', labels: ["{label}"]' if label else ""
    collected: list[dict] = []
    cursor: str | None = None
    while len(collected) < limit:
        batch = min(100, limit - len(collected))
        after = f', after: "{cursor}"' if cursor else ""
        data = _gh_api(
            f"query($owner: String!, $name: String!, $batch: Int!) {{"
            f"  repository(owner: $owner, name: $name) {{"
            f"    issues(first: $batch, states: [OPEN]{label_filter}{after}) {{"
            f"      pageInfo {{ hasNextPage endCursor }}"
            f"      nodes {{ id number title }}"
            f"    }}"
            f"  }}"
            f"}}",
            owner=owner,
            name=name,
            batch=batch,
        )
        conn = data["data"]["repository"]["issues"]
        collected.extend(conn["nodes"])
        page = conn["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    return collected


def add_issue_to_project(project_id: str, issue_node_id: str) -> str:
    data = _gh_api(
        "mutation($project: ID!, $content: ID!) {"
        "  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {"
        "    item { id }"
        "  }"
        "}",
        project=project_id,
        content=issue_node_id,
    )
    return data["data"]["addProjectV2ItemById"]["item"]["id"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True, help="Project v2 URL.")
    p.add_argument("--repo", required=True, help="owner/name of the repo to pull issues from.")
    p.add_argument("--label", default=None, help="Optional GitHub label to filter issues by.")
    p.add_argument("--limit", type=int, default=200, help="Max open issues to fetch (default 200).")
    p.add_argument("--dry-run", action="store_true", help="Print what would be added, don't mutate.")
    args = p.parse_args(argv)

    print(f"→ Project: {args.project}")
    project_id, existing = fetch_project_state(args.project)
    print(f"  already on board: {len(existing)}")

    print(f"→ Repo: {args.repo}  label={args.label or '<any>'}  limit={args.limit}")
    issues = fetch_repo_issues(args.repo, args.label, args.limit)
    print(f"  open issues matching: {len(issues)}")

    to_add = [i for i in issues if (args.repo, i["number"]) not in existing]
    print(f"→ Missing from board: {len(to_add)}")

    if not to_add:
        print("Nothing to do.")
        return 0

    if args.dry_run:
        for issue in to_add[:20]:
            print(f"   would add #{issue['number']}: {issue['title']}")
        if len(to_add) > 20:
            print(f"   … and {len(to_add) - 20} more")
        return 0

    added = 0
    for issue in to_add:
        try:
            add_issue_to_project(project_id, issue["id"])
            added += 1
            if added % 10 == 0 or added == len(to_add):
                print(f"   added {added}/{len(to_add)}…")
        except SystemExit as exc:
            print(f"✗ failed on #{issue['number']}: {exc}", file=sys.stderr)
            return 1
    print(f"✓ Added {added} issues to the board.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
