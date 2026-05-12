"""MCP tool profiles for organizing and discovering tools."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class MCPProfile:
    """Profile containing a set of tools for a specific use case."""

    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPProfile:
        """Create from dictionary."""
        return cls(
            name=data["name"],
            description=data["description"],
            tools=data.get("tools", []),
            metadata=data.get("metadata", {}),
        )


class MCPProfileRegistry:
    """Registry for MCP tool profiles."""

    def __init__(self) -> None:
        """Initialize profile registry."""
        self._profiles: dict[str, MCPProfile] = {}

    def register_profile(self, profile: MCPProfile) -> None:
        """Register a profile.

        Args:
            profile: MCPProfile to register
        """
        self._profiles[profile.name] = profile
        log.info("Registered MCP profile: %s with %d tools", profile.name, len(profile.tools))

    def get_profile(self, name: str) -> MCPProfile | None:
        """Get a profile by name.

        Args:
            name: Profile name

        Returns:
            MCPProfile or None if not found
        """
        return self._profiles.get(name)

    def list_profiles(self) -> list[MCPProfile]:
        """List all profiles.

        Returns:
            List of MCPProfile objects
        """
        return list(self._profiles.values())

    def load_from_json(self, path: Path) -> None:
        """Load profiles from JSON file.

        Args:
            path: Path to JSON file
        """
        try:
            data = json.loads(path.read_text())

            for profile_data in data.get("profiles", []):
                profile = MCPProfile.from_dict(profile_data)
                self.register_profile(profile)

            log.info("Loaded %d profiles from %s", len(self._profiles), path)
        except Exception as exc:
            log.error("Failed to load profiles from %s: %s", path, exc)
            raise

    def to_json(self, path: Path) -> None:
        """Export profiles to JSON file.

        Args:
            path: Path to write JSON file
        """
        data = {"profiles": [profile.to_dict() for profile in self._profiles.values()]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        log.info("Exported %d profiles to %s", len(self._profiles), path)


# Global profile registry instance
_profile_registry: MCPProfileRegistry | None = None


def get_profile_registry() -> MCPProfileRegistry:
    """Get or create the global MCP profile registry.

    Returns:
        Global MCPProfileRegistry instance
    """
    global _profile_registry
    if _profile_registry is None:
        _profile_registry = MCPProfileRegistry()
    return _profile_registry
