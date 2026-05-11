"""Audit event builders for CI polling evidence."""

from __future__ import annotations

from typing import Any

from whilly.ci.models import CICheckSummary, CIPollResult, CIPollSpec
from whilly.pipeline.events import PipelineTaskEvent

CI_POLL_STARTED_EVENT = "ci.poll.started"
CI_POLL_RESULT_EVENT = "ci.poll.result"


def make_ci_poll_started_event(task_id: str, spec: CIPollSpec, *, plan_id: str = "") -> PipelineTaskEvent:
    """Build the audit event emitted before a configured CI poll runs."""

    payload: dict[str, Any] = {
        "task_id": task_id,
        "name": spec.name,
        "provider": spec.provider,
        "target": spec.target,
        "required": spec.required,
        "timeout_s": spec.timeout_s,
        "poll_interval_s": spec.poll_interval_s,
        "max_attempts": spec.max_attempts,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    return PipelineTaskEvent(task_id=task_id, event_type=CI_POLL_STARTED_EVENT, payload=payload)


def make_ci_poll_result_event(task_id: str, result: CIPollResult, *, plan_id: str = "") -> PipelineTaskEvent:
    """Build bounded result evidence without persisting raw provider payloads."""

    payload: dict[str, Any] = {
        "task_id": task_id,
        "name": result.name,
        "provider": result.provider,
        "target": result.target,
        "state": result.state,
        "conclusion": result.conclusion,
        "succeeded": result.succeeded,
        "required": result.required,
        "blocking": result.blocking,
        "attempts": result.attempts,
        "max_attempts": result.max_attempts,
        "timed_out": result.timed_out,
        "unavailable": result.unavailable,
        "unauthenticated": result.unauthenticated,
        "duration_s": result.duration_s,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    if result.details_url:
        payload["details_url"] = result.details_url

    return PipelineTaskEvent(
        task_id=task_id,
        event_type=CI_POLL_RESULT_EVENT,
        payload=payload,
        detail={"checks": [_check_to_detail(check) for check in result.checks]},
    )


def _check_to_detail(check: CICheckSummary) -> dict[str, str | None]:
    return {
        "name": check.name,
        "state": check.state,
        "conclusion": check.conclusion,
        "details_url": check.details_url,
    }


__all__ = [
    "CI_POLL_RESULT_EVENT",
    "CI_POLL_STARTED_EVENT",
    "make_ci_poll_result_event",
    "make_ci_poll_started_event",
]
