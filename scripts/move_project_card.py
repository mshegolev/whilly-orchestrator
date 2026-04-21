#!/usr/bin/env python3
"""Move a GitHub Projects v2 card to a named Status via `gh api graphql`.

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
import json
import os
import re
import subprocess
import sys


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return env


def _parse_project_url(url: str) -> tuple[str, str, int]:
    """Return (owner_login, owner_type, project_number) for a projects/v2 URL."""
    patterns = [
        (r"github\.com/users/([^/]+)/projects/(\d+)", "user"),
        (r"github\.com/orgs/([^/]+)/projects/(\d+)", "organization"),
    ]
    for pat, owner_type in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1), owner_type, int(m.group(2))
    raise SystemExit(f"Unsupported project URL: {url}")


def _gh_api(query: str, *fields: str) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}", *fields]
    proc = subprocess.run(args, capture_output=True, text=True, env=_gh_env())
    if proc.returncode != 0:
        raise SystemExit(f"gh api failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def fetch_project_metadata(owner: str, owner_type: str, number: int) -> dict:
    """Return {project_id, status_field_id, option_id_by_name, items: [{id, issue_number}]}."""
    query = (
        f"query($owner: String!, $number: Int!) {{"
        f"  {owner_type}(login: $owner) {{"
        f"    projectV2(number: $number) {{"
        f"      id"
        f"      fields(first: 50) {{"
        f"        nodes {{"
        f"          __typename"
        f"          ... on ProjectV2SingleSelectField {{"
        f"            id name options {{ id name }}"
        f"          }}"
        f"        }}"
        f"      }}"
        f"      items(first: 100) {{"
        f"        nodes {{"
        f"          id"
        f"          content {{ __typename ... on Issue {{ number repository {{ nameWithOwner }} }} }}"
        f"        }}"
        f"      }}"
        f"    }}"
        f"  }}"
        f"}}"
    )
    data = _gh_api(query, "-F", f"owner={owner}", "-F", f"number={number}")
    project = data["data"][owner_type]["projectV2"]
    status_field = next(
        (
            n
            for n in project["fields"]["nodes"]
            if n.get("name") == "Status" and n.get("__typename") == "ProjectV2SingleSelectField"
        ),
        None,
    )
    if not status_field:
        raise SystemExit("Project has no 'Status' single-select field.")
    option_id = {o["name"]: o["id"] for o in status_field["options"]}
    items = []
    for node in project["items"]["nodes"]:
        content = node.get("content") or {}
        if content.get("__typename") == "Issue":
            items.append(
                {
                    "item_id": node["id"],
                    "issue_number": content["number"],
                    "repo": content["repository"]["nameWithOwner"],
                }
            )
    return {
        "project_id": project["id"],
        "status_field_id": status_field["id"],
        "option_id_by_name": option_id,
        "items": items,
    }


def set_status(project_id: str, item_id: str, field_id: str, option_id: str) -> None:
    mutation = (
        "mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {"
        "  updateProjectV2ItemFieldValue("
        "    input: { projectId: $project, itemId: $item, fieldId: $field,"
        "             value: { singleSelectOptionId: $option } }"
        "  ) { projectV2Item { id } }"
        "}"
    )
    _gh_api(
        mutation,
        "-F",
        f"project={project_id}",
        "-F",
        f"item={item_id}",
        "-F",
        f"field={field_id}",
        "-F",
        f"option={option_id}",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("project_url")
    p.add_argument("issue_number", type=int)
    p.add_argument("status_name")
    p.add_argument("--repo", default=None, help="Optional owner/repo filter (disambiguates same-numbered issues).")
    args = p.parse_args(argv)

    owner, owner_type, number = _parse_project_url(args.project_url)
    meta = fetch_project_metadata(owner, owner_type, number)

    if args.status_name not in meta["option_id_by_name"]:
        print(
            f"✗ Status '{args.status_name}' not in project. Available: {list(meta['option_id_by_name'])}",
            file=sys.stderr,
        )
        return 2

    candidates = [i for i in meta["items"] if i["issue_number"] == args.issue_number]
    if args.repo:
        candidates = [i for i in candidates if i["repo"] == args.repo]
    if not candidates:
        print(f"✗ Issue #{args.issue_number} not found on the project board.", file=sys.stderr)
        return 3
    if len(candidates) > 1:
        print(
            f"✗ Issue #{args.issue_number} is on the board multiple times — pass --repo to disambiguate.",
            file=sys.stderr,
        )
        return 4

    item = candidates[0]
    set_status(
        meta["project_id"],
        item["item_id"],
        meta["status_field_id"],
        meta["option_id_by_name"][args.status_name],
    )
    print(f"✓ Moved {item['repo']}#{args.issue_number} → {args.status_name!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
