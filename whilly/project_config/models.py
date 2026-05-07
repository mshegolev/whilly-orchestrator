"""Dataclasses for domain-adaptive project configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TaskSourceConfig:
    """External source of work: Jira, GitHub, manual PRD, etc."""

    kind: str
    ref: str = ""
    query: str = ""
    url: str = ""
    filters: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["filters"] = dict(self.filters or {})
        return out


@dataclass(frozen=True)
class SinkConfig:
    """Configured result sink for project/profile output."""

    type: str
    config: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["config"] = dict(self.config or {})
        return out


@dataclass(frozen=True)
class VerificationCommandConfig:
    """Post-execution verification command configured by a project/profile."""

    name: str
    command: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepositoryConfig:
    """Repository or local checkout used by a configured pipeline."""

    id: str
    role: str
    provider: str = ""
    repo_full_name: str = ""
    clone_url: str = ""
    path: str = ""
    default_branch: str = ""
    ref: str = ""
    ref_type: str = ""
    suite: str = ""
    writable: bool = False

    def is_repo_target(self) -> bool:
        return bool(self.provider and self.repo_full_name)

    def repo_target_id(self) -> str:
        if not self.is_repo_target():
            return ""
        return f"{self.provider}:{self.repo_full_name}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PipelineStepConfig:
    """One configured orchestration stage."""

    id: str
    kind: str
    title: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    repo_role: str = ""
    human_gate: bool = False
    commands: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    test_steps: tuple[str, ...] = ()
    priority: str = "medium"
    agent_mode: str = "implementation"

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["depends_on"] = list(self.depends_on)
        out["commands"] = list(self.commands)
        out["outputs"] = list(self.outputs)
        out["acceptance_criteria"] = list(self.acceptance_criteria)
        out["test_steps"] = list(self.test_steps)
        return out


@dataclass(frozen=True)
class HumanLoopConfig:
    """Human review/approval policy for configured pipelines."""

    enabled: bool = True
    required_steps: tuple[str, ...] = ()
    approval_channel: str = ""
    instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["required_steps"] = list(self.required_steps)
        return out


@dataclass(frozen=True)
class ProjectConfig:
    """Project-level configuration used to generate domain-adaptive plans."""

    name: str
    project_type: str
    description: str = ""
    default_runner: str = ""
    task_sources: tuple[TaskSourceConfig, ...] = ()
    repositories: tuple[RepositoryConfig, ...] = ()
    pipeline: tuple[PipelineStepConfig, ...] = ()
    verification_commands: tuple[VerificationCommandConfig, ...] = ()
    sinks: tuple[SinkConfig, ...] = ()
    human_loop: HumanLoopConfig = HumanLoopConfig()
    environment: str = ""
    release_policy: dict[str, str] | None = None
    outputs: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "project_type": self.project_type,
            "description": self.description,
            "default_runner": self.default_runner,
            "task_sources": [source.to_dict() for source in self.task_sources],
            "repositories": [repo.to_dict() for repo in self.repositories],
            "pipeline": [step.to_dict() for step in self.pipeline],
            "verification_commands": [command.to_dict() for command in self.verification_commands],
            "sinks": [sink.to_dict() for sink in self.sinks],
            "human_loop": self.human_loop.to_dict(),
            "environment": self.environment,
            "release_policy": dict(self.release_policy or {}),
            "outputs": dict(self.outputs or {}),
        }
