"""Pure domain layer for Whilly v4.0 (Hexagonal architecture, PRD TC-8).

This package contains domain models and pure business logic. It must remain
free of I/O, networking, subprocess, ORM, and asyncio imports. The
``.importlinter`` contract (PRD SC-6) enforces this — see TASK-029.

Public surface is re-exported here for ergonomic ``from whilly.core import ...``
usage, but the canonical home of each symbol is its submodule (e.g. ``models``).
"""

from whilly.core.gates import GateVerdict, GateVerdictKind, evaluate_decision_gate
from whilly.core.models import (
    Event,
    Plan,
    PlanId,
    Priority,
    Task,
    TaskId,
    TaskStatus,
    WorkerHandle,
    WorkerId,
)
from whilly.core.state_machine import StateError, Transition, apply_transition

__all__ = [
    "Event",
    "GateVerdict",
    "GateVerdictKind",
    "Plan",
    "PlanId",
    "Priority",
    "StateError",
    "Task",
    "TaskId",
    "TaskStatus",
    "Transition",
    "WorkerHandle",
    "WorkerId",
    "apply_transition",
    "evaluate_decision_gate",
]
