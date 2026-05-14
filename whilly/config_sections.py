"""Parsers that turn `whilly.toml` sections into typed domain objects.

PRD-jira-scheduler-integration §6.4 defines four new TOML sections:

* ``[[scheduler]]``  — array of scheduler rules (Phase 3)
* ``[project_map]``  — Jira project key → repo target mappings (Phase 2)
* ``[mcp_profile.<name>]`` — MCP profiles, each with a ``servers`` list (Phase 5)
* ``[confluence]``  — Confluence REST API credentials (Phase 4)

This module provides one helper per section that reads from the layered
config cache populated by :func:`whilly.config.load_layered` and returns
the typed domain object (or ``None`` when the section is empty).

Why a separate module? ``whilly.config`` is intentionally I/O-light and
must not import from :mod:`whilly.scheduler` etc. (would create import
cycles, and the loader runs during ``whilly --help``). The parsers here
import freely because they are called on demand by CLI/runtime code that
has already paid the import cost.
"""

from __future__ import annotations

import os
import re
from typing import Any

from whilly.config import get_toml_list_section, get_toml_section


# ── secret reference resolution ──────────────────────────────────────────────


_SECRET_REF_RE = re.compile(r"^(env|keyring):(.+)$")


def resolve_secret(value: str) -> str:
    """Resolve ``env:NAME`` / ``keyring:PATH`` references.

    Plain values are returned unchanged. ``env:NAME`` reads from
    ``os.environ`` (returns empty string if missing). ``keyring:PATH`` falls
    back to env-style lookup via ``WHILLY_<PATH_UPPER>`` because the keyring
    backend is optional and may not be installed on every host.
    """
    if not isinstance(value, str) or not value:
        return value or ""
    match = _SECRET_REF_RE.match(value)
    if not match:
        return value
    kind, ref = match.group(1), match.group(2)
    if kind == "env":
        return os.environ.get(ref, "")
    if kind == "keyring":
        try:
            import keyring  # type: ignore[import-untyped]

            stored = keyring.get_password("whilly", ref)
            if stored:
                return stored
        except Exception:
            pass
        env_key = "WHILLY_" + re.sub(r"[^A-Za-z0-9]", "_", ref).upper()
        return os.environ.get(env_key, "")
    return value


# ── [[scheduler]] → list[SchedulerRule] ──────────────────────────────────────


def load_scheduler_rules() -> list[Any]:
    """Build :class:`SchedulerRule` objects from the ``[[scheduler]]`` section."""
    from whilly.scheduler.models import SchedulerRule

    raw_rules = get_toml_list_section("scheduler")
    rules: list[SchedulerRule] = []
    for idx, item in enumerate(raw_rules):
        rule = _scheduler_rule_from_toml(item, source_index=idx)
        if rule is not None:
            rules.append(rule)
    return rules


def _scheduler_rule_from_toml(data: dict[str, Any], *, source_index: int) -> Any:
    """Convert a single `[[scheduler]]` table to a SchedulerRule, or None."""
    from whilly.scheduler.models import SchedulerRule

    name = str(data.get("name", "") or "").strip()
    if not name:
        return None
    rule_id = str(data.get("id", "") or name).strip()
    jql = str(data.get("jql", data.get("jql_filter", "") or "")).strip()
    jira_project = str(data.get("jira_project_key", "") or "").strip()
    if not jira_project and jql:
        match = re.search(r"project\s*=\s*['\"]?([A-Z][A-Z0-9_]*)", jql, re.IGNORECASE)
        if match:
            jira_project = match.group(1).upper()
    if not jql or not jira_project:
        return None

    poll_interval = int(data.get("poll_interval", data.get("poll_interval_seconds", 300)) or 300)
    if poll_interval <= 0:
        poll_interval = 300

    max_results = int(data.get("max_results_per_poll", data.get("max_inflight", 50)) or 50)
    if max_results <= 0:
        max_results = 50

    dedup = data.get("deduplication_fields") or ("key", "summary")
    if not isinstance(dedup, (list, tuple)):
        dedup = ("key", "summary")
    dedup_tuple = tuple(str(f) for f in dedup if str(f).strip()) or ("key", "summary")

    plan_config = data.get("plan_config") or {}
    if not isinstance(plan_config, dict):
        plan_config = {}

    custom_metadata: dict[str, Any] = dict(data.get("custom_metadata") or {})
    for key in ("mcp_profile", "repo_target", "replan_on_change"):
        if key in data and data[key] is not None:
            custom_metadata.setdefault(key, data[key])

    return SchedulerRule(
        id=rule_id,
        name=name,
        description=str(data.get("description", "") or "").strip(),
        enabled=bool(data.get("enabled", True)),
        jira_project_key=jira_project,
        jql_filter=jql,
        poll_interval_seconds=poll_interval,
        max_results_per_poll=max_results,
        deduplication_fields=dedup_tuple,
        plan_config=plan_config,
        custom_metadata=custom_metadata,
    )


# ── [project_map] → ProjectMapConfig ─────────────────────────────────────────


def load_project_map() -> Any | None:
    """Build a :class:`ProjectMapConfig` from the ``[project_map]`` section."""
    from whilly.project_config.models import ProjectMapConfig, ProjectMapEntry

    raw = get_toml_section("project_map")
    if not raw:
        return None

    mappings: list[ProjectMapEntry] = []
    default_mapping: ProjectMapEntry | None = None
    fallback_repos: tuple[str, ...] = ()

    for key, value in raw.items():
        if not isinstance(value, dict):
            if key == "fallback_repo_ids" and isinstance(value, list):
                fallback_repos = tuple(str(v) for v in value if str(v).strip())
            continue

        entry = _project_map_entry_from_toml(key, value)
        if entry is None:
            continue
        if key.lower() == "default":
            default_mapping = entry
        else:
            mappings.append(entry)

    if not mappings and default_mapping is None and not fallback_repos:
        return None

    return ProjectMapConfig(
        version="1.0",
        mappings=tuple(mappings),
        default_mapping=default_mapping,
        fallback_repo_ids=fallback_repos,
    )


def _project_map_entry_from_toml(key: str, data: dict[str, Any]) -> Any:
    """Convert one `[project_map.XYZ]` sub-table to a ProjectMapEntry."""
    from whilly.project_config.models import ProjectMapEntry

    is_label_filter = key.lower().startswith("label:")
    jira_project = "" if is_label_filter else key.upper()
    label_filters: tuple[str, ...] = ()
    if is_label_filter:
        label = key.split(":", 1)[1].strip()
        if label:
            label_filters = (label,)

    repo_target = str(data.get("repo_target", "") or "").strip()
    repo_ids: tuple[str, ...] = (repo_target,) if repo_target else ()
    extra_repos = data.get("git_repository_ids") or []
    if isinstance(extra_repos, list):
        repo_ids = tuple(dict.fromkeys((*repo_ids, *(str(r) for r in extra_repos if str(r).strip()))))

    repo_paths = data.get("git_repository_paths") or []
    repo_paths_tuple: tuple[str, ...] = (
        tuple(str(p) for p in repo_paths if str(p).strip()) if isinstance(repo_paths, list) else ()
    )

    default_repo = str(data.get("default_repo_id", "") or data.get("default_branch", "") or "").strip()
    if not repo_ids and not repo_paths_tuple and not default_repo:
        return None

    return ProjectMapEntry(
        jira_project_key=jira_project,
        git_repository_ids=repo_ids,
        git_repository_paths=repo_paths_tuple,
        issue_label_filters=label_filters,
        default_repo_id=default_repo,
        custom_field_mappings=None,
    )


# ── [confluence] → ConfluencePublisher | None ────────────────────────────────


def load_confluence_publisher() -> Any | None:
    """Build a :class:`ConfluencePublisher` from the ``[confluence]`` section."""
    from whilly.adapters.confluence.publisher import ConfluencePublisher

    cfg = get_toml_section("confluence")
    if not cfg:
        return None
    server_url = str(cfg.get("server_url", "") or "").strip()
    token = resolve_secret(str(cfg.get("token", "") or ""))
    if not server_url or not token:
        return None
    return ConfluencePublisher(
        server_url=server_url,
        username=str(cfg.get("username", "") or ""),
        token=token,
        default_space=str(cfg.get("default_space", "") or ""),
        verify_ssl=bool(cfg.get("verify_ssl", True)),
        auth_scheme=str(cfg.get("auth_scheme", "basic") or "basic"),
        timeout=int(cfg.get("timeout", 15) or 15),
    )


# ── [mcp_profile.<name>] → dict[str, MCPProfile] ─────────────────────────────


def load_mcp_profiles() -> dict[str, Any]:
    """Build a dict of :class:`MCPProfile` from ``[mcp_profile.*]`` sub-tables."""
    from whilly.mcp.profiles import MCPProfile

    profiles: dict[str, MCPProfile] = {}
    raw = get_toml_section("mcp_profile")
    for name, body in raw.items():
        if not isinstance(body, dict):
            continue
        profile = _mcp_profile_from_toml(name, body)
        if profile is not None:
            profiles[profile.name] = profile
    return profiles


def _mcp_profile_from_toml(name: str, body: dict[str, Any]) -> Any:
    """Convert one `[mcp_profile.<name>]` sub-table to an MCPProfile."""
    from whilly.mcp.profiles import MCPProfile

    description = str(body.get("description", "") or "")
    tools: list[str] = []
    metadata: dict[str, Any] = {}

    flat_tools = body.get("tools")
    if isinstance(flat_tools, list):
        tools.extend(str(t) for t in flat_tools if str(t).strip())

    servers = body.get("servers")
    if isinstance(servers, list):
        server_metadata: list[dict[str, Any]] = []
        for server in servers:
            if not isinstance(server, dict):
                continue
            server_name = str(server.get("name", "") or "").strip()
            if not server_name:
                continue
            tools.append(server_name)
            server_metadata.append(
                {
                    "name": server_name,
                    "command": server.get("command") or [],
                    "url": server.get("url") or "",
                    "env": server.get("env") or {},
                }
            )
        if server_metadata:
            metadata["servers"] = server_metadata

    tools = list(dict.fromkeys(tools))
    if not tools:
        return None

    return MCPProfile(name=name, description=description, tools=tools, metadata=metadata)


__all__ = [
    "load_scheduler_rules",
    "load_project_map",
    "load_confluence_publisher",
    "load_mcp_profiles",
    "resolve_secret",
]
