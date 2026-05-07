"""Load and validate universal project configuration files."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from whilly.project_config.models import (
    HumanLoopConfig,
    PipelineStepConfig,
    ProjectConfig,
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
from whilly.core.agent_runner import scan_command


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
SUPPORTED_SINK_TYPES = frozenset({"github_pr", "github_issue_comment", "jira_comment", "jsonl", "dashboard"})
SUPPORTED_RUNNERS = frozenset({"claude_cli", "opencode", "handoff"})


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
    repo_roles = {repo.role for repo in config.repositories}
    for step in config.pipeline:
        missing = [dep for dep in step.depends_on if dep not in step_ids]
        if missing:
            raise ProjectConfigError(f"{source}: step {step.id!r} depends on unknown step(s): {', '.join(missing)}")
        if validate_repo_roles and step.repo_role and step.repo_role not in repo_roles:
            raise ProjectConfigError(f"{source}: step {step.id!r} references unknown repo_role {step.repo_role!r}")


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
    scan = scan_command(command)
    if scan.blocked:
        raise ProjectConfigError(
            f"{source}: unsafe verification command {name!r} blocked by {scan.pattern_matched or 'shell policy'}"
        )
    return VerificationCommandConfig(
        name=name,
        command=command,
        required=bool(item.get("required", True)),
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
