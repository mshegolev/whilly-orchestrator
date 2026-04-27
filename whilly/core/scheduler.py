"""DAG scheduling primitives for Whilly v4.0 (PRD FR-3.1, NFR-4).

Pure functions that order the tasks of a :class:`~whilly.core.models.Plan`.
This module is part of the ``whilly.core`` layer (Hexagonal architecture, PRD
TC-8 / SC-6): no I/O, no networking, no subprocess, no asyncio. It imports
only :mod:`whilly.core.models` and the standard library, and the
``.importlinter`` ``core-purity`` contract (PRD SC-6) keeps it that way.

TASK-013a ships :func:`topological_sort` (Kahn's algorithm with deterministic
tie-breaking). TASK-013b adds :func:`detect_cycles` (Tarjan's SCC). TASK-013c
rounds out the surface with :func:`next_ready`, the dispatch-time picker the
local and remote workers (TASK-019, TASK-022) call once per loop iteration to
ask "what may I claim, considering priority and file-conflict guards?".

Determinism
-----------
Both algorithms are non-deterministic in textbook form: Kahn's frontier and
Tarjan's DFS-root selection both hinge on iteration order of the underlying
graph. Whilly needs *one* canonical answer so dashboards (TASK-027) and
tests (TASK-014) can compare orderings literally. We enforce this by
ordering every choice the algorithms make on ``task.id`` (lexicographic):
Kahn's frontier is a min-heap, Tarjan's DFS visits roots and successors in
sorted order, and emitted SCCs are sorted internally and then by their
canonical first id. Output is byte-identical across Python interpreters,
dict-iteration orders, and host platforms.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator

from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus

__all__ = ["CycleError", "detect_cycles", "next_ready", "topological_sort"]


# Lower number == higher priority. Mirrors PRD FR-3.4 ranking
# (critical > high > medium > low). Defined at module scope so the comparator
# in :func:`next_ready` does not rebuild the table on every call.
_PRIORITY_RANK: dict[Priority, int] = {
    Priority.CRITICAL: 0,
    Priority.HIGH: 1,
    Priority.MEDIUM: 2,
    Priority.LOW: 3,
}


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


def detect_cycles(plan: Plan) -> list[list[TaskId]]:
    """Return every dependency cycle in ``plan`` as a list of sorted task ids.

    Implementation: Tarjan's strongly-connected-components (SCC) algorithm,
    iterative (no recursion-limit fragility on pathological plans).

    A *cycle* is reported when:

    * an SCC contains at least two task ids (mutual dependence); or
    * an SCC contains a single task id that lists *itself* as a dependency
      (a self-loop ``A → A``).

    Lone, dependency-free tasks form trivial SCCs of size 1 with no
    self-edge — they are not cycles and never appear in the output.

    Each inner list is sorted lexicographically by ``task.id``; the outer
    list is sorted by inner-list contents so two calls on the same plan
    return ``==``-equal results.

    Pure: no I/O, no globals, deterministic in its single argument. Safe
    to call before :func:`topological_sort` to surface cycles with full
    membership detail (the latter raises :class:`CycleError` with only the
    set of stuck task ids, no SCC partitioning).

    Cross-plan dependency references — dependency ids that do not appear
    in ``plan.tasks`` — are silently ignored (matches
    :func:`topological_sort`). Duplicate task entries in ``plan.tasks``
    have their dependency sets merged; only in-plan edges contribute.
    """
    # Build deduplicated, in-plan adjacency. ``adj[tid]`` lists every
    # task this node depends on, sorted for deterministic DFS traversal.
    in_plan: set[TaskId] = {task.id for task in plan.tasks}
    adj_set: dict[TaskId, set[TaskId]] = {tid: set() for tid in in_plan}
    for task in plan.tasks:
        for dep in task.dependencies:
            if dep in in_plan:
                adj_set[task.id].add(dep)
    adj: dict[TaskId, list[TaskId]] = {tid: sorted(deps) for tid, deps in adj_set.items()}

    # Tarjan's bookkeeping. ``indices`` doubles as the visited set, since
    # Tarjan assigns an index on first discovery.
    indices: dict[TaskId, int] = {}
    lowlinks: dict[TaskId, int] = {}
    on_stack: dict[TaskId, bool] = {}
    component_stack: list[TaskId] = []
    next_index: int = 0
    cycles: list[list[TaskId]] = []

    # Sorted root iteration removes the last source of nondeterminism: a
    # Python ``set`` literal has hash-randomised order, so iterating
    # ``in_plan`` directly would yield SCCs in different sequences across
    # runs even though their *contents* would match.
    for root in sorted(in_plan):
        if root in indices:
            continue

        # Iterative DFS. Each frame stores the node and an iterator over
        # its remaining outgoing edges. ``StopIteration`` marks the end of
        # the recursive call and is where lowlink propagation happens.
        work_stack: list[tuple[TaskId, Iterator[TaskId]]] = []
        indices[root] = next_index
        lowlinks[root] = next_index
        next_index += 1
        component_stack.append(root)
        on_stack[root] = True
        work_stack.append((root, iter(adj[root])))

        while work_stack:
            v, neighbours = work_stack[-1]
            try:
                w = next(neighbours)
            except StopIteration:
                # All children of ``v`` processed. If ``v`` is the root of
                # an SCC, pop the component off the stack.
                work_stack.pop()
                if lowlinks[v] == indices[v]:
                    component: list[TaskId] = []
                    while True:
                        node = component_stack.pop()
                        on_stack[node] = False
                        component.append(node)
                        if node == v:
                            break
                    if len(component) > 1 or v in adj[v]:
                        cycles.append(sorted(component))
                # Propagate lowlink up to the caller frame (the recursive
                # ``v.lowlink = min(v.lowlink, w.lowlink)`` step).
                if work_stack:
                    parent_v, _ = work_stack[-1]
                    lowlinks[parent_v] = min(lowlinks[parent_v], lowlinks[v])
                continue

            if w not in indices:
                # Tree edge — recurse.
                indices[w] = next_index
                lowlinks[w] = next_index
                next_index += 1
                component_stack.append(w)
                on_stack[w] = True
                work_stack.append((w, iter(adj[w])))
            elif on_stack.get(w, False):
                # Back edge to a node still on the SCC stack — update
                # the current frame's lowlink with the destination's
                # discovery index, not its lowlink, per Tarjan's spec.
                lowlinks[v] = min(lowlinks[v], indices[w])
            # else: cross edge to a node in an already-emitted SCC. Ignore.

    cycles.sort()
    return cycles


def next_ready(plan: Plan, in_progress: set[TaskId]) -> list[TaskId]:
    """Return the task ids that are safe to dispatch right now.

    A task is *ready* when all of the following hold:

    * its ``status`` is :attr:`~whilly.core.models.TaskStatus.PENDING`. Tasks
      that are already ``CLAIMED``/``IN_PROGRESS``/``DONE``/``FAILED``/
      ``SKIPPED`` are excluded — the dispatcher only ever issues fresh work.
    * every one of its in-plan ``dependencies`` references a task whose
      ``status`` is :attr:`~whilly.core.models.TaskStatus.DONE`. A single
      pending or failed dependency keeps the task off the ready list.
    * its id is *not* in ``in_progress``. ``in_progress`` is the caller's
      view of which task ids are currently being worked on — typically the
      union of ``CLAIMED`` and ``IN_PROGRESS`` rows in the database, but the
      function is agnostic about how that set is computed (the worker
      protocol may expand the definition without touching the scheduler).
    * none of its ``key_files`` overlap with the ``key_files`` of any task
      already reserved this round — i.e. tasks named in ``in_progress`` *or*
      a higher-priority ready task already chosen earlier in this same call.
      This is the file-conflict guard from PRD FR-3.4: two agents may not
      edit the same file in parallel.

    Cross-plan dependency references and entries of ``in_progress`` whose ids
    do not appear in ``plan.tasks`` are silently ignored — mirrors
    :func:`topological_sort` and :func:`detect_cycles`. Stale ids in
    ``in_progress`` therefore reserve *nothing*; this matches the worker
    protocol where a heartbeat may name a task that has since been removed
    from the plan.

    Ordering and tie-breaking
    -------------------------
    Candidates are sorted by ``(priority_rank, task.id)`` where
    ``priority_rank`` follows the PRD FR-3.4 ladder
    (``critical`` < ``high`` < ``medium`` < ``low``). When two candidates
    share a priority bucket, the lexicographically smaller ``task.id`` wins —
    same tie-break Kahn's frontier and Tarjan's DFS use elsewhere in this
    module so the scheduler speaks one consistent language. The greedy
    file-conflict filter walks this sorted list once: a higher-priority task
    always reserves its ``key_files`` first, so a lower-priority task whose
    files overlap is dropped from this round even though it would have been
    ready in isolation. It will surface again on the next call once the
    higher-priority task moves out of ``in_progress``.

    Determinism: two invocations with ``==``-equal ``plan`` and
    ``in_progress`` produce ``==``-equal output lists, irrespective of dict
    iteration order or set hash randomisation.

    Pure: no I/O, no globals mutated, no side effects on inputs. ``plan`` is
    a frozen dataclass and ``in_progress`` is read but never modified.

    Duplicate task entries in ``plan.tasks`` collapse to a single dict entry
    (last-write-wins, matching :func:`topological_sort`), so the result list
    never contains the same id twice. Detecting on-disk duplicates is plan-
    import's responsibility, not the scheduler's.
    """
    by_id: dict[TaskId, Task] = {task.id: task for task in plan.tasks}
    in_plan: set[TaskId] = set(by_id)

    # Files already locked by tasks the caller declares in-flight. Stale ids
    # that do not exist in the plan are skipped — they cannot reserve files
    # we do not know about.
    reserved_files: set[str] = set()
    for tid in in_progress:
        task = by_id.get(tid)
        if task is not None:
            reserved_files.update(task.key_files)

    # Iterate ``by_id.values()`` rather than ``plan.tasks`` so duplicate ids
    # (last-write-wins) cannot produce duplicate candidates. dict preserves
    # insertion order in Python 3.7+, so this is still deterministic.
    candidates: list[Task] = []
    for task in by_id.values():
        if task.id in in_progress:
            continue
        if task.status != TaskStatus.PENDING:
            continue
        deps_done = all(by_id[dep].status == TaskStatus.DONE for dep in task.dependencies if dep in in_plan)
        if not deps_done:
            continue
        candidates.append(task)

    candidates.sort(key=lambda t: (_PRIORITY_RANK[t.priority], t.id))

    # Greedy admission: walk highest-priority-first, admit a task only when
    # its key_files do not collide with anything reserved so far. The chosen
    # task then locks its own files for the rest of this round.
    ready: list[TaskId] = []
    for task in candidates:
        files = set(task.key_files)
        if files & reserved_files:
            continue
        ready.append(task.id)
        reserved_files |= files

    return ready
