"""Pure decision-gate evaluator for Whilly v4.1 (TASK-104c).

A *Decision Gate* refuses tasks that are obviously incomplete before they
reach a worker. The v3 lineage (``whilly/decision_gate.py``) bundled this
check with an LLM call; the v4.1 port keeps the cheap, deterministic part
— minimum description length, presence of acceptance criteria, presence
of test steps — and lifts it into the pure ``whilly.core`` layer so it
can run inside ``LifespanManager``-driven server code without touching
the network.

Public surface
--------------
* :class:`GateVerdictKind` — ``ALLOW`` | ``REJECT`` (str-mixed Enum so a
  call site can compare with the bare string and avoid an import).
* :class:`GateVerdict` — frozen dataclass carrying ``kind``, the
  ``missing`` tuple of stable labels (one per failing rule), and a
  human-readable ``reason`` for log lines / event payloads. Frozen so
  it is hashable and round-trippable through :func:`dataclasses.asdict`
  (PRD VAL-GATES-008).
* :func:`evaluate_decision_gate` — pure function: takes a
  :class:`~whilly.core.models.Task`, returns a :class:`GateVerdict`.
  No I/O, no globals mutated, no clock, no PRNG (PRD VAL-GATES-006 /
  VAL-GATES-007).

Documented missing-field labels
-------------------------------
The ``missing`` tuple uses these stable labels — part of the public
contract because callers (CLI ``--strict``, audit-event payloads) read
them straight back:

* ``"description"`` — :attr:`Task.description` shorter than
  :data:`MIN_DESCRIPTION_LEN` (after :meth:`str.strip`).
* ``"acceptance_criteria"`` — :attr:`Task.acceptance_criteria` is empty
  (``()`` or any other falsy collection).
* ``"test_steps"`` — :attr:`Task.test_steps` is empty.

Order is fixed: ``description`` → ``acceptance_criteria`` →
``test_steps``. The evaluator does **not** short-circuit on the first
failing rule (PRD VAL-GATES-005); the operator wants the full list at
once so re-running the gate after a single edit doesn't reveal a new
defect every time.

Layering
--------
This module lives under :mod:`whilly.core` and is governed by the
``core-purity`` import-linter contract (``.importlinter``). It must
**not** import from :mod:`whilly.adapters` or :mod:`whilly.api`. The
LLM-backed gate (still in :mod:`whilly.decision_gate`) and the SQL
``skip_task`` primitive (in :mod:`whilly.adapters.db.repository`) are
the impure counterparts that compose *above* this pure check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from whilly.core.models import Task

__all__ = [
    "GateVerdict",
    "GateVerdictKind",
    "LABEL_ACCEPTANCE_CRITERIA",
    "LABEL_DESCRIPTION",
    "LABEL_TEST_STEPS",
    "MIN_DESCRIPTION_LEN",
    "evaluate_decision_gate",
]


# Documented minimum description length, kept aligned with the v3
# ``whilly.decision_gate.MIN_DESCRIPTION_LEN`` constant. 20 characters is
# enough that "fix bug" / "do thing" / "TODO" auto-refuse without an LLM
# call but short enough that legitimately compact one-liners ("Bump
# fastapi to 0.115 in pyproject.toml") still pass.
MIN_DESCRIPTION_LEN: int = 20

# Stable labels. Public so call sites (``whilly plan apply --strict``,
# audit payloads, log lines) and tests can pin on a single source of
# truth instead of duplicating literal strings.
LABEL_DESCRIPTION: str = "description"
LABEL_ACCEPTANCE_CRITERIA: str = "acceptance_criteria"
LABEL_TEST_STEPS: str = "test_steps"


class GateVerdictKind(str, Enum):
    """Decision-gate outcome.

    The ``str`` mixin lets call sites compare ``verdict.kind == "ALLOW"``
    without importing the enum (handy for log-format users) — same
    pattern as :class:`whilly.core.models.TaskStatus` and
    :class:`whilly.core.state_machine.Transition`.
    """

    ALLOW = "ALLOW"
    REJECT = "REJECT"


@dataclass(frozen=True)
class GateVerdict:
    """Pure-data outcome of :func:`evaluate_decision_gate`.

    Attributes
    ----------
    kind:
        :class:`GateVerdictKind.ALLOW` when every rule passed,
        :class:`GateVerdictKind.REJECT` when any rule fired.
    missing:
        Tuple of stable labels naming each failing rule, in the order
        listed in the module docstring. Empty tuple when ``kind`` is
        ``ALLOW``. Tuple (rather than list) so the verdict stays
        immutable end-to-end and remains hashable for use as a dict key
        / set member (PRD VAL-GATES-008).
    reason:
        Short human-readable summary suitable for log lines and event
        payloads. ``None`` when ``kind`` is ``ALLOW``. The exact wording
        is not part of the public contract — callers that need stable
        machine-readable output should consume ``missing`` instead.
    """

    kind: GateVerdictKind
    missing: tuple[str, ...] = ()
    reason: str | None = None


# Precomputed ALLOW verdict — every healthy task collapses to the same
# value-object, so we save a few allocations per call without leaking
# mutability (frozen dataclass).
_ALLOW_VERDICT: GateVerdict = GateVerdict(kind=GateVerdictKind.ALLOW, missing=(), reason=None)


def evaluate_decision_gate(task: Task) -> GateVerdict:
    """Evaluate the pure decision gate for ``task``.

    Returns :data:`GateVerdictKind.ALLOW` when every rule passes;
    :data:`GateVerdictKind.REJECT` otherwise, with ``missing`` listing
    the labels that fired in the documented stable order. The function
    is total (always returns), pure (no I/O, no globals mutated, no
    clock / PRNG), and deterministic on its input.

    Rules
    -----
    * ``description`` — the stripped description must be at least
      :data:`MIN_DESCRIPTION_LEN` characters long. Whitespace-only
      descriptions count as 0 chars.
    * ``acceptance_criteria`` — at least one entry. Empty tuples,
      empty lists, ``None`` all fail.
    * ``test_steps`` — at least one entry. Same emptiness rules as
      ``acceptance_criteria``.

    The evaluator does **not** short-circuit on the first failing rule
    (PRD VAL-GATES-005). Reporting all missing fields at once lets the
    operator make a single round of edits to satisfy the gate — a
    short-circuit version would reveal a new defect on every re-run.
    """
    missing: list[str] = []

    # ``Task.description`` defaults to ``""`` in the dataclass; we strip
    # so a description that is purely whitespace ("   \n") still counts
    # as too-short rather than slipping through on raw length.
    desc = (task.description or "").strip()
    if len(desc) < MIN_DESCRIPTION_LEN:
        missing.append(LABEL_DESCRIPTION)

    # ``acceptance_criteria`` and ``test_steps`` default to ``()``; the
    # ``not`` test catches empty tuples / lists / None alike without
    # discriminating on the concrete container type — keeps the gate
    # robust against future schema changes that swap collection kinds.
    if not task.acceptance_criteria:
        missing.append(LABEL_ACCEPTANCE_CRITERIA)
    if not task.test_steps:
        missing.append(LABEL_TEST_STEPS)

    if not missing:
        return _ALLOW_VERDICT

    reason = "decision gate refused: missing " + ", ".join(missing)
    return GateVerdict(
        kind=GateVerdictKind.REJECT,
        missing=tuple(missing),
        reason=reason,
    )
