"""Unit tests for the pure Decision Gate evaluator (TASK-104c).

The contract under test is in :mod:`whilly.core.gates`; the
assertions below mirror VAL-GATES-001 through VAL-GATES-008 in
``validation-contract.md``. The only fixtures we use are stdlib /
pytest defaults — no DB, no network, no subprocess. The "I/O is
forbidden" assertion (VAL-GATES-007) explicitly monkeypatches every
plausible I/O entry-point to raise so a regression in the evaluator
that reaches for asyncpg / requests / open() etc. would surface as a
test failure rather than passing silently.
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket
import subprocess
import urllib.request
from typing import Any

import pytest

from whilly.core.gates import (
    LABEL_ACCEPTANCE_CRITERIA,
    LABEL_DESCRIPTION,
    LABEL_TEST_STEPS,
    MIN_DESCRIPTION_LEN,
    GateVerdict,
    GateVerdictKind,
    evaluate_decision_gate,
)
from whilly.core.models import Priority, Task, TaskStatus


# ─── helpers ─────────────────────────────────────────────────────────────


def _make_task(
    *,
    description: str = "x" * MIN_DESCRIPTION_LEN,
    acceptance_criteria: tuple[str, ...] = ("AC-1",),
    test_steps: tuple[str, ...] = ("step-1",),
    task_id: str = "T-001",
    priority: Priority = Priority.MEDIUM,
) -> Task:
    """Build a healthy default Task with overrideable fields.

    Mirrors the v3 ``decision_gate`` test idiom: a single helper
    produces a ``Task`` whose every gate-relevant field passes by
    default, and individual tests poke holes in one field at a time
    to exercise each rule.
    """
    return Task(
        id=task_id,
        status=TaskStatus.PENDING,
        priority=priority,
        description=description,
        acceptance_criteria=acceptance_criteria,
        test_steps=test_steps,
    )


# ─── VAL-GATES-001: ALLOW for healthy task ───────────────────────────────


def test_allow_for_healthy_task() -> None:
    """Healthy task → ALLOW verdict, no missing labels, no reason."""
    verdict = evaluate_decision_gate(_make_task())

    assert verdict.kind == GateVerdictKind.ALLOW
    assert verdict.kind == "ALLOW", "GateVerdictKind must compare equal to its str value"
    assert verdict.missing == ()
    assert verdict.reason is None


def test_allow_with_long_description() -> None:
    """A description well above the floor still ALLOWs."""
    verdict = evaluate_decision_gate(
        _make_task(description="A reasonably descriptive task spanning multiple sentences.")
    )
    assert verdict.kind == GateVerdictKind.ALLOW
    assert verdict.missing == ()


# ─── VAL-GATES-002: REJECT names empty acceptance_criteria ───────────────


def test_reject_names_empty_acceptance_criteria() -> None:
    """Missing ``acceptance_criteria`` only → REJECT names exactly that label."""
    verdict = evaluate_decision_gate(_make_task(acceptance_criteria=()))

    assert verdict.kind == GateVerdictKind.REJECT
    assert LABEL_ACCEPTANCE_CRITERIA in verdict.missing
    assert LABEL_TEST_STEPS not in verdict.missing
    assert LABEL_DESCRIPTION not in verdict.missing


# ─── VAL-GATES-003: REJECT names empty test_steps ────────────────────────


def test_reject_names_empty_test_steps() -> None:
    """Missing ``test_steps`` only → REJECT names exactly that label."""
    verdict = evaluate_decision_gate(_make_task(test_steps=()))

    assert verdict.kind == GateVerdictKind.REJECT
    assert LABEL_TEST_STEPS in verdict.missing
    assert LABEL_ACCEPTANCE_CRITERIA not in verdict.missing
    assert LABEL_DESCRIPTION not in verdict.missing


# ─── VAL-GATES-004: REJECT names short description ───────────────────────


def test_reject_names_short_description() -> None:
    """A description below the documented floor → REJECT names ``description``."""
    short = "x" * (MIN_DESCRIPTION_LEN - 1)
    verdict = evaluate_decision_gate(_make_task(description=short))

    assert verdict.kind == GateVerdictKind.REJECT
    assert LABEL_DESCRIPTION in verdict.missing


def test_reject_whitespace_only_description() -> None:
    """A whitespace-only description has effective length 0 — also REJECT."""
    verdict = evaluate_decision_gate(_make_task(description="   \n\t   "))
    assert verdict.kind == GateVerdictKind.REJECT
    assert LABEL_DESCRIPTION in verdict.missing


def test_allow_at_exactly_min_description_len() -> None:
    """Boundary: ``len == MIN_DESCRIPTION_LEN`` passes (the rule is ``<``)."""
    desc = "x" * MIN_DESCRIPTION_LEN
    verdict = evaluate_decision_gate(_make_task(description=desc))
    assert verdict.kind == GateVerdictKind.ALLOW


# ─── VAL-GATES-005: REJECT enumerates all missing fields ────────────────


def test_reject_enumerates_all_missing_fields() -> None:
    """All three rules failing simultaneously → all three labels reported."""
    verdict = evaluate_decision_gate(
        _make_task(
            description="short",  # < MIN_DESCRIPTION_LEN
            acceptance_criteria=(),
            test_steps=(),
        )
    )
    assert verdict.kind == GateVerdictKind.REJECT
    assert set(verdict.missing) == {
        LABEL_DESCRIPTION,
        LABEL_ACCEPTANCE_CRITERIA,
        LABEL_TEST_STEPS,
    }
    assert len(verdict.missing) == 3
    # Stable order is part of the public contract.
    assert verdict.missing == (
        LABEL_DESCRIPTION,
        LABEL_ACCEPTANCE_CRITERIA,
        LABEL_TEST_STEPS,
    )


# ─── VAL-GATES-006: Determinism on equal inputs ─────────────────────────


@pytest.mark.parametrize(
    "task",
    [
        _make_task(),  # healthy
        _make_task(acceptance_criteria=()),  # missing AC
        _make_task(test_steps=()),  # missing test_steps
        _make_task(description="oops"),  # short desc
        _make_task(description="oops", acceptance_criteria=(), test_steps=()),  # all missing
    ],
)
def test_determinism_on_equal_inputs(task: Task) -> None:
    """Two evaluations on equal inputs return ``==``-equal verdicts."""
    first = evaluate_decision_gate(task)
    for _ in range(50):
        again = evaluate_decision_gate(task)
        assert first == again
        assert first.kind == again.kind
        assert first.missing == again.missing
        assert first.reason == again.reason


# ─── VAL-GATES-007: No I/O performed ────────────────────────────────────


def test_no_io_performed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every plausible I/O entrypoint to raise; the gate must still run.

    A regression that pulls asyncpg / urllib / subprocess / open() into
    :func:`evaluate_decision_gate` would trip one of these patches and
    surface as ``AssertionError("I/O attempted")``. Each patch is a
    *defensive* signal: the gate is supposed to be pure, and these
    patches prove it.
    """

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("I/O attempted")

    monkeypatch.setattr(socket, "socket", _fail)
    monkeypatch.setattr(subprocess, "run", _fail)
    monkeypatch.setattr(subprocess, "Popen", _fail)
    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    monkeypatch.setattr("builtins.open", _fail)
    monkeypatch.setattr(asyncio, "get_event_loop", _fail)

    # Healthy task path.
    healthy = evaluate_decision_gate(_make_task())
    assert healthy.kind == GateVerdictKind.ALLOW

    # Rejecting task path.
    rejecting = evaluate_decision_gate(_make_task(acceptance_criteria=(), test_steps=()))
    assert rejecting.kind == GateVerdictKind.REJECT


# ─── VAL-GATES-008: Verdict shape is hashable / serializable ────────────


def test_verdict_is_hashable() -> None:
    """Frozen dataclass → hashable, suitable for ``set`` / ``dict`` keys."""
    verdict = evaluate_decision_gate(_make_task())
    assert hash(verdict) == hash(verdict)
    assert {verdict, verdict} == {verdict}
    # Use as a dict key — common pattern in caches.
    cache: dict[GateVerdict, str] = {verdict: "ok"}
    assert cache[verdict] == "ok"


def test_verdict_is_dataclass_serialisable() -> None:
    """``dataclasses.asdict`` round-trips without raising."""
    verdict = evaluate_decision_gate(_make_task(acceptance_criteria=()))
    payload = dataclasses.asdict(verdict)
    # ``Enum`` values stay as-is; the ``str`` mixin makes them
    # JSON-friendly without an extra conversion step.
    assert payload["kind"] == GateVerdictKind.REJECT
    assert payload["missing"] == (LABEL_ACCEPTANCE_CRITERIA,)
    assert isinstance(payload["reason"], str)


def test_verdict_is_immutable() -> None:
    """Frozen dataclass → attribute assignment raises."""
    verdict = evaluate_decision_gate(_make_task())
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.reason = "tampered"  # type: ignore[misc]


# ─── REJECT verdict carries a non-empty reason string ────────────────────


def test_reject_verdict_reason_mentions_missing_fields() -> None:
    """Free-form ``reason`` should reference at least one missing label.

    The exact wording is not part of the public contract, but the
    presence of the missing-field labels in the reason makes log
    grepping reliable for operators.
    """
    verdict = evaluate_decision_gate(_make_task(test_steps=()))
    assert verdict.reason is not None
    assert LABEL_TEST_STEPS in verdict.reason
