"""Domain models for Whilly v4.0 (PRD FR-2.2, NFR-4, Module structure).

All models are frozen dataclasses to give us value-object semantics and a
working ``__hash__`` (so :class:`Task` instances can live in sets — useful for
the scheduler in TASK-013). Collections default to ``tuple`` rather than
``list`` because :pep:`557`'s ``frozen=True`` only forbids attribute
reassignment, not mutation of mutable contents.

This module is part of the ``whilly.core`` layer (Hexagonal architecture, PRD
TC-8 / SC-6): no I/O, no networking, no subprocess, no asyncio. Enforced by
``.importlinter``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

type TaskId = str
type PlanId = str
type WorkerId = str


class TaskStatus(str, Enum):
    """Lifecycle states for a :class:`Task` (PRD FR-2.2).

    Inheriting from ``str`` makes the enum trivially JSON-serialisable and
    interoperable with raw SQL ``text``/``varchar`` columns without bespoke
    converters in the adapter layer.
    """

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Priority(str, Enum):
    """Task priority bucket used by the scheduler (PRD FR-3.4)."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Task:
    """A single unit of work in a plan.

    The ``version`` field is the optimistic-locking counter (PRD FR-2.4): every
    state transition increments it and the Postgres adapter writes
    ``UPDATE ... WHERE id = $1 AND version = $2`` so concurrent claimers
    detect lost updates.

    All collection-typed fields default to empty tuples so the dataclass stays
    immutable end-to-end.
    """

    id: TaskId
    status: TaskStatus
    dependencies: tuple[TaskId, ...] = ()
    key_files: tuple[str, ...] = ()
    priority: Priority = Priority.MEDIUM
    description: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    test_steps: tuple[str, ...] = ()
    prd_requirement: str = ""
    version: int = 0


@dataclass(frozen=True)
class Plan:
    """A named DAG of :class:`Task` instances (PRD FR-3.1)."""

    id: PlanId
    name: str
    tasks: tuple[Task, ...] = ()


@dataclass(frozen=True)
class Event:
    """Audit-log entry for a task state transition (PRD FR-2.4).

    One row per transition is appended to the ``events`` table in the same
    transaction as the corresponding ``tasks`` UPDATE, giving an immutable
    history that the dashboard and tests can rely on.

    ``payload`` is typed as :class:`~collections.abc.Mapping` rather than
    ``dict`` to discourage in-place mutation by readers; the JSON-shaped
    contents (reason, version, error message, etc.) are caller-defined.
    """

    id: int
    task_id: TaskId
    event_type: str
    payload: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class WorkerHandle:
    """Server-side record of a registered worker (PRD FR-1.1, FR-1.2).

    ``token_hash`` stores a hash of the per-worker bearer token (never the
    plaintext) so the database can authenticate heartbeats and claim/complete
    requests without holding reusable credentials at rest.
    """

    worker_id: WorkerId
    hostname: str
    last_heartbeat: datetime
    token_hash: str
