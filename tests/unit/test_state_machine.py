"""Unit tests for :mod:`whilly.core.state_machine` (TASK-006, PRD FR-2.2).

Exhaustively sweeps the 6 transitions × 6 statuses Cartesian product (36 cases)
to give mathematical certainty that the lifecycle DAG matches the contract
documented in ``whilly/core/state_machine.py``:

* ``CLAIM``    : ``PENDING``     → ``CLAIMED``
* ``START``    : ``CLAIMED``     → ``IN_PROGRESS``
* ``COMPLETE`` : ``IN_PROGRESS`` → ``DONE``
* ``FAIL``     : ``CLAIMED`` | ``IN_PROGRESS`` → ``FAILED``
* ``SKIP``     : ``PENDING`` | ``CLAIMED`` | ``IN_PROGRESS`` → ``SKIPPED``
* ``RELEASE``  : ``CLAIMED`` | ``IN_PROGRESS`` → ``PENDING``

Plus the explicit AC scenarios from TASK-006:

- the happy path ``PENDING → CLAIMED → IN_PROGRESS → DONE`` increments
  ``version`` exactly three times;
- ``CLAIM`` on an already-CLAIMED task returns :class:`StateError`;
- terminal-state transitions (e.g. ``DONE → FAILED``) are rejected;
- ``RELEASE`` from ``CLAIMED`` returns the task to ``PENDING`` (visibility
  timeout path used by ``release_stale_tasks`` in TASK-009d);
- on a ``StateError`` return the input :class:`Task` is left untouched
  (frozen dataclass — so the version counter cannot drift).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from whilly.core.models import Priority, Task, TaskStatus
from whilly.core.state_machine import (
    StateError,
    Transition,
    apply_transition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(status: TaskStatus, *, version: int = 0, task_id: str = "TASK-006") -> Task:
    """Build a Task with non-default optional fields so we can assert the
    transition preserves everything except status/version.
    """
    return Task(
        id=task_id,
        status=status,
        dependencies=("TASK-005",),
        key_files=("whilly/core/state_machine.py",),
        priority=Priority.CRITICAL,
        description="Lifecycle test fixture",
        acceptance_criteria=("AC: parametrized sweep",),
        test_steps=("pytest tests/unit/test_state_machine.py",),
        prd_requirement="FR-2.2",
        version=version,
    )


# ---------------------------------------------------------------------------
# Cartesian product of (Transition, TaskStatus) → expected next status (or None)
# ---------------------------------------------------------------------------

# A flat truth table mirroring _VALID_TRANSITIONS in state_machine.py.
# Every transition × status combination is listed exactly once; if either side
# diverges the corresponding test fails. ``None`` means the edge is invalid
# and ``apply_transition`` must return :class:`StateError`.
_TRUTH_TABLE: dict[tuple[Transition, TaskStatus], TaskStatus | None] = {
    # CLAIM
    (Transition.CLAIM, TaskStatus.PENDING): TaskStatus.CLAIMED,
    (Transition.CLAIM, TaskStatus.CLAIMED): None,
    (Transition.CLAIM, TaskStatus.IN_PROGRESS): None,
    (Transition.CLAIM, TaskStatus.DONE): None,
    (Transition.CLAIM, TaskStatus.FAILED): None,
    (Transition.CLAIM, TaskStatus.SKIPPED): None,
    # START
    (Transition.START, TaskStatus.PENDING): None,
    (Transition.START, TaskStatus.CLAIMED): TaskStatus.IN_PROGRESS,
    (Transition.START, TaskStatus.IN_PROGRESS): None,
    (Transition.START, TaskStatus.DONE): None,
    (Transition.START, TaskStatus.FAILED): None,
    (Transition.START, TaskStatus.SKIPPED): None,
    # COMPLETE
    (Transition.COMPLETE, TaskStatus.PENDING): None,
    (Transition.COMPLETE, TaskStatus.CLAIMED): None,
    (Transition.COMPLETE, TaskStatus.IN_PROGRESS): TaskStatus.DONE,
    (Transition.COMPLETE, TaskStatus.DONE): None,
    (Transition.COMPLETE, TaskStatus.FAILED): None,
    (Transition.COMPLETE, TaskStatus.SKIPPED): None,
    # FAIL
    (Transition.FAIL, TaskStatus.PENDING): None,
    (Transition.FAIL, TaskStatus.CLAIMED): TaskStatus.FAILED,
    (Transition.FAIL, TaskStatus.IN_PROGRESS): TaskStatus.FAILED,
    (Transition.FAIL, TaskStatus.DONE): None,
    (Transition.FAIL, TaskStatus.FAILED): None,
    (Transition.FAIL, TaskStatus.SKIPPED): None,
    # SKIP
    (Transition.SKIP, TaskStatus.PENDING): TaskStatus.SKIPPED,
    (Transition.SKIP, TaskStatus.CLAIMED): TaskStatus.SKIPPED,
    (Transition.SKIP, TaskStatus.IN_PROGRESS): TaskStatus.SKIPPED,
    (Transition.SKIP, TaskStatus.DONE): None,
    (Transition.SKIP, TaskStatus.FAILED): None,
    (Transition.SKIP, TaskStatus.SKIPPED): None,
    # RELEASE
    (Transition.RELEASE, TaskStatus.PENDING): None,
    (Transition.RELEASE, TaskStatus.CLAIMED): TaskStatus.PENDING,
    (Transition.RELEASE, TaskStatus.IN_PROGRESS): TaskStatus.PENDING,
    (Transition.RELEASE, TaskStatus.DONE): None,
    (Transition.RELEASE, TaskStatus.FAILED): None,
    (Transition.RELEASE, TaskStatus.SKIPPED): None,
}

_VALID_CASES: list[tuple[Transition, TaskStatus, TaskStatus]] = [
    (transition, status, next_status)
    for (transition, status), next_status in _TRUTH_TABLE.items()
    if next_status is not None
]
_INVALID_CASES: list[tuple[Transition, TaskStatus]] = [
    (transition, status) for (transition, status), next_status in _TRUTH_TABLE.items() if next_status is None
]


# Sanity checks on the truth table itself — protects against future edits
# silently dropping a row or duplicating an edge.
def test_truth_table_covers_full_cartesian_product() -> None:
    assert len(_TRUTH_TABLE) == len(Transition) * len(TaskStatus) == 36


def test_truth_table_has_ten_valid_edges() -> None:
    assert len(_VALID_CASES) == 10
    assert len(_INVALID_CASES) == 26


# ---------------------------------------------------------------------------
# Valid transitions — parametrized sweep (10 cases)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transition", "from_status", "expected_status"),
    _VALID_CASES,
    ids=[f"{t.value}-{s.value}->{n.value}" for (t, s, n) in _VALID_CASES],
)
def test_valid_transition_returns_task_with_new_status(
    transition: Transition,
    from_status: TaskStatus,
    expected_status: TaskStatus,
) -> None:
    task = _task(from_status, version=7)

    result = apply_transition(task, transition)

    assert isinstance(result, Task)
    assert result.status is expected_status
    assert result.version == 8  # incremented exactly once


@pytest.mark.parametrize(
    ("transition", "from_status", "expected_status"),
    _VALID_CASES,
    ids=[f"{t.value}-{s.value}-preserves" for (t, s, _n) in _VALID_CASES],
)
def test_valid_transition_preserves_other_fields(
    transition: Transition,
    from_status: TaskStatus,
    expected_status: TaskStatus,
) -> None:
    task = _task(from_status)

    result = apply_transition(task, transition)
    assert isinstance(result, Task)

    # Everything except status + version is left verbatim.
    assert result.id == task.id
    assert result.dependencies == task.dependencies
    assert result.key_files == task.key_files
    assert result.priority == task.priority
    assert result.description == task.description
    assert result.acceptance_criteria == task.acceptance_criteria
    assert result.test_steps == task.test_steps
    assert result.prd_requirement == task.prd_requirement


# ---------------------------------------------------------------------------
# Invalid transitions — parametrized sweep (26 cases)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transition", "from_status"),
    _INVALID_CASES,
    ids=[f"{t.value}-from-{s.value}" for (t, s) in _INVALID_CASES],
)
def test_invalid_transition_returns_state_error(
    transition: Transition,
    from_status: TaskStatus,
) -> None:
    task = _task(from_status, version=3)

    result = apply_transition(task, transition)

    assert isinstance(result, StateError)
    assert result.task_id == task.id
    assert result.from_status is from_status
    assert result.transition is transition
    # Reason should mention both sides for grep-ability in adapter logs.
    assert transition.value in result.reason
    assert from_status.value in result.reason


@pytest.mark.parametrize(
    ("transition", "from_status"),
    _INVALID_CASES,
    ids=[f"{t.value}-from-{s.value}-no-mutate" for (t, s) in _INVALID_CASES],
)
def test_invalid_transition_does_not_mutate_or_increment_version(
    transition: Transition,
    from_status: TaskStatus,
) -> None:
    task = _task(from_status, version=3)

    result = apply_transition(task, transition)

    assert isinstance(result, StateError)
    # Frozen dataclass: version on the original is unchanged. The function
    # simply does not return a new Task on the error branch, so callers can
    # safely retry the SQL UPDATE without an off-by-one drift.
    assert task.version == 3
    assert task.status is from_status


# ---------------------------------------------------------------------------
# Explicit scenarios from the TASK-006 description
# ---------------------------------------------------------------------------


def test_happy_path_pending_to_done_increments_version_three_times() -> None:
    """PENDING → CLAIMED → IN_PROGRESS → DONE; version 0 → 1 → 2 → 3."""
    task = _task(TaskStatus.PENDING, version=0)

    claimed = apply_transition(task, Transition.CLAIM)
    assert isinstance(claimed, Task)
    assert claimed.status is TaskStatus.CLAIMED
    assert claimed.version == 1

    started = apply_transition(claimed, Transition.START)
    assert isinstance(started, Task)
    assert started.status is TaskStatus.IN_PROGRESS
    assert started.version == 2

    done = apply_transition(started, Transition.COMPLETE)
    assert isinstance(done, Task)
    assert done.status is TaskStatus.DONE
    assert done.version == 3


def test_double_claim_returns_state_error() -> None:
    """Re-CLAIM of an already-CLAIMED task must reject."""
    claimed = _task(TaskStatus.CLAIMED, version=1)
    result = apply_transition(claimed, Transition.CLAIM)

    assert isinstance(result, StateError)
    assert result.from_status is TaskStatus.CLAIMED
    assert result.transition is Transition.CLAIM
    assert "CLAIM" in result.reason
    assert "CLAIMED" in result.reason


def test_done_to_failed_is_rejected() -> None:
    """DONE is terminal; FAIL out of it is illegal."""
    done = _task(TaskStatus.DONE, version=3)
    result = apply_transition(done, Transition.FAIL)

    assert isinstance(result, StateError)
    assert result.from_status is TaskStatus.DONE


def test_release_from_claimed_returns_to_pending() -> None:
    """Visibility-timeout path used by release_stale_tasks (TASK-009d)."""
    claimed = _task(TaskStatus.CLAIMED, version=4)
    result = apply_transition(claimed, Transition.RELEASE)

    assert isinstance(result, Task)
    assert result.status is TaskStatus.PENDING
    assert result.version == 5


def test_release_from_in_progress_returns_to_pending() -> None:
    """Same path, but the worker had already called START before going dark."""
    in_progress = _task(TaskStatus.IN_PROGRESS, version=4)
    result = apply_transition(in_progress, Transition.RELEASE)

    assert isinstance(result, Task)
    assert result.status is TaskStatus.PENDING
    assert result.version == 5


def test_fail_from_claimed_skips_intermediate_start() -> None:
    """A worker can fail before it manages to call START (e.g. claude bin missing)."""
    claimed = _task(TaskStatus.CLAIMED, version=1)
    result = apply_transition(claimed, Transition.FAIL)

    assert isinstance(result, Task)
    assert result.status is TaskStatus.FAILED
    assert result.version == 2


# ---------------------------------------------------------------------------
# Purity & immutability properties
# ---------------------------------------------------------------------------


def test_apply_transition_does_not_mutate_input_task() -> None:
    """The returned Task is a fresh instance; the input is untouched."""
    task = _task(TaskStatus.PENDING, version=0)
    result = apply_transition(task, Transition.CLAIM)

    assert isinstance(result, Task)
    assert result is not task
    assert task.status is TaskStatus.PENDING
    assert task.version == 0


def test_apply_transition_is_deterministic() -> None:
    """Same inputs → identical outputs (no clock / random / env reads)."""
    task = _task(TaskStatus.IN_PROGRESS, version=5)

    a = apply_transition(task, Transition.COMPLETE)
    b = apply_transition(task, Transition.COMPLETE)

    assert isinstance(a, Task)
    assert isinstance(b, Task)
    assert a == b


def test_task_is_frozen() -> None:
    """Sanity check: frozen=True genuinely blocks reassignment."""
    task = _task(TaskStatus.PENDING)
    with pytest.raises(FrozenInstanceError):
        task.status = TaskStatus.CLAIMED  # type: ignore[misc]


def test_state_error_is_frozen() -> None:
    err = StateError(
        task_id="TASK-006",
        from_status=TaskStatus.DONE,
        transition=Transition.FAIL,
        reason="invalid transition FAIL from DONE",
    )
    with pytest.raises(FrozenInstanceError):
        err.reason = "tampered"  # type: ignore[misc]


def test_state_error_carries_full_context() -> None:
    """The error value must surface task_id / from_status / transition / reason."""
    task = _task(TaskStatus.SKIPPED, task_id="TASK-XYZ")
    result = apply_transition(task, Transition.START)

    assert isinstance(result, StateError)
    assert result.task_id == "TASK-XYZ"
    assert result.from_status is TaskStatus.SKIPPED
    assert result.transition is Transition.START
    assert result.reason == "invalid transition START from SKIPPED"


# ---------------------------------------------------------------------------
# Coverage of every transition × validity bucket (AC explicit)
# ---------------------------------------------------------------------------


def test_every_transition_has_at_least_one_valid_case() -> None:
    """All six Transition values must be exercised on the success branch."""
    transitions_with_valid_case = {t for (t, _s, _n) in _VALID_CASES}
    assert transitions_with_valid_case == set(Transition)


def test_every_transition_has_at_least_one_invalid_case() -> None:
    """All six Transition values must be exercised on the rejection branch."""
    transitions_with_invalid_case = {t for (t, _s) in _INVALID_CASES}
    assert transitions_with_invalid_case == set(Transition)
