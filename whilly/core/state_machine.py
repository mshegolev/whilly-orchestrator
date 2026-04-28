"""Task lifecycle state machine for Whilly v4.0 (PRD FR-2.2, NFR-4).

Transitions are expressed as a pure function — :func:`apply_transition` takes a
:class:`~whilly.core.models.Task` plus a :class:`Transition` and returns either
the next :class:`~whilly.core.models.Task` (with ``version`` incremented) or a
:class:`StateError` value describing why the transition was rejected. We
return the error rather than raising it because at the adapter layer a
"transition rejected" outcome is normal flow control: the Postgres adapter
needs to translate it into a ``VersionConflictError`` or a ``409 Conflict``,
not a stack trace.

This module is part of the ``whilly.core`` layer (Hexagonal architecture, PRD
TC-8 / SC-6): no I/O, no networking, no subprocess, no asyncio, no globals
mutated. The only inputs are its arguments; the only output is its return
value. ``apply_transition`` is therefore deterministic and trivially testable
without any fixtures (TASK-006 builds the unit-test suite).

Transition map
--------------
The valid edges in the lifecycle DAG are:

* ``CLAIM``    : ``PENDING``     → ``CLAIMED``
* ``START``    : ``CLAIMED``     → ``IN_PROGRESS``
* ``COMPLETE`` : ``IN_PROGRESS`` → ``DONE``
* ``FAIL``     : ``CLAIMED`` | ``IN_PROGRESS`` → ``FAILED``
* ``SKIP``     : ``PENDING`` | ``CLAIMED`` | ``IN_PROGRESS`` → ``SKIPPED``
* ``RELEASE``  : ``CLAIMED`` | ``IN_PROGRESS`` → ``PENDING``

``DONE``, ``FAILED`` and ``SKIPPED`` are terminal — every transition out of
them is rejected with a :class:`StateError`. ``FAIL`` from ``CLAIMED`` covers
workers that crash before they manage to start (``CLAIMED`` → ``FAILED``
without an intermediate ``START``); ``RELEASE`` from both ``CLAIMED`` and
``IN_PROGRESS`` is the visibility-timeout path used by
``release_stale_tasks`` in TASK-009d.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from whilly.core.models import Task, TaskId, TaskStatus


class Transition(str, Enum):
    """Named state-machine edges (PRD FR-2.2).

    The ``str`` mixin lets us write the transition name straight into the
    ``events.event_type`` column without bespoke enum converters in the
    adapter layer — same trick as :class:`~whilly.core.models.TaskStatus`.
    """

    CLAIM = "CLAIM"
    START = "START"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    SKIP = "SKIP"
    RELEASE = "RELEASE"


@dataclass(frozen=True)
class StateError:
    """Returned by :func:`apply_transition` when the requested edge is invalid.

    Carries enough context (``task_id``, ``from_status``, ``transition``,
    ``reason``) for the adapter layer to log the rejection and surface a
    meaningful error to the caller without re-deriving the rule that fired.
    """

    task_id: TaskId
    from_status: TaskStatus
    transition: Transition
    reason: str


# Adjacency map: (transition, current status) → next status. Anything not in
# this map is an invalid transition. Using a plain dict (not nested per
# transition) keeps the lookup a single hash and the table easy to eyeball.
_VALID_TRANSITIONS: dict[tuple[Transition, TaskStatus], TaskStatus] = {
    (Transition.CLAIM, TaskStatus.PENDING): TaskStatus.CLAIMED,
    (Transition.START, TaskStatus.CLAIMED): TaskStatus.IN_PROGRESS,
    (Transition.COMPLETE, TaskStatus.IN_PROGRESS): TaskStatus.DONE,
    (Transition.FAIL, TaskStatus.CLAIMED): TaskStatus.FAILED,
    (Transition.FAIL, TaskStatus.IN_PROGRESS): TaskStatus.FAILED,
    (Transition.SKIP, TaskStatus.PENDING): TaskStatus.SKIPPED,
    (Transition.SKIP, TaskStatus.CLAIMED): TaskStatus.SKIPPED,
    (Transition.SKIP, TaskStatus.IN_PROGRESS): TaskStatus.SKIPPED,
    (Transition.RELEASE, TaskStatus.CLAIMED): TaskStatus.PENDING,
    (Transition.RELEASE, TaskStatus.IN_PROGRESS): TaskStatus.PENDING,
}


def apply_transition(task: Task, transition: Transition) -> Task | StateError:
    """Apply ``transition`` to ``task`` and return the resulting Task.

    If ``transition`` is not a valid edge from ``task.status``, a
    :class:`StateError` is returned instead of raising. The function is pure:
    it does not mutate ``task`` (which is frozen anyway), reads no globals,
    performs no I/O, and is deterministic in its arguments.

    On success a *new* Task is returned with:

    * ``status``  set to the destination of the edge,
    * ``version`` incremented by 1 (optimistic-locking counter, PRD FR-2.4),
    * all other fields preserved verbatim.
    """

    next_status = _VALID_TRANSITIONS.get((transition, task.status))
    if next_status is None:
        return StateError(
            task_id=task.id,
            from_status=task.status,
            transition=transition,
            reason=f"invalid transition {transition.value} from {task.status.value}",
        )

    return replace(task, status=next_status, version=task.version + 1)
