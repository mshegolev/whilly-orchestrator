"""Pure bounded repair budget policy."""

from __future__ import annotations

import re

from whilly.repair.models import RepairBudget, RepairDecision, RepairTrigger

REPAIR_ACTION_REQUEST = "request_repair"
REPAIR_ACTION_ESCALATE = "escalate"
REPAIR_REASON_DISABLED = "repair_disabled"
REPAIR_REASON_BUDGET_EXHAUSTED = "repair_budget_exhausted"

_REPAIR_TASK_ID_RE = re.compile(r"^(?P<orig>.+)-repair-(?P<attempt>\d+)$")


def parse_repair_attempt(task_id: str) -> tuple[str, int] | None:
    """Parse the final ``-repair-N`` suffix from a repair task id."""

    match = _REPAIR_TASK_ID_RE.match(task_id)
    if match is None:
        return None
    return match.group("orig"), int(match.group("attempt"))


def current_repair_attempt(task_id: str) -> int:
    """Return the parsed repair attempt number, or zero for original tasks."""

    parsed = parse_repair_attempt(task_id)
    if parsed is None:
        return 0
    return parsed[1]


def decide_repair(trigger: RepairTrigger, budget: RepairBudget) -> RepairDecision:
    """Return a deterministic request-or-escalate decision for one trigger."""

    max_attempts = max(0, budget.max_attempts)
    current_attempt = max(0, trigger.current_attempt)
    if budget.max_attempts <= 0:
        return RepairDecision(
            action=REPAIR_ACTION_ESCALATE,
            reason=REPAIR_REASON_DISABLED,
            attempt=current_attempt,
            max_attempts=max_attempts,
            trigger=trigger,
        )
    if current_attempt >= max_attempts:
        return RepairDecision(
            action=REPAIR_ACTION_ESCALATE,
            reason=REPAIR_REASON_BUDGET_EXHAUSTED,
            attempt=current_attempt,
            max_attempts=max_attempts,
            trigger=trigger,
        )

    attempt = current_attempt + 1
    return RepairDecision(
        action=REPAIR_ACTION_REQUEST,
        reason="",
        attempt=attempt,
        max_attempts=max_attempts,
        repair_task_id=f"{trigger.orig_task_id}-repair-{attempt}",
        trigger=trigger,
    )


__all__ = [
    "REPAIR_ACTION_ESCALATE",
    "REPAIR_ACTION_REQUEST",
    "REPAIR_REASON_BUDGET_EXHAUSTED",
    "REPAIR_REASON_DISABLED",
    "current_repair_attempt",
    "decide_repair",
    "parse_repair_attempt",
]
