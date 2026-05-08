"""Bounded repair primitives for verification, CI, and PR feedback failures."""

from whilly.repair.models import RepairBudget, RepairDecision, RepairTrigger
from whilly.repair.policy import (
    REPAIR_ACTION_ESCALATE,
    REPAIR_ACTION_REQUEST,
    REPAIR_REASON_BUDGET_EXHAUSTED,
    REPAIR_REASON_DISABLED,
    current_repair_attempt,
    decide_repair,
    parse_repair_attempt,
)

__all__ = [
    "REPAIR_ACTION_ESCALATE",
    "REPAIR_ACTION_REQUEST",
    "REPAIR_REASON_BUDGET_EXHAUSTED",
    "REPAIR_REASON_DISABLED",
    "RepairBudget",
    "RepairDecision",
    "RepairTrigger",
    "current_repair_attempt",
    "decide_repair",
    "parse_repair_attempt",
]
