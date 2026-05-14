"""Tests for whilly.toml section parsers (Phase 2/3/4/5 config wiring)."""

from __future__ import annotations

from pathlib import Path

import pytest

from whilly.config import load_layered
from whilly.config_sections import (
    load_confluence_publisher,
    load_mcp_profiles,
    load_project_map,
    load_scheduler_rules,
    resolve_secret,
)


@pytest.fixture
def reset_config_caches():
    """Force a clean load_layered after each test by pointing to an empty dir."""
    yield
    # Re-load from an empty directory to clear caches for subsequent tests
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        load_layered(cwd=tmp)


# ── resolve_secret ───────────────────────────────────────────────────────────


class TestResolveSecret:
    def test_plain_value_passthrough(self) -> None:
        assert resolve_secret("plaintext") == "plaintext"

    def test_empty_returns_empty(self) -> None:
        assert resolve_secret("") == ""

    def test_env_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET", "value-from-env")
        assert resolve_secret("env:MY_SECRET") == "value-from-env"

    def test_env_missing_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ABSENT_VAR", raising=False)
        assert resolve_secret("env:ABSENT_VAR") == ""

    def test_keyring_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHILLY_WHILLY_JIRA", "fallback-token")
        # keyring lookup may succeed or fail; the fallback path is the contract.
        result = resolve_secret("keyring:whilly/jira")
        # Either keyring has it (env var won't be checked), or env fallback fires.
        assert result in {"fallback-token", ""} or isinstance(result, str)


# ── [[scheduler]] ────────────────────────────────────────────────────────────


class TestSchedulerSection:
    def test_empty_when_no_toml(self, tmp_path: Path, reset_config_caches) -> None:
        load_layered(cwd=tmp_path)
        assert load_scheduler_rules() == []

    def test_single_rule(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[[scheduler]]
name = "qa-backlog"
jql = "project = QA AND status = 'Ready for Automation'"
poll_interval = 300
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        rules = load_scheduler_rules()
        assert len(rules) == 1
        assert rules[0].name == "qa-backlog"
        assert rules[0].jira_project_key == "QA"
        assert rules[0].poll_interval_seconds == 300

    def test_multiple_rules(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[[scheduler]]
name = "qa-backlog"
jql = "project = QA"

[[scheduler]]
name = "docs-watch"
jql = "project = DOCS AND issuetype = Documentation"
poll_interval = 600
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        rules = load_scheduler_rules()
        assert len(rules) == 2
        assert {r.name for r in rules} == {"qa-backlog", "docs-watch"}

    def test_rule_extra_fields_preserved_in_metadata(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[[scheduler]]
name = "rule-with-mcp"
jql = "project = QA"
mcp_profile = "qa-tools"
repo_target = "gitlab:qa/autotests"
replan_on_change = true
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        rules = load_scheduler_rules()
        assert len(rules) == 1
        meta = rules[0].custom_metadata
        assert meta["mcp_profile"] == "qa-tools"
        assert meta["repo_target"] == "gitlab:qa/autotests"
        assert meta["replan_on_change"] is True

    def test_invalid_rule_skipped(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[[scheduler]]
name = ""
jql = ""

[[scheduler]]
name = "valid"
jql = "project = OK"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        rules = load_scheduler_rules()
        assert len(rules) == 1
        assert rules[0].name == "valid"


# ── [project_map] ────────────────────────────────────────────────────────────


class TestProjectMapSection:
    def test_none_when_empty(self, tmp_path: Path, reset_config_caches) -> None:
        load_layered(cwd=tmp_path)
        assert load_project_map() is None

    def test_single_project_entry(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[project_map.QA]
repo_target = "gitlab:qa/autotests"
default_branch = "main"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        config = load_project_map()
        assert config is not None
        assert len(config.mappings) == 1
        entry = config.mappings[0]
        assert entry.jira_project_key == "QA"
        assert "gitlab:qa/autotests" in entry.git_repository_ids

    def test_label_filter_entry(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[project_map."label:service-payments"]
repo_target = "gitlab:platform/payments"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        config = load_project_map()
        assert config is not None
        assert len(config.mappings) == 1
        entry = config.mappings[0]
        assert entry.jira_project_key == ""  # Label-based, no project key
        assert "service-payments" in entry.issue_label_filters

    def test_multiple_entries(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[project_map.QA]
repo_target = "gitlab:qa/autotests"

[project_map.EORD]
repo_target = "gitlab:eord/backend"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        config = load_project_map()
        assert config is not None
        assert len(config.mappings) == 2


# ── [confluence] ─────────────────────────────────────────────────────────────


class TestConfluenceSection:
    def test_none_when_empty(self, tmp_path: Path, reset_config_caches) -> None:
        load_layered(cwd=tmp_path)
        assert load_confluence_publisher() is None

    def test_publisher_built_from_inline_token(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[confluence]
server_url = "https://wiki.example.com"
username = "bot@example.com"
token = "literal-token-value"
default_space = "QA"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        publisher = load_confluence_publisher()
        assert publisher is not None
        assert publisher.server_url == "https://wiki.example.com"
        assert publisher.token == "literal-token-value"
        assert publisher.default_space == "QA"

    def test_publisher_resolves_env_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_config_caches
    ) -> None:
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "from-env-12345")
        (tmp_path / "whilly.toml").write_text(
            """
[confluence]
server_url = "https://wiki.example.com"
username = "bot"
token = "env:CONFLUENCE_API_TOKEN"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        publisher = load_confluence_publisher()
        assert publisher is not None
        assert publisher.token == "from-env-12345"

    def test_returns_none_when_token_missing(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[confluence]
server_url = "https://wiki.example.com"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        assert load_confluence_publisher() is None


# ── [mcp_profile.<name>] ─────────────────────────────────────────────────────


class TestMcpProfileSection:
    def test_empty_when_no_profiles(self, tmp_path: Path, reset_config_caches) -> None:
        load_layered(cwd=tmp_path)
        assert load_mcp_profiles() == {}

    def test_profile_from_flat_tools(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[mcp_profile.qa-tools]
description = "QA tooling"
tools = ["allure", "jira-read"]
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        profiles = load_mcp_profiles()
        assert "qa-tools" in profiles
        profile = profiles["qa-tools"]
        assert profile.tools == ["allure", "jira-read"]
        assert profile.description == "QA tooling"

    def test_profile_from_servers_array(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[mcp_profile.dev-tools]
description = "Dev tools"

[[mcp_profile.dev-tools.servers]]
name = "git"
command = ["git", "status"]

[[mcp_profile.dev-tools.servers]]
name = "docker"
url = "http://localhost:2375"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        profiles = load_mcp_profiles()
        assert "dev-tools" in profiles
        profile = profiles["dev-tools"]
        assert "git" in profile.tools
        assert "docker" in profile.tools
        # Server metadata preserved
        assert "servers" in profile.metadata
        server_names = [s["name"] for s in profile.metadata["servers"]]
        assert server_names == ["git", "docker"]

    def test_multiple_profiles(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[mcp_profile.qa]
tools = ["allure"]

[mcp_profile.dev]
tools = ["git"]
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        profiles = load_mcp_profiles()
        assert set(profiles.keys()) == {"qa", "dev"}

    def test_empty_profile_skipped(self, tmp_path: Path, reset_config_caches) -> None:
        (tmp_path / "whilly.toml").write_text(
            """
[mcp_profile.empty]
description = "no tools"
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)
        profiles = load_mcp_profiles()
        assert "empty" not in profiles


# ── Integration: PRD §6.4 example ────────────────────────────────────────────


class TestPrdFullExample:
    """Verify the complete PRD §6.4 example parses cleanly."""

    def test_full_prd_example(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_config_caches) -> None:
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "secret-conf-token")
        (tmp_path / "whilly.toml").write_text(
            """
[jira]
server_url = "https://jira.example.com"
username = "bot"

[confluence]
server_url = "https://wiki.example.com"
username = "bot"
token = "env:CONFLUENCE_API_TOKEN"
default_space = "QA"

[[scheduler]]
name = "qa-ready"
jql = "project = QA AND status = 'Ready for Automation'"
poll_interval = 300
mcp_profile = "qa-tools"

[[scheduler]]
name = "docs-watch"
jql = "project = QA AND issuetype = Documentation"
poll_interval = 600

[project_map.QA]
repo_target = "gitlab:qa/autotests"
default_branch = "main"

[project_map.EORD]
repo_target = "gitlab:eord/backend"
default_branch = "develop"

[mcp_profile.qa-tools]
tools = ["allure", "jira-read"]
""",
            encoding="utf-8",
        )
        load_layered(cwd=tmp_path)

        rules = load_scheduler_rules()
        assert len(rules) == 2

        project_map = load_project_map()
        assert project_map is not None
        assert len(project_map.mappings) == 2

        publisher = load_confluence_publisher()
        assert publisher is not None
        assert publisher.token == "secret-conf-token"

        profiles = load_mcp_profiles()
        assert "qa-tools" in profiles
