"""Typed contracts for bounded repair decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RepairBudget:
    """Explicit cap for repair attempts.

    ``max_attempts=0`` disables repair. Negative values are retained so policy
    can treat them as disabled while still reflecting the configured value.
    """

    max_attempts: int = 0


@dataclass(frozen=True, slots=True)
class RepairTrigger:
    """Evidence describing why a repair decision is being considered."""

    orig_task_id: str
    plan_id: str
    trigger_type: str
    trigger_event_type: str
    reason: str
    current_attempt: int = 0
    last_failure_event_type: str = ""
    last_repair_task_id: str = ""

    def __post_init__(self) -> None:
        if self.current_attempt < 0:
            object.__setattr__(self, "current_attempt", 0)


@dataclass(frozen=True, slots=True)
class RepairDecision:
    """Pure policy output for a repair trigger and budget."""

    action: str
    reason: str
    attempt: int = 0
    max_attempts: int = 0
    repair_task_id: str = ""
    trigger: RepairTrigger | None = None

    def __post_init__(self) -> None:
        if self.attempt < 0:
            object.__setattr__(self, "attempt", 0)
        if self.max_attempts < 0:
            object.__setattr__(self, "max_attempts", 0)
