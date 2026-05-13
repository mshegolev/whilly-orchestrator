"""``whilly skill`` command surface for MCP/skill discovery.

Phase 5 of the Jira Scheduler integration (TASK-SCH-044).

Discovers skills and MCP servers available in the current environment:
- ~/.claude/skills/*/SKILL.md (local Claude Code skills)
- MCP profiles configured in whilly.toml or via env
- Built-in MCP tools registered via MCPRegistry
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1


def build_skill_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly skill",
        description="Discover available skills and MCP servers.",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")

    p_list = sub.add_parser("list", help="List all discovered skills and MCP tools.")
    p_list.add_argument(
        "--skills-dir",
        default=None,
        help="Override skills directory (default: ~/.claude/skills).",
    )
    p_list.add_argument(
        "--source",
        choices=["all", "skills", "mcp"],
        default="all",
        help="Filter by source: all|skills|mcp.",
    )
    p_list.add_argument("--json", action="store_true", help="Output as JSON.")

    p_show = sub.add_parser("show", help="Show details for one skill.")
    p_show.add_argument("name", help="Skill name or MCP tool name.")
    p_show.add_argument("--skills-dir", default=None, help="Override skills directory.")
    p_show.add_argument("--json", action="store_true", help="Output as JSON.")

    return parser


def run_skill_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_skill_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.action == "list":
        return _run_list(args)
    if args.action == "show":
        return _run_show(args)
    parser.error(f"unknown action {args.action!r}")
    return EXIT_ERROR


def _discover_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """Discover skills under skills_dir. Each subdir with SKILL.md is a skill."""
    if not skills_dir.exists():
        return []

    skills: list[dict[str, Any]] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            # Single-file skill (legacy: ~/.claude/skills/<name>.md)
            if entry.suffix == ".md":
                skills.append(
                    {
                        "name": entry.stem,
                        "source": "skill",
                        "path": str(entry),
                        "description": _read_skill_description(entry),
                        "trigger": "",
                    }
                )
            continue

        skill_md = entry / "SKILL.md"
        if skill_md.exists():
            description = _read_skill_description(skill_md)
            trigger = _read_skill_trigger(skill_md)
            skills.append(
                {
                    "name": entry.name,
                    "source": "skill",
                    "path": str(entry),
                    "description": description,
                    "trigger": trigger,
                }
            )
    return skills


def _read_skill_description(path: Path) -> str:
    """Read first non-empty paragraph after the title from a SKILL.md."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    lines = text.split("\n")
    in_paragraph = False
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_paragraph:
                break
            continue
        if not stripped:
            if in_paragraph:
                break
            continue
        in_paragraph = True
        paragraph.append(stripped)
    return " ".join(paragraph)[:200]


def _read_skill_trigger(path: Path) -> str:
    """Look for Trigger: line in SKILL.md."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.split("\n"):
        stripped = line.strip()
        # Match "Trigger:" or "**Trigger:**"
        lower = stripped.lower()
        for prefix in ("trigger:", "**trigger:**", "trigger —", "trigger:"):
            if lower.startswith(prefix):
                return stripped[len(prefix) :].strip(" *:`")
    return ""


def _discover_mcp_tools() -> list[dict[str, Any]]:
    """Discover MCP tools and profiles from registry."""
    try:
        from whilly.mcp import get_profile_registry, get_registry
    except ImportError:
        return []

    items: list[dict[str, Any]] = []
    registry = get_registry()
    for tool in registry.list_tools():
        items.append(
            {
                "name": getattr(tool, "name", ""),
                "source": "mcp",
                "description": getattr(tool, "description", ""),
                "category": getattr(tool, "category", ""),
            }
        )

    profile_registry = get_profile_registry()
    for profile in profile_registry.list_profiles():
        items.append(
            {
                "name": f"profile:{profile.name}",
                "source": "mcp_profile",
                "description": profile.description,
                "tools": profile.tools,
            }
        )

    return items


def _resolve_skills_dir(args: argparse.Namespace) -> Path:
    """Resolve skills directory from --skills-dir or default."""
    if args.skills_dir:
        return Path(args.skills_dir)
    return Path(os.path.expanduser("~/.claude/skills"))


def _run_list(args: argparse.Namespace) -> int:
    """List all skills and MCP tools."""
    items: list[dict[str, Any]] = []

    if args.source in ("all", "skills"):
        items.extend(_discover_skills(_resolve_skills_dir(args)))

    if args.source in ("all", "mcp"):
        items.extend(_discover_mcp_tools())

    if args.json:
        print(json.dumps({"items": items, "count": len(items)}, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not items:
        print("No skills or MCP tools discovered.")
        return EXIT_OK

    print(f"Discovered {len(items)} item(s):\n")
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)

    for source, group in sorted(by_source.items()):
        print(f"[{source}] ({len(group)})")
        for item in group:
            desc = item.get("description", "") or ""
            trigger = item.get("trigger", "")
            line = f"  - {item['name']}"
            if desc:
                line += f" — {desc[:80]}"
            if trigger:
                line += f"  (trigger: {trigger})"
            print(line)
        print()

    return EXIT_OK


def _run_show(args: argparse.Namespace) -> int:
    """Show details for a specific skill or MCP tool."""
    skills_dir = _resolve_skills_dir(args)
    skill_path = skills_dir / args.name / "SKILL.md"
    if skill_path.exists():
        text = skill_path.read_text(encoding="utf-8", errors="replace")
        if args.json:
            print(json.dumps({"name": args.name, "path": str(skill_path), "content": text}, indent=2))
        else:
            print(f"Skill: {args.name}")
            print(f"Path:  {skill_path}")
            print("---")
            print(text)
        return EXIT_OK

    # Try single-file skill
    single = skills_dir / f"{args.name}.md"
    if single.exists():
        text = single.read_text(encoding="utf-8", errors="replace")
        if args.json:
            print(json.dumps({"name": args.name, "path": str(single), "content": text}, indent=2))
        else:
            print(f"Skill: {args.name}\nPath:  {single}\n---\n{text}")
        return EXIT_OK

    # Try MCP profile
    try:
        from whilly.mcp import get_profile_registry

        profile = get_profile_registry().get_profile(args.name)
        if profile:
            if args.json:
                print(json.dumps(profile.to_dict(), indent=2))
            else:
                print(f"MCP Profile: {profile.name}")
                print(f"Description: {profile.description}")
                print(f"Tools: {', '.join(profile.tools)}")
            return EXIT_OK
    except ImportError:
        pass

    print(f"whilly skill: not found: {args.name}", file=sys.stderr)
    return EXIT_ERROR
