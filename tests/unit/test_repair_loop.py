from __future__ import annotations

import pytest

from whilly.core.models import Priority, Task, TaskStatus
from whilly.repair import (
    REPAIR_ACTION_ESCALATE,
    REPAIR_ACTION_REQUEST,
    REPAIR_ATTEMPT_COMPLETED_EVENT,
    REPAIR_ATTEMPT_REQUESTED_EVENT,
    REPAIR_ESCALATED_EVENT,
    REPAIR_REASON_BUDGET_EXHAUSTED,
    REPAIR_REASON_DISABLED,
    RepairBudget,
    RepairTrigger,
    build_repair_task,
    decide_repair,
    make_repair_attempt_completed_event,
    make_repair_attempt_requested_event,
    make_repair_escalated_event,
    parse_repair_attempt,
)


def _trigger(*, current_attempt: int = 0, orig_task_id: str = "task-1") -> RepairTrigger:
    return RepairTrigger(
        orig_task_id=orig_task_id,
        plan_id="plan-1",
        trigger_type="verification",
        trigger_event_type="verification.failed",
        reason="required verification failed",
        current_attempt=current_attempt,
        last_failure_event_type="verification.failed",
        last_repair_task_id=f"{orig_task_id}-repair-{current_attempt}" if current_attempt else "",
    )


def test_repair_disabled_escalates_without_task_request() -> None:
    trigger = _trigger()

    decision = decide_repair(trigger, RepairBudget(max_attempts=0))

    assert decision.action == REPAIR_ACTION_ESCALATE
    assert decision.reason == REPAIR_REASON_DISABLED
    assert decision.attempt == 0
    assert decision.max_attempts == 0
    assert decision.repair_task_id == ""
    assert decision.trigger == trigger


def test_repair_budget_requests_next_attempt() -> None:
    trigger = _trigger(current_attempt=0, orig_task_id="build-docs")

    decision = decide_repair(trigger, RepairBudget(max_attempts=2))

    assert decision.action == REPAIR_ACTION_REQUEST
    assert decision.reason == ""
    assert decision.attempt == 1
    assert decision.max_attempts == 2
    assert decision.repair_task_id == "build-docs-repair-1"
    assert decision.trigger == trigger


def test_repair_budget_escalates_when_exhausted() -> None:
    trigger = _trigger(current_attempt=2, orig_task_id="build-docs")

    decision = decide_repair(trigger, RepairBudget(max_attempts=2))

    assert decision.action == REPAIR_ACTION_ESCALATE
    assert decision.reason == REPAIR_REASON_BUDGET_EXHAUSTED
    assert decision.attempt == 2
    assert decision.max_attempts == 2
    assert decision.repair_task_id == ""
    assert decision.trigger == trigger


def test_parse_nested_repair_task_id_uses_final_suffix() -> None:
    assert parse_repair_attempt("build-docs-repair-1-repair-2") == ("build-docs-repair-1", 2)
    assert parse_repair_attempt("build-docs") is None


def test_repair_requested_event_payload_is_auditable() -> None:
    trigger = _trigger(orig_task_id="build-docs")
    decision = decide_repair(trigger, RepairBudget(max_attempts=2))

    event = make_repair_attempt_requested_event(trigger, decision)

    assert event.task_id == "build-docs"
    assert event.event_type == REPAIR_ATTEMPT_REQUESTED_EVENT
    assert set(event.payload) == {
        "task_id",
        "plan_id",
        "repair_task_id",
        "attempt",
        "max_attempts",
        "trigger_type",
        "trigger_event_type",
        "reason",
    }
    assert event.payload["repair_task_id"] == "build-docs-repair-1"
    assert event.payload["attempt"] == 1
    assert event.payload["max_attempts"] == 2
    assert event.payload["trigger_type"] == "verification"
    assert event.payload["trigger_event_type"] == "verification.failed"
    assert event.payload["reason"] == "required verification failed"


def test_repair_completed_event_payload_uses_repair_task_id() -> None:
    event = make_repair_attempt_completed_event(
        orig_task_id="build-docs",
        repair_task_id="build-docs-repair-1",
        plan_id="plan-1",
        attempt=1,
        max_attempts=2,
        terminal_status="DONE",
    )

    assert event.task_id == "build-docs-repair-1"
    assert event.event_type == REPAIR_ATTEMPT_COMPLETED_EVENT
    assert event.payload == {
        "task_id": "build-docs-repair-1",
        "plan_id": "plan-1",
        "orig_task_id": "build-docs",
        "attempt": 1,
        "max_attempts": 2,
        "terminal_status": "DONE",
    }


def test_repair_escalated_event_payload_names_exhaustion_reason() -> None:
    trigger = _trigger(current_attempt=2, orig_task_id="build-docs")
    decision = decide_repair(trigger, RepairBudget(max_attempts=2))

    event = make_repair_escalated_event(trigger, decision)

    assert event.task_id == "build-docs"
    assert event.event_type == REPAIR_ESCALATED_EVENT
    assert event.payload == {
        "task_id": "build-docs",
        "plan_id": "plan-1",
        "attempts": 2,
        "max_attempts": 2,
        "trigger_type": "verification",
        "last_failure_event_type": "verification.failed",
        "last_repair_task_id": "build-docs-repair-2",
        "reason": REPAIR_REASON_BUDGET_EXHAUSTED,
    }


def test_build_repair_task_has_no_failed_task_dependency() -> None:
    orig_task = Task(
        id="build-docs",
        status=TaskStatus.FAILED,
        dependencies=("setup",),
        key_files=("docs/index.md",),
        priority=Priority.HIGH,
        description="do not copy raw STDOUT into repair prompts",
        acceptance_criteria=("old criterion",),
        test_steps=("old test",),
        prd_requirement="DOC-01",
        version=7,
        repo_target_id="docs-repo",
    )
    trigger = _trigger(orig_task_id=orig_task.id)
    decision = decide_repair(trigger, RepairBudget(max_attempts=2))

    repair_task = build_repair_task(orig_task, trigger, decision)

    assert repair_task.id == "build-docs-repair-1"
    assert repair_task.status == TaskStatus.PENDING
    assert repair_task.dependencies == ()
    assert repair_task.key_files == ("docs/index.md",)
    assert repair_task.priority == Priority.HIGH
    assert repair_task.repo_target_id == "docs-repo"
    assert repair_task.version == 0
    assert "Repair attempt 1/2" in repair_task.description
    assert "Original task: build-docs" in repair_task.description
    assert "STDOUT" not in repair_task.description
    assert repair_task.prd_requirement == "Bounded repair for build-docs"


def test_build_repair_task_rejects_escalation_decision() -> None:
    orig_task = Task(id="build-docs", status=TaskStatus.FAILED)
    trigger = _trigger(current_attempt=2, orig_task_id=orig_task.id)
    decision = decide_repair(trigger, RepairBudget(max_attempts=2))

    with pytest.raises(ValueError, match="request_repair"):
        build_repair_task(orig_task, trigger, decision)
