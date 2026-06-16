## Purpose

The mcp-integration capability governs Whilly's Model Context Protocol tool and
profile registries in `whilly/mcp/registry.py` and `whilly/mcp/profiles.py`
(re-exported from `whilly/mcp/__init__.py`). This capability covers how MCP tools
are registered by name and indexed by category, how named profiles group tool
references, how both registries round-trip to and from JSON, and how each tool
names — but does not store — the credential env var a caller must supply. This is
a registration and discovery surface: it defines and looks up tool metadata; it
does not itself perform mutating external calls.

## Requirements

### Requirement: Tool registry registration and lookup
The system SHALL let `MCPRegistry` register tools by name, index each tool under its category, and expose `get_tool(name)`, `list_tools(category=None)`, and `list_categories()`, and `get_registry()` MUST return a process-global singleton instance.

#### Scenario: Register then retrieve by name and category
- **WHEN** a caller registers an `MCPTool` with a given `name` and `category` via `register_tool`
- **THEN** `get_tool(name)` SHALL return that tool
- **AND** `list_tools(category)` SHALL include it and `list_categories()` SHALL include its category

#### Scenario: Unknown tool name returns None
- **WHEN** `get_tool` is called with a name that was never registered
- **THEN** the system SHALL return `None` rather than raise

#### Scenario: get_registry returns a shared singleton
- **WHEN** `get_registry()` is called more than once in the same process
- **THEN** the system SHALL return the same `MCPRegistry` instance on every call

### Requirement: Tool registry JSON round-trip
The system SHALL load tool definitions from a JSON file via `load_from_json` and export them via `to_json`, round-tripping each tool's `name`, `description`, `category`, `parameters`, `url`, `provider`, and `api_key_env`.

#### Scenario: Load registers every tool entry
- **WHEN** `load_from_json` reads a JSON file with a `tools` array
- **THEN** the system SHALL construct an `MCPTool` (with its `MCPToolParameter` list) for each entry and register it in the registry

#### Scenario: Export preserves tool fields
- **WHEN** `to_json` writes the registry to a path
- **THEN** the output SHALL contain each tool's `name`, `description`, `category`, `parameters`, `url`, `provider`, and `api_key_env`

### Requirement: Profile registry groups tool references
The system SHALL let `MCPProfileRegistry` register named `MCPProfile` entries that each reference a set of tool names, expose `get_profile(name)` and `list_profiles()`, support JSON load/export, and `get_profile_registry()` MUST return a process-global singleton.

#### Scenario: Register and retrieve a profile
- **WHEN** a caller registers an `MCPProfile` referencing a list of tool names via `register_profile`
- **THEN** `get_profile(name)` SHALL return that profile and `list_profiles()` SHALL include it

#### Scenario: Profile JSON round-trip
- **WHEN** `load_from_json` reads a `profiles` array, or `to_json` exports the registry
- **THEN** each profile SHALL round-trip its `name`, `description`, `tools`, and `metadata`

#### Scenario: get_profile_registry returns a shared singleton
- **WHEN** `get_profile_registry()` is called more than once in the same process
- **THEN** the system SHALL return the same `MCPProfileRegistry` instance

### Requirement: Credentials are named, not stored
The system SHALL identify each tool's required credential by an `api_key_env` env-var name on the `MCPTool` definition, and the registry MUST NOT hold the secret value itself — it names the env var the caller is expected to supply at invocation.

#### Scenario: Tool carries an env-var name only
- **WHEN** an `MCPTool` declares a credential requirement
- **THEN** that requirement SHALL be expressed as the `api_key_env` env-var name on the tool definition
- **AND** the registry SHALL NOT store or persist the secret value behind that env var

### Requirement: Registry is a discovery surface, not a mutating caller
The system SHALL scope this capability to defining, registering, and discovering tool and profile metadata, and the registries MUST NOT themselves perform mutating external API calls on behalf of the named tools.

#### Scenario: Registry operations are local metadata operations
- **WHEN** any `MCPRegistry` or `MCPProfileRegistry` method is invoked (register, get, list, load, export)
- **THEN** the operation SHALL only read or write in-process metadata or local JSON files
- **AND** it SHALL NOT issue an outbound mutating call to any external tool endpoint
