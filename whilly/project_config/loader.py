"""Load and validate universal project configuration files."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from whilly.core.agent_runner import scan_command
from whilly.project_config.models import (
    HumanLoopConfig,
    PipelineStepConfig,
    ProjectConfig,
    ProjectMapConfig,
    ProjectMapEntry,
    RepositoryConfig,
    SinkConfig,
    TaskSourceConfig,
    VerificationCommandConfig,
)
from whilly.project_config.presets import (
    PUBLIC_PROJECT_TYPES,
    SUPPORTED_PROJECT_TYPES,
    normalize_project_type,
    preset_pipeline,
)
from whilly.security.secret_lint import SecretFinding, scan_mapping


class ProjectConfigError(ValueError):
    """Raised when a project configuration is invalid."""


SUPPORTED_TASK_SOURCE_KINDS = frozenset(
    {
        "json_plan",
        "github",
        "github_issues",
        "github_projects",
        "jira",
        "forge",
        "manual_prd",
    }
)
SUPPORTED_SINK_TYPES = frozenset(
    {"github_pr", "ci_status", "github_issue_comment", "jira_comment", "jsonl", "dashboard"}
)
SUPPORTED_RUNNERS = frozenset({"claude_cli", "opencode", "handoff"})
SUPPORTED_VERIFICATION_SOURCES = frozenset({"profile", "ci"})


def load_project_config(path: str | Path) -> ProjectConfig:
    """Load a JSON or TOML project config and return validated config."""

    config_path = Path(path)
    try:
        if config_path.suffix.lower() == ".json":
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        elif config_path.suffix.lower() in {".toml", ".tml"}:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        else:
            raise ProjectConfigError(f"{config_path}: expected .json or .toml config")
    except OSError as exc:
        raise ProjectConfigError(f"cannot read project config {config_path}: {exc}") from exc
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"project config {config_path} is not valid {config_path.suffix}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"{config_path}: top-level config must be an object/table")
    return project_config_from_dict(raw, source=str(config_path))


def project_config_from_dict(data: dict[str, Any], *, source: str = "<dict>") -> ProjectConfig:
    """Parse and validate an in-memory project configuration."""

    secret_finding = _project_config_secret_finding(data)
    if secret_finding is not None:
        raise ProjectConfigError(
            f"{source}: secret_lint_blocked: plaintext secret-like value at {secret_finding.field_path} "
            f"matched {secret_finding.pattern_id}; use env:, keyring:, or file: references"
        )

    project_data = data.get("project") if isinstance(data.get("project"), dict) else {}
    name = _project_string(data, project_data, "name", source)
    raw_project_type = _project_string(data, project_data, "project_type", source, aliases=("type",))
    project_type = normalize_project_type(raw_project_type)
    if raw_project_type.strip().lower() not in SUPPORTED_PROJECT_TYPES:
        raise ProjectConfigError(
            f"{source}: unsupported project_type {project_type!r}; expected one of {sorted(PUBLIC_PROJECT_TYPES)}"
        )
    default_runner = _optional_string(data.get("default_runner", project_data.get("default_runner", ""))).lower()
    if default_runner and default_runner not in SUPPORTED_RUNNERS:
        raise ProjectConfigError(
            f"{source}: unsupported default_runner {default_runner!r}; expected one of {sorted(SUPPORTED_RUNNERS)}"
        )

    task_sources = tuple(
        _task_source(item, source=source, index=index)
        for index, item in enumerate(data.get("task_sources", data.get("sources")) or ())
    )
    repositories = tuple(
        _repository(item, source=source, index=index) for index, item in enumerate(data.get("repositories") or ())
    )
    verification_commands = tuple(
        _verification_command(item, source=source, index=index)
        for index, item in enumerate(_verification_items(data, source=source))
    )
    sinks = tuple(_sink(item, source=source, index=index) for index, item in enumerate(data.get("sinks") or ()))

    raw_pipeline = _pipeline_items(data, source=source)
    pipeline = tuple(_pipeline_step(item, source=source, index=index) for index, item in enumerate(raw_pipeline))
    has_explicit_pipeline = bool(pipeline)
    if not pipeline:
        pipeline = preset_pipeline(project_type)

    human_loop = _human_loop(data.get("human_loop") or {})
    if human_loop.enabled and human_loop.required_steps:
        required = set(human_loop.required_steps)
        pipeline = tuple(replace(step, human_gate=step.human_gate or step.id in required) for step in pipeline)

    cfg = ProjectConfig(
        name=name,
        project_type=project_type,
        description=_optional_string(data.get("description", "")),
        default_runner=default_runner,
        task_sources=task_sources,
        repositories=repositories,
        pipeline=pipeline,
        verification_commands=verification_commands,
        sinks=sinks,
        human_loop=human_loop,
        environment=_optional_string(data.get("environment", "")),
        release_policy=_string_dict(data.get("release_policy") or {}, source=source, field="release_policy"),
        outputs=_string_dict(data.get("outputs") or {}, source=source, field="outputs"),
    )
    _validate_config(cfg, source=source, validate_repo_roles=has_explicit_pipeline)
    return cfg


def _project_config_secret_finding(data: dict[str, Any]) -> SecretFinding | None:
    flattened: dict[str, object] = {}
    _flatten_project_config(data, "", flattened)
    return scan_mapping(flattened, field_path_prefix="project_config")


def _flatten_project_config(value: object, field_path: str, flattened: dict[str, object]) -> None:
    if isinstance(value, dict):
        for raw_key, nested_value in value.items():
            key = str(raw_key)
            child_path = f"{field_path}.{key}" if field_path else key
            _flatten_project_config(nested_value, child_path, flattened)
        return
    if isinstance(value, (list, tuple)):
        for index, nested_value in enumerate(value):
            child_path = f"{field_path}[{index}]" if field_path else f"[{index}]"
            _flatten_project_config(nested_value, child_path, flattened)
        return
    if field_path:
        flattened[field_path] = value


def _validate_config(config: ProjectConfig, *, source: str, validate_repo_roles: bool) -> None:
    step_ids: set[str] = set()
    for step in config.pipeline:
        if step.id in step_ids:
            raise ProjectConfigError(f"{source}: duplicate pipeline step id {step.id!r}")
        step_ids.add(step.id)
    if not config.human_loop.enabled and config.human_loop.required_steps:
        raise ProjectConfigError(f"{source}: human_loop.enabled is false but required_steps are configured")
    for required_step in config.human_loop.required_steps:
        if required_step not in step_ids:
            raise ProjectConfigError(f"{source}: required human_loop step {required_step!r} is not in pipeline")
    repo_by_role = {repo.role: repo for repo in config.repositories}
    repo_roles = set(repo_by_role)
    _validate_configured_sinks(config, source=source, repo_by_role=repo_by_role)
    for step in config.pipeline:
        missing = [dep for dep in step.depends_on if dep not in step_ids]
        if missing:
            raise ProjectConfigError(f"{source}: step {step.id!r} depends on unknown step(s): {', '.join(missing)}")
        if validate_repo_roles and step.repo_role and step.repo_role not in repo_roles:
            raise ProjectConfigError(f"{source}: step {step.id!r} references unknown repo_role {step.repo_role!r}")


def _validate_configured_sinks(
    config: ProjectConfig, *, source: str, repo_by_role: dict[str, RepositoryConfig]
) -> None:
    for sink in config.sinks:
        sink_config = sink.config or {}
        if sink.type == "ci_status":
            target = _ci_status_sink_target(sink_config)
            if not target.startswith("ci://"):
                raise ProjectConfigError(f"{source}: ci_status sink requires target to start with ci://")
            _sink_non_negative_int(
                sink_config.get("repair_max_attempts", 0),
                source=source,
                field="ci_status.repair_max_attempts",
            )
            continue
        if sink.type != "github_pr":
            continue
        repo_role = _optional_string(sink_config.get("repo_role", "")).lower()
        if repo_role and repo_role not in repo_by_role:
            raise ProjectConfigError(f"{source}: github_pr sink references unknown repo_role {repo_role!r}")
        repo = repo_by_role.get(repo_role) if repo_role else _first_repo_target(repo_by_role.values())
        if repo is None or not repo.is_repo_target():
            raise ProjectConfigError(f"{source}: github_pr sink requires a provider repo target")
        if not config.human_loop.enabled and not _github_pr_sink_has_profile_approval(sink_config):
            raise ProjectConfigError(
                f"{source}: github_pr sink requires human_loop.enabled=true or explicit profile approval"
            )


def _github_pr_sink_has_profile_approval(config: dict[str, str]) -> bool:
    return _optional_string(config.get("approval", "")).lower() == "profile" or _truthy(config.get("profile_approved"))


def _first_repo_target(repositories: Iterable[RepositoryConfig]) -> RepositoryConfig | None:
    for repo in repositories:
        if repo.is_repo_target():
            return repo
    return None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _required_string(data: dict[str, Any], field: str, source: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{source}: {field!r} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _project_string(
    data: dict[str, Any],
    project_data: dict[str, Any],
    field: str,
    source: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str:
    if field in data:
        return _required_string(data, field, source)
    if field in project_data:
        return _required_string(project_data, field, f"{source}: project")
    for alias in aliases:
        if alias in data:
            return _required_string(data, alias, source)
        if alias in project_data:
            return _required_string(project_data, alias, f"{source}: project")
    return _required_string(data, field, source)


def _task_source(data: Any, *, source: str, index: int) -> TaskSourceConfig:
    item = _object(data, source=source, field=f"task_sources[{index}]")
    kind = _required_string(item, "kind" if "kind" in item else "type", f"{source}: task_sources[{index}]").lower()
    if kind not in SUPPORTED_TASK_SOURCE_KINDS:
        raise ProjectConfigError(
            f"{source}: unsupported task source kind {kind!r}; expected one of {sorted(SUPPORTED_TASK_SOURCE_KINDS)}"
        )
    return TaskSourceConfig(
        kind=kind,
        ref=_optional_string(item.get("ref", "")),
        query=_optional_string(item.get("query", "")),
        url=_optional_string(item.get("url", "")),
        filters=_string_dict(item.get("filters") or {}, source=source, field=f"task_sources[{index}].filters"),
    )


def _pipeline_items(data: dict[str, Any], *, source: str) -> tuple[Any, ...]:
    raw = data.get("pipeline") or ()
    if isinstance(raw, dict):
        stages = raw.get("stages") or ()
        if not isinstance(stages, (list, tuple)):
            raise ProjectConfigError(f"{source}: pipeline.stages must be a list of objects")
        return tuple(stages)
    if not isinstance(raw, (list, tuple)):
        raise ProjectConfigError(f"{source}: pipeline must be a list of objects or an object with stages")
    return tuple(raw)


def _sink(data: Any, *, source: str, index: int) -> SinkConfig:
    item = _object(data, source=source, field=f"sinks[{index}]")
    sink_type = _required_string(item, "type", f"{source}: sinks[{index}]").lower()
    if sink_type not in SUPPORTED_SINK_TYPES:
        raise ProjectConfigError(
            f"{source}: unsupported sink type {sink_type!r}; expected one of {sorted(SUPPORTED_SINK_TYPES)}"
        )
    return SinkConfig(
        type=sink_type,
        config=_string_dict(item.get("config") or {}, source=source, field=f"sinks[{index}].config"),
    )


def _verification_items(data: dict[str, Any], *, source: str) -> tuple[Any, ...]:
    raw = data.get("verification_commands")
    if raw is not None:
        if not isinstance(raw, (list, tuple)):
            raise ProjectConfigError(f"{source}: verification_commands must be a list of objects")
        return tuple(raw)
    verification = data.get("verification") or {}
    if not isinstance(verification, dict):
        raise ProjectConfigError(f"{source}: verification must be an object/table")
    raw_commands = verification.get("commands") or ()
    if not isinstance(raw_commands, (list, tuple)):
        raise ProjectConfigError(f"{source}: verification.commands must be a list of objects")
    return tuple(raw_commands)


def _verification_command(data: Any, *, source: str, index: int) -> VerificationCommandConfig:
    item = _object(data, source=source, field=f"verification.commands[{index}]")
    command_source = f"{source}: verification.commands[{index}]"
    name = _required_string(item, "name", command_source)
    command = _required_string(item, "command", command_source)
    verification_source = (_optional_string(item.get("source", "profile")) or "profile").lower()
    if verification_source not in SUPPORTED_VERIFICATION_SOURCES:
        raise ProjectConfigError(
            f"{source}: verification command {name!r} has unsupported source {verification_source!r}; "
            f"expected one of {sorted(SUPPORTED_VERIFICATION_SOURCES)}"
        )
    repair_max_attempts = _non_negative_int(
        item.get("repair_max_attempts", 0),
        source=source,
        field=f"verification.commands[{index}].repair_max_attempts",
    )
    if verification_source == "ci":
        if not command.startswith("ci://"):
            raise ProjectConfigError(f"{source}: ci verification command {name!r} must start with ci://")
    else:
        scan = scan_command(command)
        if scan.blocked:
            raise ProjectConfigError(
                f"{source}: unsafe verification command {name!r} blocked by {scan.pattern_matched or 'shell policy'}"
            )
    return VerificationCommandConfig(
        name=name,
        command=command,
        required=bool(item.get("required", True)),
        source=verification_source,
        repair_max_attempts=repair_max_attempts,
    )


def _repository(data: Any, *, source: str, index: int) -> RepositoryConfig:
    item = _object(data, source=source, field=f"repositories[{index}]")
    return RepositoryConfig(
        id=_required_string(item, "id", f"{source}: repositories[{index}]"),
        role=_required_string(item, "role", f"{source}: repositories[{index}]").lower(),
        provider=_optional_string(item.get("provider", "")).lower(),
        repo_full_name=_optional_string(item.get("repo_full_name", item.get("repo", ""))),
        clone_url=_optional_string(item.get("clone_url", "")),
        path=_optional_string(item.get("path", "")),
        default_branch=_optional_string(item.get("default_branch", "")),
        ref=_optional_string(item.get("ref", "")),
        ref_type=_optional_string(item.get("ref_type", "")),
        suite=_optional_string(item.get("suite", "")),
        writable=bool(item.get("writable", False)),
    )


def _pipeline_step(data: Any, *, source: str, index: int) -> PipelineStepConfig:
    item = _object(data, source=source, field=f"pipeline[{index}]")
    step_source = f"{source}: pipeline[{index}]"
    kind_field = "kind" if "kind" in item else "type"
    return PipelineStepConfig(
        id=_required_string(item, "id", step_source),
        kind=_required_string(item, kind_field, step_source),
        title=_required_string(item, "title", step_source),
        description=_optional_string(item.get("description", "")),
        depends_on=_string_tuple(item.get("depends_on") or (), source=source, field=f"pipeline[{index}].depends_on"),
        repo_role=_optional_string(item.get("repo_role", "")).lower(),
        human_gate=bool(item.get("human_gate", False)),
        commands=_string_tuple(item.get("commands") or (), source=source, field=f"pipeline[{index}].commands"),
        outputs=_string_tuple(item.get("outputs") or (), source=source, field=f"pipeline[{index}].outputs"),
        acceptance_criteria=_string_tuple(
            item.get("acceptance_criteria") or (),
            source=source,
            field=f"pipeline[{index}].acceptance_criteria",
        ),
        test_steps=_string_tuple(item.get("test_steps") or (), source=source, field=f"pipeline[{index}].test_steps"),
        priority=_optional_string(item.get("priority", "medium")) or "medium",
        agent_mode=_optional_string(item.get("agent_mode", "implementation")) or "implementation",
    )


def _human_loop(data: Any) -> HumanLoopConfig:
    if not isinstance(data, dict):
        return HumanLoopConfig()
    return HumanLoopConfig(
        enabled=bool(data.get("enabled", True)),
        required_steps=tuple(str(item) for item in data.get("required_steps") or ()),
        approval_channel=_optional_string(data.get("approval_channel", "")),
        instructions=_optional_string(data.get("instructions", "")),
    )


def _object(data: Any, *, source: str, field: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProjectConfigError(f"{source}: {field} must be an object/table")
    return data


def _string_tuple(data: Any, *, source: str, field: str) -> tuple[str, ...]:
    if not isinstance(data, (list, tuple)):
        raise ProjectConfigError(f"{source}: {field} must be a list of strings")
    out: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, str):
            raise ProjectConfigError(f"{source}: {field}[{index}] must be a string")
        out.append(item)
    return tuple(out)


def _string_dict(data: Any, *, source: str, field: str) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ProjectConfigError(f"{source}: {field} must be an object/table")
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ProjectConfigError(f"{source}: {field} keys must be strings")
        out[key] = str(value)
    return out


def _non_negative_int(value: Any, *, source: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectConfigError(f"{source}: {field} must be a non-negative integer")
    return value


def _sink_non_negative_int(value: Any, *, source: str, field: str) -> int:
    if isinstance(value, bool):
        raise ProjectConfigError(f"{source}: {field} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise ProjectConfigError(f"{source}: {field} must be a non-negative integer")
    if parsed < 0:
        raise ProjectConfigError(f"{source}: {field} must be a non-negative integer")
    return parsed


def _ci_status_sink_target(config: dict[str, str]) -> str:
    return _optional_string(config.get("target", config.get("command", config.get("ci_target", ""))))


def load_project_map(path: str | Path) -> ProjectMapConfig:
    """Load a JSON or TOML project map and return validated config."""

    map_path = Path(path)
    try:
        if map_path.suffix.lower() == ".json":
            raw = json.loads(map_path.read_text(encoding="utf-8"))
        elif map_path.suffix.lower() in {".toml", ".tml"}:
            raw = tomllib.loads(map_path.read_text(encoding="utf-8"))
        else:
            raise ProjectConfigError(f"{map_path}: expected .json or .toml project map")
    except OSError as exc:
        raise ProjectConfigError(f"cannot read project map {map_path}: {exc}") from exc
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"project map {map_path} is not valid {map_path.suffix}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"{map_path}: top-level project map must be an object/table")
    return project_map_from_dict(raw, source=str(map_path))


def project_map_from_dict(raw: dict[str, Any], *, source: str = "<dict>") -> ProjectMapConfig:
    """Convert a raw dict to a validated ProjectMapConfig."""

    try:
        version = _optional_string(raw.get("version", "1.0"))

        mappings = []
        raw_mappings = raw.get("mappings", [])
        if not isinstance(raw_mappings, list):
            raise ProjectConfigError(f"{source}: 'mappings' must be a list")

        for i, entry_dict in enumerate(raw_mappings):
            if not isinstance(entry_dict, dict):
                raise ProjectConfigError(f"{source}: mapping[{i}] must be an object")

            jira_key = _required_string(entry_dict, "jira_project_key", f"{source}:mappings[{i}]")
            git_repo_ids = _string_tuple(
                entry_dict.get("git_repository_ids", []), source=source, field=f"mappings[{i}].git_repository_ids"
            )
            git_repo_paths = _string_tuple(
                entry_dict.get("git_repository_paths", []), source=source, field=f"mappings[{i}].git_repository_paths"
            )
            label_filters = _string_tuple(
                entry_dict.get("issue_label_filters", []), source=source, field=f"mappings[{i}].issue_label_filters"
            )
            default_repo = _optional_string(entry_dict.get("default_repo_id", ""))
            custom_fields = entry_dict.get("custom_field_mappings")
            if custom_fields is None:
                field_mappings = {}
            else:
                field_mappings = _string_dict(
                    custom_fields, source=source, field=f"mappings[{i}].custom_field_mappings"
                )

            entry = ProjectMapEntry(
                jira_project_key=jira_key,
                git_repository_ids=git_repo_ids,
                git_repository_paths=git_repo_paths,
                issue_label_filters=label_filters,
                default_repo_id=default_repo,
                custom_field_mappings=field_mappings,
            )
            mappings.append(entry)

        default_mapping = None
        raw_default = raw.get("default_mapping")
        if raw_default is not None:
            if not isinstance(raw_default, dict):
                raise ProjectConfigError(f"{source}: 'default_mapping' must be an object")

            jira_key = _required_string(raw_default, "jira_project_key", f"{source}:default_mapping")
            git_repo_ids = _string_tuple(
                raw_default.get("git_repository_ids", []), source=source, field="default_mapping.git_repository_ids"
            )
            git_repo_paths = _string_tuple(
                raw_default.get("git_repository_paths", []), source=source, field="default_mapping.git_repository_paths"
            )
            label_filters = _string_tuple(
                raw_default.get("issue_label_filters", []), source=source, field="default_mapping.issue_label_filters"
            )
            default_repo = _optional_string(raw_default.get("default_repo_id", ""))
            custom_fields = raw_default.get("custom_field_mappings")
            if custom_fields is None:
                field_mappings = {}
            else:
                field_mappings = _string_dict(
                    custom_fields, source=source, field="default_mapping.custom_field_mappings"
                )

            default_mapping = ProjectMapEntry(
                jira_project_key=jira_key,
                git_repository_ids=git_repo_ids,
                git_repository_paths=git_repo_paths,
                issue_label_filters=label_filters,
                default_repo_id=default_repo,
                custom_field_mappings=field_mappings,
            )

        fallback_repos = _string_tuple(raw.get("fallback_repo_ids", []), source=source, field="fallback_repo_ids")

        return ProjectMapConfig(
            version=version,
            mappings=tuple(mappings),
            default_mapping=default_mapping,
            fallback_repo_ids=fallback_repos,
        )
    except ProjectConfigError:
        raise
    except KeyError as exc:
        raise ProjectConfigError(f"{source}: missing required project map field: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ProjectConfigError(f"{source}: invalid project map configuration: {exc}") from exc
