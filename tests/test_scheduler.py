"""Tests for scheduler components (rules, executor, worker, etc.)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from whilly.scheduler.deduplicator import compute_issue_hash, deduplicate_issues
from whilly.scheduler.models import SchedulerPollCycle, SchedulerRule
from whilly.scheduler.repository import InMemorySchedulerRepository, SchedulerRepositoryError


class TestSchedulerRule:
    """Test SchedulerRule model."""

    def test_rule_creation(self) -> None:
        """Test creating a SchedulerRule."""
        rule = SchedulerRule(
            id="rule-1",
            name="Test Rule",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )
        assert rule.id == "rule-1"
        assert rule.name == "Test Rule"
        assert rule.enabled is True

    def test_rule_to_dict(self) -> None:
        """Test serializing a SchedulerRule."""
        rule = SchedulerRule(
            id="rule-1",
            name="Test",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )
        d = rule.to_dict()
        assert d["id"] == "rule-1"
        assert d["name"] == "Test"


class TestSchedulerPollCycle:
    """Test SchedulerPollCycle model."""

    def test_cycle_creation(self) -> None:
        """Test creating a SchedulerPollCycle."""
        cycle = SchedulerPollCycle(
            id=1,
            rule_id="rule-1",
            poll_status="completed",
            total_issues_found=5,
        )
        assert cycle.id == 1
        assert cycle.rule_id == "rule-1"
        assert cycle.poll_status == "completed"

    def test_cycle_to_dict(self) -> None:
        """Test serializing a SchedulerPollCycle."""
        cycle = SchedulerPollCycle(
            id=1,
            rule_id="rule-1",
            created_at=datetime.now(timezone.utc),
        )
        d = cycle.to_dict()
        assert d["id"] == 1
        assert d["rule_id"] == "rule-1"


class TestInMemorySchedulerRepository:
    """Test InMemorySchedulerRepository."""

    @pytest.mark.asyncio
    async def test_create_and_get_rule(self) -> None:
        """Test creating and retrieving a rule."""
        repo = InMemorySchedulerRepository()
        rule = SchedulerRule(
            id="rule-1",
            name="Test",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )

        await repo.create_rule(rule)
        retrieved = await repo.get_rule("rule-1")
        assert retrieved is not None
        assert retrieved.id == "rule-1"

    @pytest.mark.asyncio
    async def test_create_duplicate_rule_raises(self) -> None:
        """Test that creating duplicate rule raises error."""
        repo = InMemorySchedulerRepository()
        rule = SchedulerRule(
            id="rule-1",
            name="Test",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )

        await repo.create_rule(rule)
        with pytest.raises(SchedulerRepositoryError, match="already exists"):
            await repo.create_rule(rule)

    @pytest.mark.asyncio
    async def test_list_rules(self) -> None:
        """Test listing rules."""
        repo = InMemorySchedulerRepository()
        rule1 = SchedulerRule(
            id="rule-1",
            name="Rule 1",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=True,
        )
        rule2 = SchedulerRule(
            id="rule-2",
            name="Rule 2",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=False,
        )

        await repo.create_rule(rule1)
        await repo.create_rule(rule2)

        all_rules = await repo.list_rules(enabled_only=False)
        assert len(all_rules) == 2

        enabled_rules = await repo.list_rules(enabled_only=True)
        assert len(enabled_rules) == 1
        assert enabled_rules[0].id == "rule-1"

    @pytest.mark.asyncio
    async def test_record_poll_cycle(self) -> None:
        """Test recording a poll cycle."""
        repo = InMemorySchedulerRepository()
        cycle = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="completed",
            total_issues_found=5,
        )

        cycle_id = await repo.record_poll_cycle(cycle)
        assert cycle_id == 1

        retrieved = await repo.get_poll_cycle(cycle_id)
        assert retrieved is not None
        assert retrieved.poll_status == "completed"

    @pytest.mark.asyncio
    async def test_get_last_successful_poll(self) -> None:
        """Test getting last successful poll."""
        repo = InMemorySchedulerRepository()

        cycle1 = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="completed",
            created_at=datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc),
        )
        cycle2 = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="completed",
            created_at=datetime(2026, 5, 13, 11, 0, 0, tzinfo=timezone.utc),
        )
        cycle3 = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="failed",
            created_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc),
        )

        await repo.record_poll_cycle(cycle1)
        await repo.record_poll_cycle(cycle2)
        await repo.record_poll_cycle(cycle3)

        last = await repo.get_last_successful_poll("rule-1")
        assert last is not None
        assert last.created_at == cycle2.created_at


class TestDeduplicatorWithScheduler:
    """Test deduplicator with scheduler-style data."""

    def test_deduplicate_jira_issues(self) -> None:
        """Test deduplicating Jira issues."""
        issues = [
            {"key": "EINVY-1", "summary": "Bug in login", "updated": "2026-05-13T10:00:00Z"},
            {"key": "EINVY-2", "summary": "Bug in login", "updated": "2026-05-13T11:00:00Z"},
            {"key": "EINVY-3", "summary": "Feature request", "updated": "2026-05-13T12:00:00Z"},
        ]

        unique, dups = deduplicate_issues(issues, ("summary",))
        assert len(unique) == 2
        assert len(dups) == 1
        assert "EINVY-2" in dups

    def test_compute_hash_with_jira_fields(self) -> None:
        """Test hash computation with Jira-style fields."""
        issue = {
            "key": "EINVY-100",
            "summary": "Fix database connection",
            "project": {"key": "EINVY"},
        }

        hash1 = compute_issue_hash(issue, ("key",))
        hash2 = compute_issue_hash(issue, ("summary",))
        hash3 = compute_issue_hash(issue, ("key", "summary"))

        assert hash1 != hash2
        assert hash3 != hash1


class TestSchedulerConfiguration:
    """Test scheduler configuration loading."""

    def test_config_validation_missing_required_field(self) -> None:
        """Test that missing required fields raise error."""
        from whilly.scheduler.config import _rule_from_dict, SchedulerConfigError

        rule_data = {"id": "rule-1", "name": "Test"}
        with pytest.raises(SchedulerConfigError, match="jira_project_key"):
            _rule_from_dict(rule_data, "test")

    def test_config_validation_empty_id(self) -> None:
        """Test that empty ID raises error."""
        from whilly.scheduler.config import _rule_from_dict, SchedulerConfigError

        rule_data = {"id": "", "name": "Test", "jira_project_key": "TEST", "jql_filter": "project = TEST"}
        with pytest.raises(SchedulerConfigError, match="id"):
            _rule_from_dict(rule_data, "test")


class TestSchedulerDocumentation:
    """Test scheduler documentation generation."""

    def test_generate_rule_markdown(self) -> None:
        """Test markdown generation for a rule."""
        from whilly.scheduler import SchedulerDocumentation

        rule = SchedulerRule(
            id="rule-1",
            name="Test Rule",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            description="A test rule",
        )

        docs = SchedulerDocumentation()
        markdown = docs.generate_rule_markdown(rule)

        assert "Test Rule" in markdown
        assert "project = TEST" in markdown
        assert "rule-1" in markdown

    def test_generate_rules_index(self) -> None:
        """Test index generation for multiple rules."""
        from whilly.scheduler import SchedulerDocumentation

        rule1 = SchedulerRule(
            id="rule-1",
            name="Rule 1",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=True,
        )
        rule2 = SchedulerRule(
            id="rule-2",
            name="Rule 2",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=False,
        )

        docs = SchedulerDocumentation()
        index = docs.generate_rules_index([rule1, rule2])

        assert "Rule 1" in index
        assert "Rule 2" in index
        assert "Enabled Rules" in index
        assert "Disabled Rules" in index


class TestMCPRegistry:
    """Test MCP registry."""

    def test_register_and_get_tool(self) -> None:
        """Test registering and retrieving a tool."""
        from whilly.mcp import MCPRegistry, MCPTool, MCPToolParameter

        registry = MCPRegistry()
        tool = MCPTool(
            name="test_tool",
            description="A test tool",
            category="testing",
            parameters=[MCPToolParameter(name="arg1", type="string", description="Test arg")],
        )

        registry.register_tool(tool)
        retrieved = registry.get_tool("test_tool")

        assert retrieved is not None
        assert retrieved.name == "test_tool"
        assert retrieved.category == "testing"

    def test_list_tools_by_category(self) -> None:
        """Test listing tools by category."""
        from whilly.mcp import MCPRegistry, MCPTool

        registry = MCPRegistry()
        tool1 = MCPTool(name="tool1", description="Test", category="jira")
        tool2 = MCPTool(name="tool2", description="Test", category="jira")
        tool3 = MCPTool(name="tool3", description="Test", category="github")

        registry.register_tool(tool1)
        registry.register_tool(tool2)
        registry.register_tool(tool3)

        jira_tools = registry.list_tools(category="jira")
        assert len(jira_tools) == 2

        github_tools = registry.list_tools(category="github")
        assert len(github_tools) == 1


class TestMCPProfiles:
    """Test MCP profiles."""

    def test_register_and_get_profile(self) -> None:
        """Test registering and retrieving a profile."""
        from whilly.mcp import MCPProfile, MCPProfileRegistry

        registry = MCPProfileRegistry()
        profile = MCPProfile(
            name="test_profile",
            description="A test profile",
            tools=["tool1", "tool2"],
        )

        registry.register_profile(profile)
        retrieved = registry.get_profile("test_profile")

        assert retrieved is not None
        assert retrieved.name == "test_profile"
        assert len(retrieved.tools) == 2
