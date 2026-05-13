"""Tests for Phase 5 MCP profiles and skill CLI."""

from __future__ import annotations

from pathlib import Path


from whilly.cli.skill import (
    _discover_skills,
    _read_skill_description,
    _read_skill_trigger,
    build_skill_parser,
)
from whilly.mcp import MCPProfile, MCPProfileRegistry


class TestSkillDiscovery:
    def test_discover_skills_empty_dir(self, tmp_path: Path) -> None:
        assert _discover_skills(tmp_path) == []

    def test_discover_skills_no_dir(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing"
        assert _discover_skills(missing) == []

    def test_discover_single_skill(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "# My Skill\n\nThis is a great skill.\n\nTrigger: /my-skill\n",
            encoding="utf-8",
        )
        result = _discover_skills(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "my-skill"
        assert result[0]["source"] == "skill"
        assert "great skill" in result[0]["description"]
        assert result[0]["trigger"] == "/my-skill"

    def test_discover_single_file_skill(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "quick-skill.md"
        skill_file.write_text("# Quick Skill\n\nDescription here.\n", encoding="utf-8")
        result = _discover_skills(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "quick-skill"

    def test_discover_multiple_skills(self, tmp_path: Path) -> None:
        for name in ("alpha", "beta", "gamma"):
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name}\n\n{name} description", encoding="utf-8")
        result = _discover_skills(tmp_path)
        assert len(result) == 3
        names = {item["name"] for item in result}
        assert names == {"alpha", "beta", "gamma"}


class TestSkillMetadata:
    def test_read_skill_description(self, tmp_path: Path) -> None:
        p = tmp_path / "skill.md"
        p.write_text("# Title\n\nThis is the first paragraph.\n\nSecond.\n", encoding="utf-8")
        assert _read_skill_description(p) == "This is the first paragraph."

    def test_read_skill_trigger_simple(self, tmp_path: Path) -> None:
        p = tmp_path / "skill.md"
        p.write_text("# Title\n\nTrigger: /foo\n", encoding="utf-8")
        assert _read_skill_trigger(p) == "/foo"

    def test_read_skill_trigger_bold(self, tmp_path: Path) -> None:
        p = tmp_path / "skill.md"
        p.write_text("# Title\n\n**Trigger:** /bar\n", encoding="utf-8")
        assert _read_skill_trigger(p) == "/bar"

    def test_read_skill_no_trigger(self, tmp_path: Path) -> None:
        p = tmp_path / "skill.md"
        p.write_text("# Title\n\nNo trigger here.\n", encoding="utf-8")
        assert _read_skill_trigger(p) == ""


class TestSkillParser:
    def test_parser_has_list_command(self) -> None:
        parser = build_skill_parser()
        args = parser.parse_args(["list"])
        assert args.action == "list"
        assert args.source == "all"

    def test_parser_has_show_command(self) -> None:
        parser = build_skill_parser()
        args = parser.parse_args(["show", "my-skill"])
        assert args.action == "show"
        assert args.name == "my-skill"

    def test_parser_source_filter(self) -> None:
        parser = build_skill_parser()
        args = parser.parse_args(["list", "--source", "skills"])
        assert args.source == "skills"

    def test_parser_json_flag(self) -> None:
        parser = build_skill_parser()
        args = parser.parse_args(["list", "--json"])
        assert args.json is True


class TestMCPProfile:
    def test_profile_creation(self) -> None:
        p = MCPProfile(name="qa-tools", description="QA tools", tools=["allure", "jira"])
        assert p.name == "qa-tools"
        assert len(p.tools) == 2

    def test_profile_to_dict_and_back(self) -> None:
        p = MCPProfile(name="dev", description="Dev tools", tools=["git", "docker"], metadata={"env": "prod"})
        data = p.to_dict()
        restored = MCPProfile.from_dict(data)
        assert restored.name == p.name
        assert restored.tools == p.tools
        assert restored.metadata == p.metadata


class TestMCPProfileRegistry:
    def test_registry_register_and_get(self) -> None:
        registry = MCPProfileRegistry()
        profile = MCPProfile(name="test", description="test profile", tools=["t1"])
        registry.register_profile(profile)
        assert registry.get_profile("test") is profile

    def test_registry_list_profiles(self) -> None:
        registry = MCPProfileRegistry()
        registry.register_profile(MCPProfile(name="a", description="a", tools=[]))
        registry.register_profile(MCPProfile(name="b", description="b", tools=[]))
        profiles = registry.list_profiles()
        assert len(profiles) == 2

    def test_registry_get_missing_returns_none(self) -> None:
        registry = MCPProfileRegistry()
        assert registry.get_profile("missing") is None

    def test_registry_save_and_load_json(self, tmp_path: Path) -> None:
        registry = MCPProfileRegistry()
        registry.register_profile(MCPProfile(name="qa", description="QA", tools=["allure", "jira"]))

        path = tmp_path / "profiles.json"
        registry.to_json(path)
        assert path.exists()

        registry2 = MCPProfileRegistry()
        registry2.load_from_json(path)
        assert registry2.get_profile("qa") is not None
        assert registry2.get_profile("qa").tools == ["allure", "jira"]


class TestPromptInjection:
    """Phase 5 TASK-SCH-042: build_task_prompt accepts mcp_profile."""

    def _make_plan_and_task(self):
        from whilly.core.models import Plan, Priority, Task, TaskStatus

        task = Task(
            id="t-mcp-1",
            status=TaskStatus.PENDING,
            description="Test task",
            priority=Priority.MEDIUM,
            acceptance_criteria=(),
            test_steps=(),
        )
        plan = Plan(id="plan-mcp", name="MCP test plan")
        return plan, task

    def test_no_mcp_profile_omits_section(self) -> None:
        from whilly.core.prompts import build_task_prompt

        plan, task = self._make_plan_and_task()
        prompt = build_task_prompt(task, plan)
        assert "## Available Tools" not in prompt

    def test_empty_mcp_profile_omits_section(self) -> None:
        from whilly.core.prompts import build_task_prompt

        plan, task = self._make_plan_and_task()
        prompt = build_task_prompt(task, plan, mcp_profile={"name": "empty", "tools": []})
        assert "## Available Tools" not in prompt

    def test_mcp_profile_with_string_tools(self) -> None:
        from whilly.core.prompts import build_task_prompt

        plan, task = self._make_plan_and_task()
        profile = {"name": "qa-tools", "description": "QA tooling", "tools": ["allure", "jira-read"]}
        prompt = build_task_prompt(task, plan, mcp_profile=profile)

        assert "## Available Tools" in prompt
        assert "Profile: **qa-tools**" in prompt
        assert "QA tooling" in prompt
        assert "**allure**" in prompt
        assert "**jira-read**" in prompt

    def test_mcp_profile_with_dict_tools(self) -> None:
        from whilly.core.prompts import build_task_prompt

        plan, task = self._make_plan_and_task()
        profile = {
            "name": "dev",
            "tools": [
                {"name": "git", "description": "Version control"},
                {"name": "docker", "description": "Container runtime"},
            ],
        }
        prompt = build_task_prompt(task, plan, mcp_profile=profile)

        assert "## Available Tools" in prompt
        assert "**git** — Version control" in prompt
        assert "**docker** — Container runtime" in prompt

    def test_mcp_profile_section_after_rules(self) -> None:
        from whilly.core.prompts import build_task_prompt

        plan, task = self._make_plan_and_task()
        profile = {"name": "tools", "tools": ["t1"]}
        prompt = build_task_prompt(task, plan, mcp_profile=profile)

        rules_idx = prompt.index("## Правила")
        tools_idx = prompt.index("## Available Tools")
        assert rules_idx < tools_idx, "Available Tools section should come after rules"

    def test_mcp_profile_filters_empty_tool_names(self) -> None:
        from whilly.core.prompts import build_task_prompt

        plan, task = self._make_plan_and_task()
        profile = {
            "name": "mixed",
            "tools": [
                {"name": "real-tool", "description": "Works"},
                {"name": "", "description": "no name"},
                "",
            ],
        }
        prompt = build_task_prompt(task, plan, mcp_profile=profile)
        assert "**real-tool**" in prompt
        # Empty-name entries must be skipped — count tool bullets in Available Tools section
        tools_section = prompt.split("## Available Tools", 1)[1]
        bullet_lines = [line for line in tools_section.split("\n") if line.startswith("- **")]
        assert len(bullet_lines) == 1
        assert "real-tool" in bullet_lines[0]
