"""Universal project configuration for Whilly pipelines."""

from whilly.project_config.loader import ProjectConfigError, load_project_config, project_config_from_dict
from whilly.project_config.plan_builder import build_plan_payload
from whilly.project_config.presets import preset_pipeline

__all__ = [
    "ProjectConfigError",
    "build_plan_payload",
    "load_project_config",
    "preset_pipeline",
    "project_config_from_dict",
]
