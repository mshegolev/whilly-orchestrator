"""MCP (Model Context Protocol) integration for tool discovery and routing."""

from whilly.mcp.profiles import MCPProfile, MCPProfileRegistry, get_profile_registry
from whilly.mcp.registry import MCPRegistry, MCPTool, MCPToolParameter, get_registry

__all__ = [
    "MCPRegistry",
    "MCPTool",
    "MCPToolParameter",
    "MCPProfile",
    "MCPProfileRegistry",
    "get_registry",
    "get_profile_registry",
]
