#!/usr/bin/env python3
"""Validate the explicit, immutable allowlist used to land the swarm foundation."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


# Selection is deliberately independent of Git's dirty-file inventory. Shared
# files are evidence of a source snapshot, not permission to copy every hunk.
PATH_GROUPS = {
    "source": (
        "whilly/swarm/**/*.py", "whilly/adapters/db/learning_*.py",
        "whilly/adapters/filesystem/knowledge_sources.py", "whilly/adapters/filesystem/memory_leases.py",
        "whilly/adapters/filesystem/swarm_*.py", "whilly/adapters/memory/**/*.py",
        "whilly/adapters/runner/swarm_*.py", "whilly/core/swarm_execution.py",
        "whilly/api/swarm_*.py", "whilly/api/product_swarm.py", "whilly/api/product_workflow.py",
        "whilly/cli/swarm.py", "whilly/cli/__init__.py", "whilly/adapters/transport/server.py",
        "whilly/api/templates/swarm.html.j2", "whilly/api/templates/product_swarm.html.j2",
        "whilly/api/static/product-swarm.js", "whilly/api/static/swarm-gantt.js",
    ),
    "owned-dependency": (
        "whilly/adapters/db/repository.py",
        "whilly/api/static/whilly-navigation.js", "whilly/api/static/whilly-theme.js",
        "whilly/api/templates/_keyboard_controls.html.j2", "whilly/api/templates/_theme_control.html.j2",
    ),
    "migration": tuple(f"whilly/adapters/db/migrations/versions/{number:03}_*.py" for number in range(29, 41)),
    "tests": (
        "tests/unit/test_swarm_*.py", "tests/unit/test_learning_*.py", "tests/unit/test_product_*.py",
        "tests/unit/test_collaboration_*.py", "tests/unit/test_proposal_*.py",
        "tests/unit/test_cognee_process.py", "tests/unit/test_memory_lookup.py",
        "tests/integration/test_swarm_*.py", "tests/integration/test_learning_*.py",
        "tests/integration/test_product_*.py", "tests/integration/test_guarded_swarm_runtime.py",
        "tests/integration/test_cognee_worker.py", "tests/integration/test_memory_leases.py",
        "tests/integration/test_alembic_full_chain.py", "tests/integration/swarm_fake_agent.py",
        "tests/swarm_helpers.py", "tests/local/test_swarm_*.py", "tests/*swarm*.test.cjs",
        "tests/collaboration_ui.test.cjs", "tests/proposals_ui.test.cjs",
        "tests/keyboard_navigation.test.cjs", "tests/theme.test.cjs",
    ),
    "docs": (
        "docs/Local-Swarm.md", "docs/Product-Swarm.md", "docs/Guarded-Swarm-Execution.md",
        "docs/Swarm-UI.md", "docs/Cognee-Memory.md", "docs/Workspace-Layout.md",
        "docs/superpowers/plans/2026-09-29-*swarm*.md",
        "docs/superpowers/plans/2026-09-29-cognee-adapter.md",
        "docs/superpowers/plans/2026-09-30-swarm-execution-guardrails.md",
        "docs/superpowers/specs/2026-09-29-cognee-adapter-design.md",
        "docs/superpowers/specs/2026-09-29-swarm-learning-design.md",
        "docs/superpowers/specs/2026-09-30-swarm-execution-guardrails-design.md",
    ),
    "openspec": (
        "openspec/changes/add-guarded-swarm-execution/**/*.md",
        "openspec/changes/add-swarm-learning/**/*.md",
        "openspec/changes/archive/2026-09-28-add-local-swarm-runtime/**/*.md",
        "openspec/changes/archive/2026-09-28-add-swarm-ui/**/*.md",
        "openspec/changes/archive/2026-09-29-*swarm*/**/*.md",
        "openspec/changes/archive/2026-09-29-add-cognee-memory/**/*.md",
        "openspec/changes/archive/2026-09-29-add-independent-candidate-git/**/*.md",
        "openspec/changes/archive/2026-09-29-add-mandatory-project-verification/**/*.md",
        "openspec/changes/archive/2026-09-29-add-session-organization/**/*.md",
        "openspec/changes/archive/2026-09-29-optional-subscription-profile-budget/**/*.md",
        "openspec/changes/archive/2026-09-29-add-keyboard-navigation/**/*.md",
        "openspec/changes/archive/2026-09-29-add-terminal-ui-themes/**/*.md",
        "openspec/specs/swarm-memory/**/*.md", "openspec/COVERAGE-MATRIX.md",
        "openspec/specs/agent-dispatch/spec.md", "openspec/specs/cli-surface/spec.md",
        "openspec/specs/orchestration-loop/spec.md", "openspec/specs/plan-json-contract/spec.md",
        "openspec/specs/web-status-ui/spec.md", "openspec/specs/auth-security/spec.md",
    ),
    "config": (".importlinter", "requirements/cognee-worker.txt"),
}
FORBIDDEN_PARTS = {"__pycache__", "graphify-out", "backups", "private", "out", "dist", "build"}


@dataclass(frozen=True)
class ManifestEntry:
    category: str
    path: str
    sha256: str


@dataclass(frozen=True)
class FoundationManifest:
    base_commit: str
    entries: tuple[ManifestEntry, ...]


def _check_path(name: str) -> None:
    parts = PurePosixPath(name).parts
    if (
        not name or name.startswith("/") or "\\" in name or ":" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(part in FORBIDDEN_PARTS or part.startswith(".") for part in parts if part != ".importlinter")
        or name.endswith((".pyc", ".pyo", ".pem", ".key"))
    ):
        raise ValueError(f"unsafe generated/private path: {name}")


def _check_category(category: str, name: str) -> None:
    prefixes = {
        "source": "whilly/", "owned-dependency": "whilly/", "migration": "whilly/adapters/db/migrations/versions/",
        "tests": "tests/", "docs": "docs/", "openspec": "openspec/",
    }
    if category == "config" and name in PATH_GROUPS["config"]:
        return
    if category not in prefixes or not name.startswith(prefixes[category]):
        raise ValueError(f"unclassified selected file: {category}: {name}")


def load_manifest(path: Path) -> FoundationManifest:
    """Load a Markdown table, rejecting ambiguous entries before reading source."""
    content = path.read_text(encoding="utf-8")
    bases = re.findall(r"^Base commit: `([0-9a-f]{7,40})`$", content, re.MULTILINE)
    if len(bases) != 1:
        raise ValueError("manifest must name exactly one base commit")
    entries = []
    seen = set()
    for line in content.splitlines():
        if not line.startswith("|"):
            continue
        fields = [field.strip().strip("`") for field in line.strip("|").split("|")]
        if fields == ["Category", "Path", "SHA-256"] or all(re.fullmatch(r"[-: ]+", field) for field in fields):
            continue
        if len(fields) != 3:
            raise ValueError("invalid manifest row")
        category, name, digest = fields
        _check_path(name)
        _check_category(category, name)
        if name in seen:
            raise ValueError(f"duplicate path: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA-256: {name}")
        seen.add(name)
        entries.append(ManifestEntry(category, name, digest))
    if not entries:
        raise ValueError("manifest contains no selected files")
    return FoundationManifest(bases[0], tuple(entries))


def selected_paths(source: Path) -> dict[str, str]:
    """Enumerate only explicit foundation capability groups, without generated files."""
    selected = {}
    for category, patterns in PATH_GROUPS.items():
        for pattern in patterns:
            for path in sorted(source.glob(pattern)):
                name = path.relative_to(source).as_posix()
                if not path.is_file() or set(PurePosixPath(name).parts) & FORBIDDEN_PARTS:
                    continue
                _check_path(name)
                if name in selected and selected[name] != category:
                    raise ValueError(f"ambiguous classification: {name}")
                selected[name] = category
    return selected


def _module_paths(module: str, source: Path) -> list[str]:
    stem = module.replace(".", "/")
    return [name for name in (f"{stem}.py", f"{stem}/__init__.py") if (source / name).is_file()]


def _imports(name: str, source: Path) -> set[str]:
    tree = ast.parse((source / name).read_text(encoding="utf-8"), filename=name)
    package = name.removesuffix(".py").split("/")[:-1]
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = ".".join(package[:len(package) - node.level + 1] + ([module] if module else []))
            if module:
                modules.add(module)
                for alias in node.names:
                    child = f"{module}.{alias.name}"
                    if _module_paths(child, source):
                        modules.add(child)
                    elif (
                        module.split(".")[0] in {"whilly", "tests"}
                        and (source / module.replace(".", "/")).is_dir()
                        and alias.name != "*"
                    ):
                        initializer = source / module.replace(".", "/") / "__init__.py"
                        exports = set()
                        if initializer.is_file():
                            for declaration in ast.walk(ast.parse(initializer.read_text(encoding="utf-8"))):
                                if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                    exports.add(declaration.name)
                                elif isinstance(declaration, ast.Name) and isinstance(declaration.ctx, ast.Store):
                                    exports.add(declaration.id)
                                elif isinstance(declaration, (ast.Import, ast.ImportFrom)):
                                    exports.update(item.asname or item.name.split(".")[0] for item in declaration.names)
                        if alias.name not in exports and "__getattr__" not in exports:
                            raise ValueError(f"missing imported module: {child} in {name}")
    return {module for module in modules if module.split(".")[0] in {"whilly", "tests"}}


def validate_manifest(manifest: FoundationManifest, source: Path) -> None:
    """Check hashes, import closure against the baseline, and capability completeness."""
    source = source.resolve()
    result = subprocess.run(
        ["git", "-C", str(source), "ls-tree", "-r", "--name-only", manifest.base_commit],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError("base commit unavailable in source")
    baseline = set(result.stdout.splitlines())
    selected = {entry.path for entry in manifest.entries}
    allowed = selected_paths(source)
    for entry in manifest.entries:
        if entry.path not in allowed:
            raise ValueError(f"manifest entry outside explicit allowlist: {entry.path}")
        if entry.category != allowed[entry.path]:
            raise ValueError(f"unclassified selected file: {entry.path}")
        path = source / entry.path
        if not path.is_file() or not path.resolve().is_relative_to(source) or path.is_symlink():
            raise ValueError(f"missing or unsafe source file: {entry.path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.sha256:
            raise ValueError(f"SHA-256 mismatch: {entry.path}")
        if path.suffix != ".py":
            continue
        for module in sorted(_imports(entry.path, source)):
            dependencies = _module_paths(module, source)
            if not dependencies and not (source / module.replace(".", "/")).is_dir():
                raise ValueError(f"missing imported module: {module} in {entry.path}")
            for name in dependencies:
                if name not in selected and name not in baseline:
                    raise ValueError(f"dependency absent from manifest: {name} imported by {entry.path}")
        # Package initializers can introduce their own imports on any import path.
        for parent in PurePosixPath(entry.path).parents:
            name = (parent / "__init__.py").as_posix()
            if (source / name).is_file() and name not in selected and name not in baseline:
                raise ValueError(f"dependency absent from manifest: {name}")
    for name in allowed:
        if name not in selected:
            raise ValueError(f"selected file absent from manifest: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest, args.source)
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: {len(manifest.entries)} selected files; base {manifest.base_commit}; no unclassified selected file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
