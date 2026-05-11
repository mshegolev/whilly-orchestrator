"""Audit event builders for bounded repair attempts and escalations."""

from __future__ import annotations

from whilly.pipeline.events import PipelineTaskEvent
from whilly.repair.models import RepairDecision, RepairTrigger
from whilly.repair.policy import REPAIR_ACTION_ESCALATE, REPAIR_ACTION_REQUEST

REPAIR_ATTEMPT_REQUESTED_EVENT = "repair.attempt.requested"
REPAIR_ATTEMPT_COMPLETED_EVENT = "repair.attempt.completed"
REPAIR_ESCALATED_EVENT = "repair.escalated"


def make_repair_attempt_requested_event(trigger: RepairTrigger, decision: RepairDecision) -> PipelineTaskEvent:
    """Build the audit event for one requested repair attempt."""

    if decision.action != REPAIR_ACTION_REQUEST or not decision.repair_task_id:
        raise ValueError("repair attempt requested events require a request_repair decision")

    payload = {
        "task_id": trigger.orig_task_id,
        "plan_id": trigger.plan_id,
        "repair_task_id": decision.repair_task_id,
        "attempt": decision.attempt,
        "max_attempts": decision.max_attempts,
        "trigger_type": trigger.trigger_type,
        "trigger_event_type": trigger.trigger_event_type,
        "reason": trigger.reason,
    }
    return PipelineTaskEvent(
        task_id=trigger.orig_task_id,
        event_type=REPAIR_ATTEMPT_REQUESTED_EVENT,
        payload=payload,
    )


def make_repair_attempt_completed_event(
    *,
    orig_task_id: str,
    repair_task_id: str,
    plan_id: str,
    attempt: int,
    max_attempts: int,
    terminal_status: str,
) -> PipelineTaskEvent:
    """Build the audit event emitted when a repair task reaches a terminal status."""

    if terminal_status not in {"DONE", "FAILED"}:
        raise ValueError("terminal_status must be DONE or FAILED")

    payload = {
        "task_id": repair_task_id,
        "plan_id": plan_id,
        "orig_task_id": orig_task_id,
        "attempt": max(0, attempt),
        "max_attempts": max(0, max_attempts),
        "terminal_status": terminal_status,
    }
    return PipelineTaskEvent(
        task_id=repair_task_id,
        event_type=REPAIR_ATTEMPT_COMPLETED_EVENT,
        payload=payload,
    )


def make_repair_escalated_event(trigger: RepairTrigger, decision: RepairDecision) -> PipelineTaskEvent:
    """Build the audit event for disabled or exhausted repair."""

    if decision.action != REPAIR_ACTION_ESCALATE:
        raise ValueError("repair escalation events require an escalate decision")

    payload = {
        "task_id": trigger.orig_task_id,
        "plan_id": trigger.plan_id,
        "attempts": decision.attempt,
        "max_attempts": decision.max_attempts,
        "trigger_type": trigger.trigger_type,
        "last_failure_event_type": trigger.last_failure_event_type or trigger.trigger_event_type,
        "last_repair_task_id": trigger.last_repair_task_id,
        "reason": decision.reason,
    }
    return PipelineTaskEvent(
        task_id=trigger.orig_task_id,
        event_type=REPAIR_ESCALATED_EVENT,
        payload=payload,
    )


__all__ = [
    "REPAIR_ATTEMPT_COMPLETED_EVENT",
    "REPAIR_ATTEMPT_REQUESTED_EVENT",
    "REPAIR_ESCALATED_EVENT",
    "make_repair_attempt_completed_event",
    "make_repair_attempt_requested_event",
    "make_repair_escalated_event",
]
