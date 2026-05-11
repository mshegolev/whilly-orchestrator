"""Tests for GitHub Projects integration."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from whilly.github_projects import GitHubProjectsConverter, ProjectItem, SyncConfig
from whilly.config import WhillyConfig


@pytest.fixture
def mock_config():
    """Mock Whilly config."""
    config = Mock(spec=WhillyConfig)
    return config


@pytest.fixture
def sync_config():
    """Test sync configuration."""
    with tempfile.TemporaryDirectory() as temp_dir:
        sync_file = Path(temp_dir) / "test_sync_state.json"
        config = SyncConfig(target_statuses={"Todo"}, sync_state_file=str(sync_file))
        yield config


@pytest.fixture
def converter(mock_config, sync_config):
    """GitHub Projects converter instance."""
    with patch.object(GitHubProjectsConverter, "_check_gh_cli"):
        return GitHubProjectsConverter(config=mock_config, sync_config=sync_config)


class TestProjectItem:
    """Test ProjectItem data class."""

    def test_project_item_defaults(self):
        """Test ProjectItem with minimal data."""
        item = ProjectItem(id="test-id", title="Test Title")

        assert item.id == "test-id"
        assert item.title == "Test Title"
        assert item.body == ""
        assert item.status == "Todo"
        assert item.priority == "medium"
        assert item.labels == []
        assert item.assignee is None
        assert item.url is None

    def test_project_item_whilly_label_mapping(self):
        """Test status to whilly label mapping."""
        test_cases = [
            ("Todo", "whilly:ready"),
            ("In Progress", "whilly:in-progress"),
            ("Review", "whilly:review"),
            ("Done", "whilly:done"),
            ("Backlog", "whilly:backlog"),
            ("Unknown Status", "whilly:ready"),  # default
        ]

        for status, expected_label in test_cases:
            item = ProjectItem(id="test", title="Test", status=status)
            assert item.whilly_label == expected_label


class TestSyncConfig:
    """Test SyncConfig data class."""

    def test_default_sync_config(self):
        """Test default sync configuration."""
        config = SyncConfig()

        assert config.target_statuses == {"Todo"}
        assert "Todo" in config.status_mapping
        assert "whilly:ready" in config.reverse_mapping
        assert config.watch_interval == 60

    def test_custom_sync_config(self):
        """Test custom sync configuration."""
        custom_statuses = {"Todo", "In Progress"}
        config = SyncConfig(target_statuses=custom_statuses, watch_interval=30)

        assert config.target_statuses == custom_statuses
        assert config.watch_interval == 30


class TestGitHubProjectsConverter:
    """Test GitHubProjectsConverter class."""

    @patch("subprocess.run")
    def test_check_gh_cli_accepts_auth_status_success(self, mock_run):
        """Test _check_gh_cli accepts a successful gh auth status probe."""
        mock_run.return_value = Mock(stdout="Logged in to github.com as test", stderr="", returncode=0)

        GitHubProjectsConverter(config=Mock(spec=WhillyConfig), check_gh_cli=True)

        assert mock_run.call_args.args[0] == ["gh", "auth", "status"]

    @patch("subprocess.run")
    def test_check_gh_cli_falls_back_to_api_probe(self, mock_run):
        """Test _check_gh_cli accepts usable API auth even if gh auth status is stale."""
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, ["gh", "auth", "status"], stderr="token invalid"),
            Mock(stdout="test\n", stderr="", returncode=0),
        ]

        GitHubProjectsConverter(config=Mock(spec=WhillyConfig), check_gh_cli=True)

        assert mock_run.call_args_list[1].args[0] == ["gh", "api", "user", "--jq", ".login"]

    def test_sync_state_loading(self, converter):
        """Test sync state loading and saving."""
        # Initially empty
        assert converter._sync_state["last_sync"] is None
        assert converter._sync_state["synced_items"] == {}

        # Save state
        converter._sync_state["test_key"] = "test_value"
        converter._save_sync_state()

        # Create new converter with same sync config
        with patch.object(GitHubProjectsConverter, "_check_gh_cli"):
            new_converter = GitHubProjectsConverter(config=converter.config, sync_config=converter.sync_config)

        assert new_converter._sync_state["test_key"] == "test_value"

    def test_parse_project_url_user_format(self, converter):
        """Test parsing user project URLs."""
        url = "https://github.com/users/mshegolev/projects/4"
        result = converter.parse_project_url(url)

        assert result["owner"] == "mshegolev"
        assert result["project_number"] == 4
        assert result["type"] == "user"
        assert result["repo"] is None

    def test_parse_project_url_org_format(self, converter):
        """Test parsing org project URLs."""
        url = "https://github.com/orgs/myorg/projects/5"
        result = converter.parse_project_url(url)

        assert result["owner"] == "myorg"
        assert result["project_number"] == 5
        assert result["type"] == "org"

    def test_parse_project_url_repo_format(self, converter):
        """Test parsing repo project URLs."""
        url = "https://github.com/mshegolev/whilly-orchestrator/projects/3"
        result = converter.parse_project_url(url)

        assert result["owner"] == "mshegolev"
        assert result["repo"] == "whilly-orchestrator"
        assert result["project_number"] == 3
        assert result["type"] == "repo"

    def test_parse_project_url_with_view(self, converter):
        """Test parsing project URL with view parameter."""
        url = "https://github.com/users/mshegolev/projects/4/views/1"
        result = converter.parse_project_url(url)

        assert result["owner"] == "mshegolev"
        assert result["project_number"] == 4
        assert result["type"] == "user"

    def test_parse_project_url_invalid(self, converter):
        """Test parsing invalid project URLs."""
        with pytest.raises(ValueError, match="Invalid GitHub Project URL"):
            converter.parse_project_url("https://example.com/invalid")

    @patch("subprocess.run")
    def test_fetch_project_items_with_filter(self, mock_run, converter):
        """Test fetching project items with status filter."""
        # Mock GraphQL response
        mock_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "id": "item1",
                                    "updatedAt": "2024-01-01T00:00:00Z",
                                    "fieldValues": {"nodes": [{"name": "Todo", "field": {"name": "Status"}}]},
                                    "content": {"title": "Test Item 1", "body": "Test body 1"},
                                },
                                {
                                    "id": "item2",
                                    "updatedAt": "2024-01-01T00:00:00Z",
                                    "fieldValues": {"nodes": [{"name": "Done", "field": {"name": "Status"}}]},
                                    "content": {"title": "Test Item 2", "body": "Test body 2"},
                                },
                            ]
                        }
                    }
                }
            }
        }

        mock_run.return_value = Mock(stdout=json.dumps(mock_response), returncode=0)

        # Fetch all items
        all_items = converter.fetch_project_items("https://github.com/users/test/projects/1", include_updated_at=True)
        assert len(all_items) == 2

        # Fetch only Todo items
        todo_items = converter.fetch_project_items(
            "https://github.com/users/test/projects/1", filter_statuses={"Todo"}, include_updated_at=True
        )
        assert len(todo_items) == 1
        assert todo_items[0].status == "Todo"
        assert todo_items[0].title == "Test Item 1"

    @patch("subprocess.run")
    @patch("whilly.sources.github_issues.fetch_github_issues")
    def test_sync_todo_items(self, mock_fetch_issues, mock_run, converter):
        """Test syncing Todo items."""
        # Mock project items response
        mock_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "id": "item1",
                                    "updatedAt": "2024-01-01T00:00:00Z",
                                    "fieldValues": {"nodes": [{"name": "Todo", "field": {"name": "Status"}}]},
                                    "content": {"title": "New Todo Item", "body": "Test body"},
                                }
                            ]
                        }
                    }
                }
            }
        }

        # Mock issue creation response
        issue_url = "https://github.com/test/repo/issues/123"

        mock_run.side_effect = [
            Mock(stdout=json.dumps(mock_response), returncode=0),  # GraphQL query
            Mock(stdout=issue_url, returncode=0),  # Issue creation
        ]

        mock_fetch_issues.return_value = ("tasks.json", {"created": 1})

        # Run sync
        stats = converter.sync_todo_items("https://github.com/users/test/projects/1", "test", "repo")

        assert stats["created_count"] == 1
        assert stats["total_todo_items"] == 1

    @patch("subprocess.run")
    @patch("whilly.sources.github_issues.fetch_github_issues")
    def test_sync_todo_items_existing_only_skips_drafts(self, mock_fetch_issues, mock_run, converter):
        """Test existing-only sync records Issues but does not create Issues for draft items."""
        mock_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "id": "draft-item",
                                    "updatedAt": "2024-01-01T00:00:00Z",
                                    "fieldValues": {"nodes": [{"name": "Todo", "field": {"name": "Status"}}]},
                                    "content": {"title": "Draft Todo", "body": "Draft body"},
                                },
                                {
                                    "id": "issue-item",
                                    "updatedAt": "2024-01-01T00:00:00Z",
                                    "fieldValues": {"nodes": [{"name": "Todo", "field": {"name": "Status"}}]},
                                    "content": {
                                        "title": "Existing Issue",
                                        "body": "Issue body",
                                        "number": 245,
                                        "url": "https://github.com/test/repo/issues/245",
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }
        mock_run.return_value = Mock(stdout=json.dumps(mock_response), returncode=0)

        stats = converter.sync_todo_items(
            "https://github.com/users/test/projects/1",
            "test",
            "repo",
            create_draft_issues=False,
        )

        assert stats == {
            "created_count": 0,
            "skipped_count": 2,
            "synced_count": 0,
            "total_todo_items": 2,
        }
        assert converter._sync_state["synced_items"] == {
            "issue-item:Todo": {
                "issue_number": 245,
                "issue_url": "https://github.com/test/repo/issues/245",
                "last_sync": converter._sync_state["synced_items"]["issue-item:Todo"]["last_sync"],
            }
        }
        assert mock_run.call_count == 1
        mock_fetch_issues.assert_not_called()

    def test_get_sync_status(self, converter):
        """Test getting sync status."""
        # Set some state
        converter._sync_state.update(
            {
                "last_sync": "2024-01-01T00:00:00Z",
                "project_url": "https://github.com/users/test/projects/1",
                "repo_owner": "test",
                "repo_name": "repo",
                "synced_items": {"item1:Todo": {"issue_number": 123}},
            }
        )

        status = converter.get_sync_status()

        assert status["last_sync"] == "2024-01-01T00:00:00Z"
        assert status["project_url"] == "https://github.com/users/test/projects/1"
        assert status["repo"] == "test/repo"
        assert status["total_synced_items"] == 1

    def test_reset_sync_state(self, converter):
        """Test resetting sync state."""
        # Set some state
        converter._sync_state["test_key"] = "test_value"

        # Reset
        converter.reset_sync_state()

        assert converter._sync_state["last_sync"] is None
        assert converter._sync_state["synced_items"] == {}
        assert "test_key" not in converter._sync_state

    @patch("subprocess.run")
    def test_convert_items_to_issues_skip_existing(self, mock_run, converter):
        """Test that existing issues are skipped during conversion."""
        items = [
            ProjectItem(id="item1", title="New Item", body="New body"),
            ProjectItem(
                id="item2", title="Existing Issue", body="Existing body", url="https://github.com/test/repo/issues/123"
            ),
        ]

        # Mock issue creation only for new item
        mock_run.return_value = Mock(stdout="https://github.com/test/repo/issues/124", returncode=0)

        result = converter.convert_items_to_issues(items, "test", "repo")

        # Should only create one issue (skip the existing one)
        assert len(result) == 1
        assert result[0]["title"] == "New Item"

        # Only one subprocess call for issue creation
        assert mock_run.call_count == 1

    def test_sync_status_changes_no_project_info(self, converter):
        """Test sync_status_changes fails without project info."""
        result = converter.sync_status_changes(123, "In Progress")
        assert result is False

    @patch.object(GitHubProjectsConverter, "_update_project_item_status", return_value=True)
    def test_sync_status_changes_restores_project_info_from_state(self, mock_update, converter):
        """Test sync_status_changes can run in a fresh process after sync_todo_items saved state."""
        converter._sync_state.update(
            {
                "project_url": "https://github.com/users/test/projects/1",
                "synced_items": {"PVTI_item:Todo": {"issue_number": 123}},
            }
        )

        result = converter.sync_status_changes(123, "In Progress")

        assert result is True
        assert converter._project_info == {
            "owner": "test",
            "project_number": 1,
            "type": "user",
            "repo": None,
        }
        mock_update.assert_called_once_with("PVTI_item", "In Progress")

    def test_sync_status_changes_item_not_found(self, converter):
        """Test sync_status_changes with unknown issue."""
        converter._project_info = {"test": "data"}
        result = converter.sync_status_changes(999, "In Progress")
        assert result is False

    @patch("subprocess.run")
    def test_sync_status_changes_updates_project_v2_status(self, mock_run, converter):
        """Test sync_status_changes updates a Project v2 single-select Status field."""
        converter._project_info = {
            "owner": "test",
            "project_number": 1,
            "type": "user",
            "repo": None,
        }
        converter._sync_state["synced_items"] = {"PVTI_item:Todo": {"issue_number": 123}}
        fields_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "id": "PVT_project",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "PVTSSF_status",
                                    "name": "Status",
                                    "options": [
                                        {"id": "todo-option", "name": "Todo"},
                                        {"id": "done-option", "name": "Done"},
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
        }
        mutation_response = {
            "data": {
                "updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": "PVTI_item"},
                }
            }
        }
        mock_run.side_effect = [
            Mock(stdout=json.dumps(fields_response), returncode=0),
            Mock(stdout=json.dumps(mutation_response), returncode=0),
        ]

        result = converter.sync_status_changes(123, "Done")

        assert result is True
        assert mock_run.call_count == 2
        field_query = mock_run.call_args_list[0].args[0]
        mutation = mock_run.call_args_list[1].args[0]
        assert "fields(first: 50)" in field_query[4]
        assert "updateProjectV2ItemFieldValue" in mutation[4]
        assert "-F" in mutation
        assert "projectId=PVT_project" in mutation
        assert "itemId=PVTI_item" in mutation
        assert "fieldId=PVTSSF_status" in mutation
        assert "optionId=done-option" in mutation

    @patch("subprocess.run")
    def test_sync_status_changes_unknown_project_status_fails(self, mock_run, converter):
        """Test sync_status_changes fails if the Project has no matching Status option."""
        converter._project_info = {
            "owner": "test",
            "project_number": 1,
            "type": "user",
            "repo": None,
        }
        converter._sync_state["synced_items"] = {"PVTI_item:Todo": {"issue_number": 123}}
        fields_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "id": "PVT_project",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "PVTSSF_status",
                                    "name": "Status",
                                    "options": [{"id": "todo-option", "name": "Todo"}],
                                }
                            ]
                        },
                    }
                }
            }
        }
        mock_run.return_value = Mock(stdout=json.dumps(fields_response), returncode=0)

        result = converter.sync_status_changes(123, "Done")

        assert result is False
        assert mock_run.call_count == 1


class TestStatusMapping:
    """Test status mapping functionality."""

    def test_status_to_label_mapping(self):
        """Test various status to label mappings."""
        config = SyncConfig()

        test_cases = [
            ("Todo", "whilly:ready"),
            ("In Progress", "whilly:in-progress"),
            ("Review", "whilly:review"),
            ("Done", "whilly:done"),
            ("Backlog", "whilly:backlog"),
        ]

        for status, expected_label in test_cases:
            assert config.status_mapping[status] == expected_label

    def test_reverse_mapping(self):
        """Test reverse mapping from labels to statuses."""
        config = SyncConfig()

        test_cases = [
            ("whilly:ready", "Todo"),
            ("whilly:in-progress", "In Progress"),
            ("whilly:review", "Review"),
            ("whilly:done", "Done"),
            ("whilly:backlog", "Backlog"),
        ]

        for label, expected_status in test_cases:
            assert config.reverse_mapping[label] == expected_status


if __name__ == "__main__":
    pytest.main([__file__])
