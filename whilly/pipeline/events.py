"""Pure helpers for pipeline runtime audit events.

This module intentionally only builds event values. Worker adapters can pass
the resulting fields to ``TaskRepository.record_task_event`` without coupling
pipeline taxonomy code to Postgres, FastAPI, subprocesses, or worker runtime
concerns.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PIPELINE_STAGE_STARTED = "pipeline.stage.started"
PIPELINE_STAGE_SUCCEEDED = "pipeline.stage.succeeded"
PIPELINE_STAGE_FAILED = "pipeline.stage.failed"
PIPELINE_STAGE_SKIPPED = "pipeline.stage.skipped"

_CONFIGURED_REQUIREMENT_RE = re.compile(r"^Configured (?P<project_type>\S+) pipeline step: (?P<stage_id>.+)$")


@dataclass(frozen=True, slots=True)
class PipelineStageContext:
    """Snapshot of the configured stage currently being executed.

    ``task_id`` is required because the current repository hook records these
    as task-scoped diagnostic events. The remaining identifiers are omitted
    from payloads when unavailable so legacy or profile-free runs can keep
    emitting the compact shape they can actually prove.
    """

    task_id: str
    plan_id: str | None = None
    stage_id: str | None = None
    project_type: str | None = None
    profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineTaskEvent:
    """Record-ready task event produced by the pipeline taxonomy helpers."""

    task_id: str
    event_type: str
    payload: dict[str, Any]
    detail: Mapping[str, Any] | None = None

    def record_task_event_args(self) -> tuple[str, str, dict[str, Any]]:
        """Return positional args for ``TaskRepository.record_task_event``."""

        return (self.task_id, self.event_type, self.payload)

    def record_task_event_kwargs(self) -> dict[str, Mapping[str, Any]]:
        """Return keyword args for ``TaskRepository.record_task_event``."""

        if self.detail is None:
            return {}
        return {"detail": self.detail}


def _put_if_available(payload: dict[str, Any], key: str, value: str | None) -> None:
    if value:
        payload[key] = value


def build_stage_event_payload(context: PipelineStageContext, *, reason: str | None = None) -> dict[str, Any]:
    """Build the deterministic JSON payload for a pipeline stage event."""

    payload: dict[str, Any] = {"task_id": context.task_id}
    _put_if_available(payload, "plan_id", context.plan_id)
    _put_if_available(payload, "stage_id", context.stage_id)
    _put_if_available(payload, "project_type", context.project_type)
    _put_if_available(payload, "profile_id", context.profile_id)
    _put_if_available(payload, "reason", reason)
    return payload


def make_stage_event(
    event_type: str,
    context: PipelineStageContext | None,
    *,
    reason: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> PipelineTaskEvent | None:
    """Build a record-ready stage event, or ``None`` when no context exists."""

    if context is None:
        return None
    return PipelineTaskEvent(
        task_id=context.task_id,
        event_type=event_type,
        payload=build_stage_event_payload(context, reason=reason),
        detail=detail,
    )


def make_stage_started_event(context: PipelineStageContext | None) -> PipelineTaskEvent | None:
    return make_stage_event(PIPELINE_STAGE_STARTED, context)


def make_stage_succeeded_event(context: PipelineStageContext | None) -> PipelineTaskEvent | None:
    return make_stage_event(PIPELINE_STAGE_SUCCEEDED, context)


def make_stage_failed_event(
    context: PipelineStageContext | None,
    *,
    reason: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> PipelineTaskEvent | None:
    return make_stage_event(PIPELINE_STAGE_FAILED, context, reason=reason, detail=detail)


def make_stage_skipped_event(
    context: PipelineStageContext | None,
    *,
    reason: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> PipelineTaskEvent | None:
    return make_stage_event(PIPELINE_STAGE_SKIPPED, context, reason=reason, detail=detail)


def stage_context_from_task(task: Any, plan: Any) -> PipelineStageContext | None:
    """Infer configured-pipeline context from a generated task and plan.

    Profile-free/manual plans return ``None`` so workers avoid fabricating
    stage events when the task does not prove it came from project config.
    """

    requirement = str(getattr(task, "prd_requirement", "") or "").strip()
    match = _CONFIGURED_REQUIREMENT_RE.match(requirement)
    if match is None:
        return None
    stage_id = match.group("stage_id").strip()
    if not stage_id:
        return None

    origin = getattr(plan, "origin", None)
    profile_id = ""
    if str(getattr(origin, "system", "") or "") == "project_config":
        profile_id = str(getattr(origin, "ref", "") or "")

    return PipelineStageContext(
        task_id=str(getattr(task, "id")),
        plan_id=str(getattr(plan, "id")),
        stage_id=stage_id,
        project_type=match.group("project_type"),
        profile_id=profile_id or None,
    )


__all__ = [
    "PIPELINE_STAGE_FAILED",
    "PIPELINE_STAGE_SKIPPED",
    "PIPELINE_STAGE_STARTED",
    "PIPELINE_STAGE_SUCCEEDED",
    "PipelineStageContext",
    "PipelineTaskEvent",
    "build_stage_event_payload",
    "make_stage_event",
    "make_stage_failed_event",
    "make_stage_skipped_event",
    "make_stage_started_event",
    "make_stage_succeeded_event",
    "stage_context_from_task",
]
