"""Unit tests for :mod:`whilly.core.scheduler` (TASK-014, PRD FR-3.1/3.3/3.4, SC-4).

The scheduler module ships three pure functions that together drive the
distributed dispatch loop:

* :func:`whilly.core.scheduler.topological_sort` — deterministic Kahn's
  ordering, raises :class:`~whilly.core.scheduler.CycleError` on a cycle.
* :func:`whilly.core.scheduler.detect_cycles` — Tarjan's SCC, returns every
  cycle (including self-loops) as a sorted ``list[list[TaskId]]``.
* :func:`whilly.core.scheduler.next_ready` — dispatch picker that filters by
  status + dependencies, ranks by priority, and applies the file-conflict
  guard from PRD FR-3.4.

These tests exhaustively cover the AC of TASK-014 (≥15 tests, cycle topology,
priority ordering, key_files conflict resolution) plus the determinism /
purity invariants the rest of the system relies on. They live in the
``whilly.core`` purity envelope so no I/O, networking, or DB fixtures are
required — pytest's default rootdir discovery picks them up alongside
``test_state_machine.py``.
"""

from __future__ import annotations

import pytest

from whilly.core.models import Plan, Priority, Task, TaskStatus
from whilly.core.scheduler import CycleError, detect_cycles, next_ready, topological_sort

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    dependencies: tuple[str, ...] = (),
    key_files: tuple[str, ...] = (),
    priority: Priority = Priority.MEDIUM,
) -> Task:
    """Build a :class:`Task` with sane defaults for scheduler tests.

    Most scheduler invariants are independent of description / acceptance
    criteria / version, so we ignore those fields here. Tests that care
    about a specific field set it explicitly via kwargs.
    """
    return Task(
        id=task_id,
        status=status,
        dependencies=dependencies,
        key_files=key_files,
        priority=priority,
    )


def _plan(*tasks: Task, plan_id: str = "plan-test", name: str = "scheduler-test") -> Plan:
    return Plan(id=plan_id, name=name, tasks=tasks)


# ---------------------------------------------------------------------------
# topological_sort
# ---------------------------------------------------------------------------


def test_topological_sort_empty_plan_returns_empty_list() -> None:
    """An empty plan has no tasks and therefore no ordering. Boundary case."""
    plan = _plan()
    assert topological_sort(plan) == []


def test_topological_sort_single_task_returns_singleton() -> None:
    plan = _plan(_task("A"))
    assert topological_sort(plan) == ["A"]


def test_topological_sort_independent_tasks_sorted_lexicographically() -> None:
    """No edges → frontier = all tasks → min-heap pops alphabetically."""
    plan = _plan(_task("C"), _task("A"), _task("B"))
    assert topological_sort(plan) == ["A", "B", "C"]


def test_topological_sort_chain_orders_predecessors_first() -> None:
    """A → B → C dependency chain must be emitted A, B, C."""
    plan = _plan(
        _task("C", dependencies=("B",)),
        _task("B", dependencies=("A",)),
        _task("A"),
    )
    assert topological_sort(plan) == ["A", "B", "C"]


def test_topological_sort_diamond_emits_join_after_branches() -> None:
    """Diamond A → {B, C} → D: B and C may come in any order, but both
    precede D and follow A. Determinism gives us the exact answer."""
    plan = _plan(
        _task("A"),
        _task("B", dependencies=("A",)),
        _task("C", dependencies=("A",)),
        _task("D", dependencies=("B", "C")),
    )
    assert topological_sort(plan) == ["A", "B", "C", "D"]


def test_topological_sort_is_deterministic_across_calls() -> None:
    """Two invocations on the same plan must produce ``==``-equal output.
    Guards against a future refactor that drops the min-heap tie-break."""
    plan = _plan(
        _task("C"),
        _task("A"),
        _task("B", dependencies=("A",)),
        _task("D", dependencies=("B", "C")),
    )
    assert topological_sort(plan) == topological_sort(plan)


def test_topological_sort_ignores_cross_plan_dependencies() -> None:
    """Dep ids that do not appear in plan.tasks are silently skipped — they
    can never be satisfied internally so they would otherwise deadlock."""
    plan = _plan(
        _task("A", dependencies=("X-EXTERNAL",)),
        _task("B", dependencies=("A", "Y-EXTERNAL")),
    )
    assert topological_sort(plan) == ["A", "B"]


def test_topological_sort_raises_cycle_error_on_two_node_cycle() -> None:
    """A → B → A: both ids stuck in nonzero in-degree, must surface."""
    plan = _plan(
        _task("A", dependencies=("B",)),
        _task("B", dependencies=("A",)),
    )
    with pytest.raises(CycleError) as exc_info:
        topological_sort(plan)
    assert exc_info.value.remaining == ("A", "B")


def test_topological_sort_raises_cycle_error_on_self_loop() -> None:
    """A → A is a degenerate cycle: in-degree 1 with no other path to zero."""
    plan = _plan(_task("A", dependencies=("A",)))
    with pytest.raises(CycleError) as exc_info:
        topological_sort(plan)
    assert exc_info.value.remaining == ("A",)


def test_topological_sort_partially_acyclic_plan_still_raises() -> None:
    """Cycle B↔C blocks the topological emission of B, C while A may emit
    fine. The function refuses to return a partial order."""
    plan = _plan(
        _task("A"),
        _task("B", dependencies=("C",)),
        _task("C", dependencies=("B",)),
    )
    with pytest.raises(CycleError) as exc_info:
        topological_sort(plan)
    assert exc_info.value.remaining == ("B", "C")


# ---------------------------------------------------------------------------
# detect_cycles
# ---------------------------------------------------------------------------


def test_detect_cycles_returns_empty_list_for_acyclic_plan() -> None:
    """Trivial SCCs of size 1 with no self-edge are not cycles."""
    plan = _plan(
        _task("A"),
        _task("B", dependencies=("A",)),
        _task("C", dependencies=("A",)),
    )
    assert detect_cycles(plan) == []


def test_detect_cycles_finds_three_node_cycle() -> None:
    """A → B → C → A is the canonical AC scenario from TASK-014: must
    produce ``[["A", "B", "C"]]`` with the inner list sorted."""
    plan = _plan(
        _task("A", dependencies=("C",)),
        _task("B", dependencies=("A",)),
        _task("C", dependencies=("B",)),
    )
    assert detect_cycles(plan) == [["A", "B", "C"]]


def test_detect_cycles_finds_self_loop() -> None:
    """Self-edge A → A is a single-element SCC that *is* a cycle."""
    plan = _plan(_task("A", dependencies=("A",)))
    assert detect_cycles(plan) == [["A"]]


def test_detect_cycles_finds_multiple_disjoint_cycles_sorted() -> None:
    """Two disjoint cycles → outer list sorted by inner-list contents.
    Acyclic node X is excluded from the output."""
    plan = _plan(
        _task("A", dependencies=("B",)),
        _task("B", dependencies=("A",)),
        _task("C", dependencies=("D",)),
        _task("D", dependencies=("C",)),
        _task("X"),
    )
    assert detect_cycles(plan) == [["A", "B"], ["C", "D"]]


def test_detect_cycles_does_not_report_acyclic_neighbours() -> None:
    """A cycle that has an acyclic predecessor (X → A → B → A) reports only
    {A, B}; X is in its own trivial SCC and not part of the cycle."""
    plan = _plan(
        _task("X"),
        _task("A", dependencies=("B", "X")),
        _task("B", dependencies=("A",)),
    )
    assert detect_cycles(plan) == [["A", "B"]]


def test_detect_cycles_is_deterministic_across_calls() -> None:
    plan = _plan(
        _task("A", dependencies=("B",)),
        _task("B", dependencies=("A",)),
        _task("C", dependencies=("D",)),
        _task("D", dependencies=("C",)),
    )
    assert detect_cycles(plan) == detect_cycles(plan)


def test_detect_cycles_ignores_cross_plan_edges() -> None:
    """Dangling external dep does not create a phantom cycle."""
    plan = _plan(_task("A", dependencies=("EXTERNAL",)))
    assert detect_cycles(plan) == []


# ---------------------------------------------------------------------------
# next_ready
# ---------------------------------------------------------------------------


def test_next_ready_returns_root_tasks_when_nothing_in_progress() -> None:
    """Two roots, no in-flight: both returned in (priority, id) order."""
    plan = _plan(_task("B"), _task("A"))
    assert next_ready(plan, set()) == ["A", "B"]


def test_next_ready_excludes_tasks_with_unresolved_dependencies() -> None:
    """B depends on A; A is still PENDING, so B is not ready yet."""
    plan = _plan(_task("A"), _task("B", dependencies=("A",)))
    assert next_ready(plan, set()) == ["A"]


def test_next_ready_releases_task_once_dependency_is_done() -> None:
    """A is DONE, B's only dep is A → B becomes ready (and A is no longer
    eligible because it is not PENDING)."""
    plan = _plan(
        _task("A", status=TaskStatus.DONE),
        _task("B", dependencies=("A",)),
    )
    assert next_ready(plan, set()) == ["B"]


def test_next_ready_skips_non_pending_tasks() -> None:
    """Only PENDING tasks are dispatchable. CLAIMED/IN_PROGRESS/DONE/
    FAILED/SKIPPED are all filtered out."""
    plan = _plan(
        _task("A", status=TaskStatus.PENDING),
        _task("B", status=TaskStatus.CLAIMED),
        _task("C", status=TaskStatus.IN_PROGRESS),
        _task("D", status=TaskStatus.DONE),
        _task("E", status=TaskStatus.FAILED),
        _task("F", status=TaskStatus.SKIPPED),
    )
    assert next_ready(plan, set()) == ["A"]


def test_next_ready_excludes_tasks_listed_in_in_progress() -> None:
    """Caller-supplied in-flight set takes precedence even when the task
    row itself looks PENDING (e.g. just-claimed-but-not-yet-flushed)."""
    plan = _plan(_task("A"), _task("B"))
    assert next_ready(plan, {"A"}) == ["B"]


def test_next_ready_priority_critical_before_high_before_medium_before_low() -> None:
    """PRD FR-3.4 ladder: critical < high < medium < low. With no file
    conflicts every task is admitted, in priority order."""
    plan = _plan(
        _task("low-task", priority=Priority.LOW),
        _task("med-task", priority=Priority.MEDIUM),
        _task("hi-task", priority=Priority.HIGH),
        _task("crit-task", priority=Priority.CRITICAL),
    )
    assert next_ready(plan, set()) == ["crit-task", "hi-task", "med-task", "low-task"]


def test_next_ready_breaks_priority_ties_by_task_id() -> None:
    """Two MEDIUM tasks → lexicographically smaller id wins. Matches the
    Kahn / Tarjan tie-break so the whole module speaks one ordering."""
    plan = _plan(
        _task("Z", priority=Priority.MEDIUM),
        _task("A", priority=Priority.MEDIUM),
        _task("M", priority=Priority.MEDIUM),
    )
    assert next_ready(plan, set()) == ["A", "M", "Z"]


def test_next_ready_blocks_lower_priority_task_with_conflicting_files() -> None:
    """Critical and medium tasks both touch ``shared.py``: the critical
    one reserves the file, the medium one is dropped from this round.
    AC-explicit scenario from TASK-014."""
    plan = _plan(
        _task("crit", priority=Priority.CRITICAL, key_files=("shared.py",)),
        _task("med", priority=Priority.MEDIUM, key_files=("shared.py",)),
    )
    assert next_ready(plan, set()) == ["crit"]


def test_next_ready_admits_disjoint_files_in_parallel() -> None:
    """Two ready tasks touching different files are both dispatched."""
    plan = _plan(
        _task("A", key_files=("a.py",)),
        _task("B", key_files=("b.py",)),
    )
    assert next_ready(plan, set()) == ["A", "B"]


def test_next_ready_blocks_task_conflicting_with_in_progress_task() -> None:
    """An in-flight task locks its files for the whole round, even though
    it does not appear in the candidate list itself."""
    plan = _plan(
        _task("running", status=TaskStatus.IN_PROGRESS, key_files=("x.py",)),
        _task("waiting", key_files=("x.py",)),
    )
    assert next_ready(plan, {"running"}) == []


def test_next_ready_ignores_stale_in_progress_ids() -> None:
    """An in_progress id absent from the plan reserves nothing — matches
    the worker protocol where a heartbeat may name a removed task."""
    plan = _plan(_task("A", key_files=("x.py",)))
    assert next_ready(plan, {"GHOST-123"}) == ["A"]


def test_next_ready_is_deterministic_across_calls() -> None:
    """Same plan + same in_progress → same list. Catches accidental dict
    iteration / set hash leaks into the output."""
    plan = _plan(
        _task("Z", priority=Priority.LOW, key_files=("z.py",)),
        _task("A", priority=Priority.HIGH, key_files=("a.py",)),
        _task("M", priority=Priority.CRITICAL, key_files=("m.py",)),
    )
    assert next_ready(plan, set()) == next_ready(plan, set())


def test_next_ready_does_not_mutate_inputs() -> None:
    """Pure function: the input plan and in_progress set are untouched."""
    plan = _plan(_task("A"), _task("B"))
    in_progress: set[str] = {"A"}

    next_ready(plan, in_progress)

    assert in_progress == {"A"}
    assert plan.tasks == (_task("A"), _task("B"))


def test_next_ready_chained_priority_and_file_conflict() -> None:
    """Realistic scenario combining priority ordering and the file guard.

    - ``crit`` (CRITICAL) edits ``a.py``.
    - ``hi`` (HIGH) edits ``b.py`` — disjoint, so admitted.
    - ``med-conflict`` (MEDIUM) edits ``a.py`` — blocked by ``crit``.
    - ``low`` (LOW) edits ``c.py`` — disjoint, so admitted last.
    """
    plan = _plan(
        _task("crit", priority=Priority.CRITICAL, key_files=("a.py",)),
        _task("hi", priority=Priority.HIGH, key_files=("b.py",)),
        _task("med-conflict", priority=Priority.MEDIUM, key_files=("a.py",)),
        _task("low", priority=Priority.LOW, key_files=("c.py",)),
    )
    assert next_ready(plan, set()) == ["crit", "hi", "low"]
