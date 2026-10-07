from pathlib import Path

import pytest

from whilly.swarm.bmad import BmadContextError, build_bmad_context
from whilly.swarm.registry import Registry


def registry(raw: dict) -> Registry:
    return Registry(name="test", overview="", projects={}, roles={}, raw=raw)


def write_skill(root: Path, name: str = "bmad-build", *, body: str = "Build instructions") -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")


def test_missing_bmad_config_is_named() -> None:
    with pytest.raises(BmadContextError, match="bmad config is missing"):
        build_bmad_context(registry({}))


def test_missing_skill_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(BmadContextError, match="workflow skill 'bmad-build' is missing"):
        build_bmad_context(registry({"bmad": {"skill_root": str(tmp_path), "workflow_skills": ["bmad-build"]}}))


def test_skill_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BmadContextError, match="outside configured skill root"):
        build_bmad_context(registry({"bmad": {"skill_root": str(tmp_path), "workflow_skills": ["../escape"]}}))


def test_context_budget_is_enforced(tmp_path: Path) -> None:
    write_skill(tmp_path, body="x" * 100)
    config = {"bmad": {"skill_root": str(tmp_path), "workflow_skills": ["bmad-build"], "max_context_chars": 20}}
    with pytest.raises(BmadContextError, match="context exceeds max_context_chars"):
        build_bmad_context(registry(config))


def test_valid_skill_and_referenced_resource_and_customization_are_loaded(tmp_path: Path) -> None:
    write_skill(tmp_path, body="# Build\n\nRead [rules](references/rules.md).")
    (tmp_path / "bmad-build" / "references").mkdir()
    (tmp_path / "bmad-build" / "references" / "rules.md").write_text("Actual rules", encoding="utf-8")
    customization = tmp_path / "custom.md"
    customization.write_text("Configured customization", encoding="utf-8")
    config = {
        "bmad": {
            "skill_root": str(tmp_path),
            "workflow_skills": ["bmad-build"],
            "customization": str(customization),
        }
    }
    context = build_bmad_context(registry(config))
    assert "# Build" in context
    assert "Actual rules" in context
    assert "Configured customization" in context


def test_explicit_bmad_paths_load_assets_scripts_and_customization_content(tmp_path: Path) -> None:
    write_skill(tmp_path, body="Use the configured BMAD files.")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "spec.md").write_text("Actual spec asset", encoding="utf-8")
    config = {
        "bmad": {
            "skill_root": str(tmp_path),
            "workflow_skills": ["bmad-build"],
            "resources": ["assets/spec.md"],
            "customization_content": "customize.toml: configured",
        }
    }
    context = build_bmad_context(registry(config))
    assert "Actual spec asset" in context
    assert "customize.toml: configured" in context
    assert "setup-blocked/unsupported" in context


def test_explicit_script_helper_requires_executor(tmp_path: Path) -> None:
    write_skill(tmp_path)
    script = tmp_path / "_bmad" / "scripts" / "resolve_config.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('never execute')", encoding="utf-8")
    config = {
        "bmad": {
            "skill_root": str(tmp_path),
            "workflow_skills": ["bmad-build"],
            "resources": ["_bmad/scripts/resolve_config.py"],
        }
    }
    with pytest.raises(BmadContextError, match="bmad_executor_required"):
        build_bmad_context(registry(config))


def test_script_dependent_skill_fails_closed_before_context_is_returned(tmp_path: Path) -> None:
    write_skill(tmp_path, body="This workflow requires _bmad/scripts/resolve_config.py.")
    with pytest.raises(BmadContextError, match="bmad_executor_required"):
        build_bmad_context(registry({"bmad": {"skill_root": str(tmp_path), "workflow_skills": ["bmad-build"]}}))
