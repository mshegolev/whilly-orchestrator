from __future__ import annotations

from whilly.repair import (
    REPAIR_ACTION_ESCALATE,
    REPAIR_ACTION_REQUEST,
    REPAIR_REASON_BUDGET_EXHAUSTED,
    REPAIR_REASON_DISABLED,
    RepairBudget,
    RepairTrigger,
    decide_repair,
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
