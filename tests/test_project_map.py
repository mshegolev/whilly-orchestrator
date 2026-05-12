"""Tests for project map configuration, loading, and resolution."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from whilly.project_config.loader import load_project_map, project_map_from_dict, ProjectConfigError
from whilly.project_config.models import ProjectMapConfig, ProjectMapEntry
from whilly.project_config.resolver import (
    find_matching_entry,
    match_label_filters,
    resolve_repositories,
    ProjectMapError,
)


class TestProjectMapModels:
    """Test ProjectMapEntry and ProjectMapConfig dataclasses."""

    def test_project_map_entry_creation(self) -> None:
        """Test creating a ProjectMapEntry."""
        entry = ProjectMapEntry(
            jira_project_key="ACME",
            git_repository_ids=("whilly-core", "whilly-workers"),
            issue_label_filters=("automation",),
        )
        assert entry.jira_project_key == "ACME"
        assert entry.git_repository_ids == ("whilly-core", "whilly-workers")
        assert entry.issue_label_filters == ("automation",)

    def test_project_map_entry_to_dict(self) -> None:
        """Test ProjectMapEntry.to_dict() serialization."""
        entry = ProjectMapEntry(
            jira_project_key="ACME",
            git_repository_ids=("repo1", "repo2"),
            custom_field_mappings={"field1": "value1"},
        )
        d = entry.to_dict()
        assert d["jira_project_key"] == "ACME"
        assert d["git_repository_ids"] == ("repo1", "repo2")
        assert d["custom_field_mappings"] == {"field1": "value1"}

    def test_project_map_config_creation(self) -> None:
        """Test creating a ProjectMapConfig."""
        entry = ProjectMapEntry(jira_project_key="ACME", git_repository_ids=("repo1",))
        config = ProjectMapConfig(
            version="1.0",
            mappings=(entry,),
            fallback_repo_ids=("default-repo",),
        )
        assert config.version == "1.0"
        assert len(config.mappings) == 1
        assert config.fallback_repo_ids == ("default-repo",)

    def test_project_map_config_to_dict(self) -> None:
        """Test ProjectMapConfig.to_dict() serialization."""
        entry = ProjectMapEntry(jira_project_key="ACME", git_repository_ids=("repo1",))
        config = ProjectMapConfig(mappings=(entry,))
        d = config.to_dict()
        assert d["version"] == "1.0"
        assert len(d["mappings"]) == 1
        assert d["mappings"][0]["jira_project_key"] == "ACME"


class TestProjectMapLoader:
    """Test project map loading from JSON and TOML."""

    def test_load_from_dict(self) -> None:
        """Test loading project map from dict."""
        raw = {
            "version": "1.0",
            "mappings": [
                {
                    "jira_project_key": "ACME",
                    "git_repository_ids": ["whilly-core"],
                }
            ],
        }
        config = project_map_from_dict(raw)
        assert len(config.mappings) == 1
        assert config.mappings[0].jira_project_key == "ACME"

    def test_load_from_json_file(self) -> None:
        """Test loading project map from JSON file."""
        raw = {
            "version": "1.0",
            "mappings": [
                {
                    "jira_project_key": "ACME",
                    "git_repository_ids": ["whilly-core"],
                }
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(raw, f)
            tmp_path = f.name

        try:
            config = load_project_map(tmp_path)
            assert len(config.mappings) == 1
        finally:
            Path(tmp_path).unlink()

    def test_load_invalid_file_path(self) -> None:
        """Test loading from non-existent file."""
        with pytest.raises(ProjectConfigError, match="cannot read project map"):
            load_project_map("/nonexistent/path.json")

    def test_load_invalid_json(self) -> None:
        """Test loading invalid JSON file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json}")
            tmp_path = f.name

        try:
            with pytest.raises(ProjectConfigError, match="not valid"):
                load_project_map(tmp_path)
        finally:
            Path(tmp_path).unlink()

    def test_load_missing_required_field(self) -> None:
        """Test that missing required fields raise error."""
        raw = {"version": "1.0", "mappings": [{"git_repository_ids": ["repo1"]}]}
        with pytest.raises(ProjectConfigError, match="jira_project_key"):
            project_map_from_dict(raw)


class TestProjectMapResolver:
    """Test project map resolution."""

    def test_find_matching_entry_exact_match(self) -> None:
        """Test finding entry with exact project key match."""
        entry1 = ProjectMapEntry(jira_project_key="ACME", git_repository_ids=("repo1",))
        entry2 = ProjectMapEntry(jira_project_key="DEMO", git_repository_ids=("repo2",))
        config = ProjectMapConfig(mappings=(entry1, entry2))

        result = find_matching_entry("ACME", [], config)
        assert result is not None
        assert result.jira_project_key == "ACME"

    def test_find_matching_entry_case_insensitive(self) -> None:
        """Test that project key matching is case-insensitive."""
        entry = ProjectMapEntry(jira_project_key="ACME", git_repository_ids=("repo1",))
        config = ProjectMapConfig(mappings=(entry,))

        result = find_matching_entry("acme", [], config)
        assert result is not None
        assert result.jira_project_key == "ACME"

    def test_find_matching_entry_with_labels(self) -> None:
        """Test matching entry with label filters."""
        entry = ProjectMapEntry(
            jira_project_key="DEMO",
            git_repository_ids=("repo1",),
            issue_label_filters=("critical", "high"),
        )
        config = ProjectMapConfig(mappings=(entry,))

        result = find_matching_entry("DEMO", ["critical"], config)
        assert result is not None

    def test_find_matching_entry_label_mismatch(self) -> None:
        """Test that entry with label filter doesn't match without the label."""
        entry = ProjectMapEntry(
            jira_project_key="DEMO",
            git_repository_ids=("repo1",),
            issue_label_filters=("critical",),
        )
        config = ProjectMapConfig(mappings=(entry,))

        result = find_matching_entry("DEMO", ["low"], config)
        assert result is None

    def test_match_label_filters_any_match(self) -> None:
        """Test label matching with any match."""
        assert match_label_filters(["critical", "bug"], ("critical",)) is True

    def test_match_label_filters_no_match(self) -> None:
        """Test label matching with no match."""
        assert match_label_filters(["low"], ("critical",)) is False

    def test_match_label_filters_empty_filters(self) -> None:
        """Test that empty filter list always matches."""
        assert match_label_filters(["any", "label"], ()) is True

    def test_resolve_repositories_exact_match(self) -> None:
        """Test resolving repositories for an issue."""
        entry = ProjectMapEntry(
            jira_project_key="ACME",
            git_repository_ids=("whilly-core", "whilly-workers"),
        )
        config = ProjectMapConfig(mappings=(entry,))
        issue = {
            "key": "ACME-100",
            "project": {"key": "ACME"},
            "labels": [],
        }

        repos = resolve_repositories(issue, config)
        assert repos == ("whilly-core", "whilly-workers")

    def test_resolve_repositories_fallback(self) -> None:
        """Test resolving to fallback repos when no match found."""
        config = ProjectMapConfig(fallback_repo_ids=("default-repo",))
        issue = {
            "key": "UNKNOWN-100",
            "project": {"key": "UNKNOWN"},
            "labels": [],
        }

        repos = resolve_repositories(issue, config)
        assert repos == ("default-repo",)

    def test_resolve_repositories_default_mapping(self) -> None:
        """Test resolving to default mapping when no exact match."""
        default_entry = ProjectMapEntry(
            jira_project_key="DEFAULT",
            git_repository_ids=("default-repo",),
        )
        config = ProjectMapConfig(default_mapping=default_entry)
        issue = {
            "key": "UNKNOWN-100",
            "project": {"key": "UNKNOWN"},
            "labels": [],
        }

        repos = resolve_repositories(issue, config)
        assert repos == ("default-repo",)

    def test_resolve_repositories_no_mapping_raises(self) -> None:
        """Test that resolving without mapping or fallback raises error."""
        config = ProjectMapConfig()
        issue = {
            "key": "UNKNOWN-100",
            "project": {"key": "UNKNOWN"},
            "labels": [],
        }

        with pytest.raises(ProjectMapError, match="No mapping found"):
            resolve_repositories(issue, config)

    def test_resolve_repositories_invalid_issue(self) -> None:
        """Test that invalid issue data raises error."""
        config = ProjectMapConfig()

        with pytest.raises(ProjectMapError, match="required field"):
            resolve_repositories({}, config)
