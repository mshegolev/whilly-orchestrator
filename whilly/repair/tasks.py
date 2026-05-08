"""Deterministic repair task construction."""

from __future__ import annotations

from whilly.core.models import Task, TaskStatus
from whilly.repair.models import RepairDecision, RepairTrigger
from whilly.repair.policy import REPAIR_ACTION_REQUEST


def build_repair_task(orig_task: Task, trigger: RepairTrigger, decision: RepairDecision) -> Task:
    """Build a new repair task without depending on the failed original task."""

    if decision.action != REPAIR_ACTION_REQUEST or not decision.repair_task_id:
        raise ValueError("build_repair_task requires a request_repair decision with repair_task_id")

    description = "\n".join(
        (
            f"Repair attempt {decision.attempt}/{decision.max_attempts}",
            f"Original task: {orig_task.id}",
            f"Trigger: {trigger.trigger_type} via {trigger.trigger_event_type}",
            "Use bounded repair audit events for failure evidence; do not copy raw provider output into this task.",
        )
    )
    acceptance_criteria = (
        f"Address the {trigger.trigger_type} failure that triggered bounded repair.",
        f"Respect repair budget {decision.attempt}/{decision.max_attempts}.",
        "Run the original verification or CI gate before marking this repair task done.",
    )
    test_steps = (
        f"Review the {trigger.trigger_event_type} audit event for bounded failure context.",
        "Run the relevant verification or CI command after applying the repair.",
    )

    return Task(
        id=decision.repair_task_id,
        status=TaskStatus.PENDING,
        dependencies=(),
        key_files=orig_task.key_files,
        priority=orig_task.priority,
        description=description,
        acceptance_criteria=acceptance_criteria,
        test_steps=test_steps,
        prd_requirement=f"Bounded repair for {orig_task.id}",
        version=0,
        repo_target_id=orig_task.repo_target_id,
    )


__all__ = ["build_repair_task"]
