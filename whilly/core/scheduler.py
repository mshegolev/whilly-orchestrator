"""DAG scheduling primitives for Whilly v4.0 (PRD FR-3.1, NFR-4).

Pure functions that order the tasks of a :class:`~whilly.core.models.Plan`.
This module is part of the ``whilly.core`` layer (Hexagonal architecture, PRD
TC-8 / SC-6): no I/O, no networking, no subprocess, no asyncio. It imports
only :mod:`whilly.core.models` and the standard library, and the
``.importlinter`` ``core-purity`` contract (PRD SC-6) keeps it that way.

TASK-013a ships :func:`topological_sort` (Kahn's algorithm with deterministic
tie-breaking). The remaining scheduler surface — :func:`detect_cycles`
(TASK-013b) and :func:`next_ready` (TASK-013c) — is added in follow-up tasks.

Determinism
-----------
Kahn's algorithm is non-deterministic in general: when multiple tasks have
zero unresolved dependencies at the same moment, any of them is a legal
next step. Whilly needs *one* canonical answer so dashboards (TASK-027)
and tests (TASK-014) can compare orderings literally. We enforce this by
popping the lexicographically smallest available task id at each step via
a min-heap keyed on ``task.id`` — output is byte-identical across Python
interpreters, dict-iteration orders, and host platforms.
"""

from __future__ import annotations

import heapq

from whilly.core.models import Plan, TaskId

__all__ = ["CycleError", "topological_sort"]


class CycleError(ValueError):
    """Raised by :func:`topological_sort` when ``plan`` contains a cycle.

    Kahn's algorithm cannot order tasks that participate in a strongly
    connected component of size > 1 (or a self-loop). Rather than silently
    drop those tasks, we surface the failure so callers know the plan is
    invalid. Use :func:`detect_cycles` (TASK-013b) to enumerate the
    offending cycles before invoking :func:`topological_sort`.

    ``remaining`` is a sorted tuple of the task ids that could not be
    ordered — exactly the membership of the cycle(s).
    """

    def __init__(self, remaining: tuple[TaskId, ...]) -> None:
        self.remaining: tuple[TaskId, ...] = remaining
        joined = ", ".join(remaining)
        super().__init__(f"plan contains a cycle; {len(remaining)} task(s) not orderable: {joined}")


def topological_sort(plan: Plan) -> list[TaskId]:
    """Return a deterministic topological ordering of ``plan.tasks``.

    Implementation: Kahn's algorithm.

    1. Compute the *unresolved-in-degree* of each task — how many of its
       declared dependencies still point inside this plan.
    2. Seed a min-heap with every task whose in-degree is zero.
    3. Pop the lexicographically smallest task id, append to the output,
       and decrement the in-degree of every dependent. Tasks that just
       hit zero join the heap.
    4. If the output length matches ``len(plan.tasks)``, we are done.
       Otherwise some tasks were stuck in a cycle and we raise
       :class:`CycleError` listing their ids.

    Pure: no I/O, no globals, deterministic in its single argument. Two
    invocations on the same plan return ``==``-equal lists.

    Cross-plan dependency references — i.e. dependency ids that do not
    appear in ``plan.tasks`` — are silently ignored. They cannot be
    satisfied by anything inside the plan, so treating them as no-ops
    keeps the ordering well-defined. Domain-level referential integrity
    of dependency ids belongs to plan-import validation (TASK-010), not
    the scheduler.

    Duplicate task ids in ``plan.tasks`` are also tolerated: the in-degree
    table collapses them to a single entry, so the function never raises
    on duplicates by itself. Detecting duplicates is plan-import's job.
    """
    in_plan: set[TaskId] = {task.id for task in plan.tasks}

    # in_degree[id] = number of this task's dependencies that still point
    # inside the plan and have not yet been emitted.
    in_degree: dict[TaskId, int] = {
        task.id: sum(1 for dep in task.dependencies if dep in in_plan) for task in plan.tasks
    }

    # Reverse adjacency: dep_id -> list of task ids that declare it as a
    # dependency. Built with a single pass over plan.tasks so the cost is
    # O(V + E).
    dependents: dict[TaskId, list[TaskId]] = {task.id: [] for task in plan.tasks}
    for task in plan.tasks:
        for dep in task.dependencies:
            if dep in in_plan:
                dependents[dep].append(task.id)

    # Min-heap on task id makes the wavefront tie-break deterministic.
    frontier: list[TaskId] = [tid for tid, deg in in_degree.items() if deg == 0]
    heapq.heapify(frontier)

    order: list[TaskId] = []
    while frontier:
        tid = heapq.heappop(frontier)
        order.append(tid)
        for child in dependents[tid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                heapq.heappush(frontier, child)

    if len(order) != len(in_degree):
        remaining = tuple(sorted(tid for tid, deg in in_degree.items() if deg > 0))
        raise CycleError(remaining)

    return order
