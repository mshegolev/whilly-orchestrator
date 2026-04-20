"""Smart task router — Protocol surface.

When a new idea / issue / request arrives, whilly has to decide:

1. **What level is this?** Epic, Story, or Task.
2. **Does it have a natural parent?** An open Story a Task could hang under,
   an open Epic a Story could decompose from.
3. **What action should follow?** Link it as a child, promote it to a real
   item, or create it standalone.

This module defines the data shapes (``ClassificationResult``,
``RoutingDecision``) and Protocols (``TaskClassifier``, ``ParentMatcher``)
that the LLM / heuristic / future impls plug into. Same idiom as
``whilly.agents.AgentBackend``, ``whilly.workflow.BoardSink``,
``whilly.quality.QualityGate``, ``whilly.hierarchy.HierarchyAdapter``.

Decisions, not actions
----------------------

Classifier and router produce **decisions** — plain data. Executing the
decision (creating the issue, linking it to parent) goes through
:class:`whilly.hierarchy.HierarchyAdapter`. This split keeps the
classifier testable without a live tracker, and keeps the adapter free
of business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from whilly.hierarchy.base import HierarchyLevel, WorkItem


# ── Action enum — what should be done with a new item ────────────────────────


class RoutingAction(str, Enum):
    """What the router decided to do with a new input."""

    LINK_AS_CHILD = "link_as_child"
    """The item fits as a child of an existing open parent — attach it."""

    CREATE_ORPHAN = "create_orphan"
    """No parent found (or confidence too low). Create at its classified
    level with no parent, flag for human review."""

    PROMOTE_DRAFT = "promote_draft"
    """Input is currently a draft; materialise to its classified level
    and THEN try to attach to a parent in a second pass."""

    REJECT = "reject"
    """Classifier refused — gibberish, below-threshold-length, duplicate
    of an existing item, etc. Caller should show reasoning to the user."""


# ── ClassificationResult — output of the classifier step ─────────────────────


@dataclass
class ClassificationResult:
    """Classifier's verdict on a piece of input text.

    Fields:
        level: the best-fit hierarchy level.
        confidence: 0.0–1.0. Values below 0.6 typically mean "ask a
            human" — the router's threshold is configurable.
        reasoning: 1–3 sentence justification. Shown to the user when
            confidence is low so they can override.
        estimated_children: expected number of items one level down
            (Epic → ~N stories; Story → ~M tasks). Zero for TASK level.
        complexity_score: 1 (trivial) to 10 (architect-level). Used by
            downstream gates (Decision Gate, budget allocation).
        flags: free-form observations ("duplicate-suspected",
            "needs-clarification", "out-of-scope"). Surfaced in UI.
    """

    level: HierarchyLevel
    confidence: float = 1.0
    reasoning: str = ""
    estimated_children: int = 0
    complexity_score: int = 1
    flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.level, str) and not isinstance(self.level, HierarchyLevel):
            self.level = HierarchyLevel(self.level)
        # Clamp to sane ranges so downstream code never has to validate.
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.complexity_score = max(1, min(10, int(self.complexity_score)))
        self.estimated_children = max(0, int(self.estimated_children))

    @property
    def is_high_confidence(self) -> bool:
        """True when the classifier is confident enough to auto-apply."""
        return self.confidence >= 0.75


# ── ParentMatch — output of the parent-matching step ─────────────────────────


@dataclass
class ParentMatch:
    """Single candidate parent with a match score.

    ``score`` is in 0.0–1.0. A :class:`ParentMatcher` returns an ordered
    list sorted by score descending — the router picks the head unless
    the top score is below its threshold.
    """

    parent: WorkItem
    score: float
    reasoning: str = ""

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, float(self.score)))


# ── RoutingDecision — the router's final verdict ────────────────────────────


@dataclass
class RoutingDecision:
    """What the router decided to do with the input.

    This is the complete plan — caller can apply it via the hierarchy
    adapter (`create_child`, `promote`, `link`) or print it for a
    human to approve first.

    Fields:
        action: what to do (see :class:`RoutingAction`).
        classification: the classifier output that drove the decision.
        target_parent: the parent to link under (None for ORPHAN /
            REJECT).
        title / body: normalised text for creating the item. Classifier
            may have cleaned up the input (trim whitespace, pull title
            from a multi-line body).
        parent_confidence: how confident the matcher was about the parent
            (distinct from classification.confidence).
        applied: set to True after the decision was executed via adapter.
            Lets the CLI print "dry-run plan" vs "applied plan" uniformly.
    """

    action: RoutingAction
    classification: ClassificationResult
    title: str
    body: str
    target_parent: WorkItem | None = None
    parent_confidence: float = 0.0
    applied: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.action, str) and not isinstance(self.action, RoutingAction):
            self.action = RoutingAction(self.action)
        self.parent_confidence = max(0.0, min(1.0, float(self.parent_confidence)))


# ── Protocols ────────────────────────────────────────────────────────────────


class TaskClassifier(Protocol):
    """Decide the hierarchy level + complexity of a piece of input."""

    kind: str

    def classify(self, title: str, body: str) -> ClassificationResult:
        """Return a :class:`ClassificationResult`. Never raises — on LLM
        failure impls should fall back to a heuristic or a low-confidence
        default (TASK level, confidence 0.3, reasoning explaining the
        fallback)."""
        ...


class ParentMatcher(Protocol):
    """Pick the best existing parent for a candidate item, if any."""

    kind: str

    def find_matches(
        self,
        candidate_title: str,
        candidate_body: str,
        candidates: list[WorkItem],
        *,
        max_matches: int = 3,
    ) -> list[ParentMatch]:
        """Return up to *max_matches* best-fit parents, sorted by score
        descending. Empty list = "no good match" (router turns this into
        :attr:`RoutingAction.CREATE_ORPHAN`)."""
        ...
