"""Load explicitly configured BMAD instructions for the read-only planner."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from whilly.swarm.registry import Registry

__all__ = ["BmadContextError", "build_bmad_context"]

_DEFAULT_MAX_CONTEXT_CHARS = 100_000
_MAX_RESOURCES = 64
_MARKDOWN_LINK = re.compile(r"!?(?:\[[^\]]*\])\(([^)\s]+)\)")


class BmadContextError(ValueError):
    """Named, operator-actionable failure while loading configured BMAD context."""


def _under_root(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BmadContextError(f"{label} is outside configured skill root: {path}") from exc
    return resolved


def _read_file(path: Path, *, label: str, root: Path) -> str:
    safe = _under_root(path, root, label=label)
    if not safe.is_file():
        raise BmadContextError(f"{label} is missing: {path}")
    try:
        return safe.read_text(encoding="utf-8")
    except OSError as exc:
        raise BmadContextError(f"cannot read {label} {path}: {exc}") from exc


def _resource_paths(skill_file: Path, body: str) -> list[Path]:
    paths: list[Path] = []
    for target in _MARKDOWN_LINK.findall(body):
        if target.startswith(("#", "/", "http:", "https:", "mailto:")):
            continue
        paths.append(skill_file.parent / target.split("#", 1)[0])
    return paths


def _configured_resources(config: dict[str, Any], root: Path) -> list[Path]:
    raw_resources = config.get("resources", [])
    if not isinstance(raw_resources, list) or not all(isinstance(item, str) and item for item in raw_resources):
        raise BmadContextError("bmad resources must be a list of relative paths")
    if len(raw_resources) > _MAX_RESOURCES:
        raise BmadContextError(f"bmad resources exceed maximum {_MAX_RESOURCES}")
    return [_under_root(root / item, root, label=f"configured BMAD resource '{item}'") for item in raw_resources]


def _config(registry: Registry) -> dict[str, Any]:
    config = registry.raw.get("bmad")
    if not isinstance(config, dict):
        raise BmadContextError("bmad config is missing; configure raw['bmad']")
    if not isinstance(config.get("skill_root"), str) or not config["skill_root"].strip():
        raise BmadContextError("bmad skill_root is missing")
    skills = config.get("workflow_skills")
    if not isinstance(skills, list) or not skills or not all(isinstance(item, str) and item for item in skills):
        raise BmadContextError("bmad workflow_skills must be a non-empty list")
    return config


def build_bmad_context(registry: Registry, *, allow_executor: bool = False) -> str:
    """Return actual configured BMAD instructions, bounded and read-only.

    Only named workflow skills are loaded.  Required markdown resources are
    followed relative to each skill's ``SKILL.md``; no directory scan or
    arbitrary path lookup is performed.
    """
    config = _config(registry)
    root = Path(config["skill_root"]).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise BmadContextError(f"configured skill root is missing: {root}")
    max_chars = config.get("max_context_chars", _DEFAULT_MAX_CONTEXT_CHARS)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise BmadContextError("bmad max_context_chars must be a positive integer")
    isolated_executor = allow_executor and config.get("mode") == "host-spec"

    chunks: list[str] = [
        "## BMAD adapter boundary\n"
        "This is read-only instruction loading for a headless planner. The host persists returned artifacts; "
        "the planner does not execute BMAD scripts or edit worktrees. Script-dependent instructions are "
        "setup-blocked/unsupported until the host provides an isolated executor."
    ]
    loaded: set[Path] = set()

    def load_resource(path: Path, *, label: str, recursive: bool = True) -> None:
        safe = _under_root(path, root, label=label)
        if safe in loaded:
            return
        body = _read_file(safe, label=label, root=root)
        loaded.add(safe)
        if len(loaded) > _MAX_RESOURCES:
            raise BmadContextError(f"recursive BMAD resources exceed maximum {_MAX_RESOURCES}")
        chunks.append(f"## BMAD resource: {safe.relative_to(root)}\n{body}")
        if recursive:
            for child in _resource_paths(safe, body):
                load_resource(child, label=f"resource referenced by '{safe.relative_to(root)}'")

    for resource in _configured_resources(config, root):
        if "_bmad/scripts/" in resource.relative_to(root).as_posix() and not isolated_executor:
            raise BmadContextError("bmad_executor_required: configured BMAD script helper needs an isolated executor")
        load_resource(resource, label=f"configured BMAD resource '{resource.relative_to(root)}'")

    for skill_name in config["workflow_skills"]:
        skill_dir = _under_root(root / skill_name, root, label=f"workflow skill '{skill_name}'")
        skill_file = skill_dir / "SKILL.md"
        body = _read_file(skill_file, label=f"workflow skill '{skill_name}'", root=root)
        if ("_bmad/scripts/" in body or "bmad/scripts/" in body) and not isolated_executor:
            raise BmadContextError(
                f"bmad_executor_required: workflow skill '{skill_name}' requires BMAD script helpers"
            )
        chunks.append(f"## BMAD skill: {skill_name}\n{body}")
        for resource in _resource_paths(skill_file, body):
            load_resource(resource, label=f"resource referenced by workflow skill '{skill_name}'")

    customization = config.get("customization")
    if customization is not None:
        if not isinstance(customization, str) or not customization:
            raise BmadContextError("bmad customization must be a path string")
        customization_path = Path(customization).expanduser()
        load_resource(customization_path, label="bmad customization")
    customization_content = config.get("customization_content")
    if customization_content is not None:
        if not isinstance(customization_content, str):
            raise BmadContextError("bmad customization_content must be text")
        chunks.append("## BMAD customization content\n" + customization_content)

    context = "\n\n".join(chunks)
    total = len(context)
    if total > max_chars:
        raise BmadContextError(f"context exceeds max_context_chars ({total} > {max_chars})")
    return context
