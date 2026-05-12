"""MCP tool registry for discovering and routing external tools."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class MCPToolParameter:
    """Parameter definition for an MCP tool."""

    name: str
    type: str
    description: str
    required: bool = False
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
        }
        if self.default is not None:
            d["default"] = self.default
        return d


@dataclass
class MCPTool:
    """MCP tool definition."""

    name: str
    description: str
    category: str
    parameters: list[MCPToolParameter] = field(default_factory=list)
    url: str | None = None
    provider: str | None = None
    api_key_env: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "parameters": [p.to_dict() for p in self.parameters],
            "url": self.url,
            "provider": self.provider,
            "api_key_env": self.api_key_env,
        }


class MCPRegistry:
    """Registry for MCP tools and profiles."""

    def __init__(self) -> None:
        """Initialize registry."""
        self._tools: dict[str, MCPTool] = {}
        self._categories: dict[str, list[str]] = {}

    def register_tool(self, tool: MCPTool) -> None:
        """Register a tool in the registry.

        Args:
            tool: MCPTool to register
        """
        self._tools[tool.name] = tool

        if tool.category not in self._categories:
            self._categories[tool.category] = []
        self._categories[tool.category].append(tool.name)

        log.info("Registered MCP tool: %s (category=%s)", tool.name, tool.category)

    def get_tool(self, name: str) -> MCPTool | None:
        """Get a tool by name.

        Args:
            name: Tool name

        Returns:
            MCPTool or None if not found
        """
        return self._tools.get(name)

    def list_tools(self, category: str | None = None) -> list[MCPTool]:
        """List all tools, optionally filtered by category.

        Args:
            category: Optional category filter

        Returns:
            List of MCPTool objects
        """
        if category:
            names = self._categories.get(category, [])
            return [self._tools[name] for name in names if name in self._tools]
        return list(self._tools.values())

    def list_categories(self) -> list[str]:
        """List all tool categories.

        Returns:
            List of category names
        """
        return sorted(self._categories.keys())

    def load_from_json(self, path: Path) -> None:
        """Load tool definitions from JSON file.

        Args:
            path: Path to JSON file
        """
        try:
            data = json.loads(path.read_text())

            for tool_data in data.get("tools", []):
                params = [
                    MCPToolParameter(
                        name=p["name"],
                        type=p["type"],
                        description=p["description"],
                        required=p.get("required", False),
                        default=p.get("default"),
                    )
                    for p in tool_data.get("parameters", [])
                ]

                tool = MCPTool(
                    name=tool_data["name"],
                    description=tool_data["description"],
                    category=tool_data["category"],
                    parameters=params,
                    url=tool_data.get("url"),
                    provider=tool_data.get("provider"),
                    api_key_env=tool_data.get("api_key_env"),
                )

                self.register_tool(tool)

            log.info("Loaded %d tools from %s", len(self._tools), path)
        except Exception as exc:
            log.error("Failed to load tools from %s: %s", path, exc)
            raise

    def to_json(self, path: Path) -> None:
        """Export registry to JSON file.

        Args:
            path: Path to write JSON file
        """
        data = {"tools": [tool.to_dict() for tool in self._tools.values()]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        log.info("Exported %d tools to %s", len(self._tools), path)


# Global registry instance
_registry: MCPRegistry | None = None


def get_registry() -> MCPRegistry:
    """Get or create the global MCP registry.

    Returns:
        Global MCPRegistry instance
    """
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry
