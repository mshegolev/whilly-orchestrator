"""Reject incomplete or unsafe foundation snapshots before copying any source."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def load_checker():
    path = Path(__file__).resolve().parents[2] / "scripts/check-foundation-manifest.py"
    assert path.is_file(), "foundation manifest checker is absent"
    spec = importlib.util.spec_from_file_location("foundation_manifest", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def manifest_file(tmp_path, rows, base="271053d"):
    path = tmp_path / "manifest.md"
    path.write_text(
        f"# Foundation snapshot\n\nBase commit: `{base}`\n\n"
        "| Category | Path | SHA-256 |\n| --- | --- | --- |\n"
        + "".join(f"| {category} | `{name}` | `{digest}` |\n" for category, name, digest in rows)
    )
    return path


def git_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "README.md").write_text("Public fixture\n")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Example", "-c", "user.email=you@example.com",
         "commit", "-qm", "baseline"], check=True
    )
    base = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    return source, base


def source_file(source, name, content):
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return hashlib.sha256(content.encode()).hexdigest()


def test_duplicate_paths_are_rejected(tmp_path):
    checker = load_checker()
    row = ("source", "whilly/swarm/runtime.py", "a" * 64)
    with pytest.raises(ValueError, match="duplicate"):
        checker.load_manifest(manifest_file(tmp_path, [row, row]))


@pytest.mark.parametrize("name", [
    "whilly/swarm/__pycache__/runtime.pyc", ".whilly/registry.json", "backups/state.json",
    "graphify-out/graph.json", "whilly/graphify-out/graph.json", ".env", "private/registry.json",
    "/workspace/runtime.py", "../runtime.py", "whilly/../runtime.py", "whilly//runtime.py",
])
def test_generated_private_and_nonrelative_paths_are_rejected(tmp_path, name):
    checker = load_checker()
    with pytest.raises(ValueError, match="unsafe|generated|private"):
        checker.load_manifest(manifest_file(tmp_path, [("source", name, "a" * 64)]))


@pytest.mark.parametrize("category,name,digest", [
    ("unknown", "whilly/swarm/runtime.py", "a" * 64),
    ("tests", "whilly/swarm/runtime.py", "a" * 64),
    ("source", "whilly/swarm/runtime.py", "not-a-digest"),
])
def test_unclassified_paths_and_invalid_hashes_are_rejected(tmp_path, category, name, digest):
    checker = load_checker()
    with pytest.raises(ValueError):
        checker.load_manifest(manifest_file(tmp_path, [(category, name, digest)]))


def test_missing_imported_module_is_rejected(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    digest = source_file(source, name, "from whilly.swarm.missing import Coordinator\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="missing imported module.*whilly.swarm.missing"):
        checker.validate_manifest(manifest, source)


def test_omitted_new_dependency_is_rejected(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    digest = source_file(source, name, "from whilly.adapters.runner.new_launcher import launch\n")
    source_file(source, "whilly/adapters/runner/new_launcher.py", "def launch(): pass\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="dependency absent from manifest.*new_launcher"):
        checker.validate_manifest(manifest, source)


def test_relative_dependency_is_required(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    digest = source_file(source, name, "from . import plan\n")
    source_file(source, "whilly/swarm/plan.py", "class Plan: pass\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="dependency absent from manifest.*plan"):
        checker.validate_manifest(manifest, source)


def test_missing_relative_imported_module_is_rejected(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    digest = source_file(source, name, "from . import missing\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="missing imported module.*whilly.swarm.missing"):
        checker.validate_manifest(manifest, source)


def test_omitted_selected_nonpython_file_is_rejected(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    digest = source_file(source, name, "enabled = False\n")
    source_file(source, "whilly/api/static/product-swarm.js", "const enabled = false;\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="selected file absent from manifest.*product-swarm.js"):
        checker.validate_manifest(manifest, source)


def test_required_completion_hook_dependency_cannot_be_omitted_even_when_in_baseline(tmp_path):
    checker = load_checker()
    source, _ = git_source(tmp_path)
    dependency = "whilly/adapters/db/repository.py"
    source_file(source, dependency, "class TaskRepository: pass\n")
    subprocess.run(["git", "-C", str(source), "add", dependency], check=True)
    subprocess.run(
        [
            "git", "-C", str(source), "-c", "user.name=Example", "-c", "user.email=you@example.com",
            "commit", "-qm", "baseline repository",
        ],
        check=True,
    )
    base = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    name = "whilly/swarm/store.py"
    digest = source_file(source, name, "from whilly.adapters.db.repository import TaskRepository\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="selected file absent from manifest.*whilly/adapters/db/repository.py"):
        checker.validate_manifest(manifest, source)


@pytest.mark.parametrize("category,name,content", [
    ("docs", "docs/JEV-Decision-Layer.md", "# Standalone decision layer\n"),
    ("source", "whilly/core/decision_layer.py", "enabled = False\n"),
    ("docs", "docs/unrelated.md", "# Unrelated capability\n"),
    ("source", "whilly/quality/architecture.py", "enabled = False\n"),
])
def test_extra_excluded_capability_rows_are_rejected_even_with_valid_hash(tmp_path, category, name, content):
    checker = load_checker()
    source, base = git_source(tmp_path)
    runtime = "whilly/swarm/runtime.py"
    runtime_digest = source_file(source, runtime, "enabled = False\n")
    extra_digest = source_file(source, name, content)
    rows = [("source", runtime, runtime_digest), (category, name, extra_digest)]
    manifest = checker.load_manifest(manifest_file(tmp_path, rows, base))
    with pytest.raises(ValueError, match="outside explicit allowlist"):
        checker.validate_manifest(manifest, source)


def test_allowed_dependency_cannot_be_reclassified_as_source(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/api/static/whilly-theme.js"
    digest = source_file(source, name, "const theme = 'dark';\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="unclassified selected file"):
        checker.validate_manifest(manifest, source)


def test_source_symlink_is_rejected(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("enabled = False\n")
    name = "whilly/swarm/runtime.py"
    link = source / name
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, digest)], base))
    with pytest.raises(ValueError, match="unsafe source file"):
        checker.validate_manifest(manifest, source)


def test_source_hash_mismatch_is_rejected(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    source_file(source, name, "changed = True\n")
    manifest = checker.load_manifest(manifest_file(tmp_path, [("source", name, "a" * 64)], base))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        checker.validate_manifest(manifest, source)


def test_cli_accepts_complete_snapshot_and_rejects_modified_source(tmp_path):
    checker = load_checker()
    source, base = git_source(tmp_path)
    name = "whilly/swarm/runtime.py"
    digest = source_file(source, name, "enabled = False\n")
    path = manifest_file(tmp_path, [("source", name, digest)], base)
    command = [sys.executable, checker.__file__, "--source", str(source), "--manifest", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout
    source_file(source, name, "enabled = True\n")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
