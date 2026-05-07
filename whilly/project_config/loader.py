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
    TaskSourceConfig,
)
from whilly.project_config.presets import SUPPORTED_PROJECT_TYPES, preset_pipeline


class ProjectConfigError(ValueError):
    """Raised when a project configuration is invalid."""


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

    name = _required_string(data, "name", source)
    project_type = _required_string(data, "project_type", source).lower()
    if project_type not in SUPPORTED_PROJECT_TYPES:
        raise ProjectConfigError(
            f"{source}: unsupported project_type {project_type!r}; expected one of {sorted(SUPPORTED_PROJECT_TYPES)}"
        )

    task_sources = tuple(
        _task_source(item, source=source, index=index) for index, item in enumerate(data.get("task_sources") or ())
    )
    repositories = tuple(
        _repository(item, source=source, index=index) for index, item in enumerate(data.get("repositories") or ())
    )

    raw_pipeline = data.get("pipeline") or ()
    pipeline = tuple(_pipeline_step(item, source=source, index=index) for index, item in enumerate(raw_pipeline))
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
        task_sources=task_sources,
        repositories=repositories,
        pipeline=pipeline,
        human_loop=human_loop,
        environment=_optional_string(data.get("environment", "")),
        release_policy=_string_dict(data.get("release_policy") or {}, source=source, field="release_policy"),
        outputs=_string_dict(data.get("outputs") or {}, source=source, field="outputs"),
    )
    _validate_config(cfg, source=source)
    return cfg


def _validate_config(config: ProjectConfig, *, source: str) -> None:
    step_ids: set[str] = set()
    for step in config.pipeline:
        if step.id in step_ids:
            raise ProjectConfigError(f"{source}: duplicate pipeline step id {step.id!r}")
        step_ids.add(step.id)
    repo_roles = {repo.role for repo in config.repositories}
    for step in config.pipeline:
        missing = [dep for dep in step.depends_on if dep not in step_ids]
        if missing:
            raise ProjectConfigError(f"{source}: step {step.id!r} depends on unknown step(s): {', '.join(missing)}")
        if step.repo_role and step.repo_role not in repo_roles:
            raise ProjectConfigError(f"{source}: step {step.id!r} references unknown repo_role {step.repo_role!r}")


def _required_string(data: dict[str, Any], field: str, source: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{source}: {field!r} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _task_source(data: Any, *, source: str, index: int) -> TaskSourceConfig:
    item = _object(data, source=source, field=f"task_sources[{index}]")
    kind = _required_string(item, "kind", f"{source}: task_sources[{index}]")
    return TaskSourceConfig(
        kind=kind.lower(),
        ref=_optional_string(item.get("ref", "")),
        query=_optional_string(item.get("query", "")),
        url=_optional_string(item.get("url", "")),
        filters=_string_dict(item.get("filters") or {}, source=source, field=f"task_sources[{index}].filters"),
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
    return PipelineStepConfig(
        id=_required_string(item, "id", step_source),
        kind=_required_string(item, "kind", step_source),
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
