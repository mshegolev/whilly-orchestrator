"""High-level router — ties classifier + matcher into one decision.

Caller passes raw text (or an existing :class:`WorkItem`) and a hierarchy
adapter; router returns a :class:`RoutingDecision` describing what should
happen. Execution is caller's responsibility — the decision is plain data.

Flow:

    text ──► classifier ──► ClassificationResult (level + confidence)
                │
                ├─ if TASK   → search open Stories via adapter + matcher
                ├─ if STORY  → search open Epics via adapter + matcher
                └─ if EPIC   → no parent lookup (Epic is root)

    best_match.score ≥ threshold  → RoutingAction.LINK_AS_CHILD
    best_match.score < threshold  → RoutingAction.CREATE_ORPHAN
    classification flags REJECT   → RoutingAction.REJECT

Threshold defaults (``MATCH_THRESHOLD`` / ``CLASSIFY_THRESHOLD``) are tuned
to "don't act unless genuinely confident" — false-link is much more
expensive to undo than false-orphan.
"""

from __future__ import annotations

import logging

from whilly.classifier.base import (
    ClassificationResult,
    ParentMatcher,
    RoutingAction,
    RoutingDecision,
    TaskClassifier,
)
from whilly.classifier.llm import LLMClassifier
from whilly.classifier.matcher import LLMParentMatcher
from whilly.hierarchy.base import HierarchyAdapter, WorkItem

log = logging.getLogger("whilly.classifier.router")


# Decision thresholds. The router can be constructed with overrides.
DEFAULT_MATCH_THRESHOLD = 0.55
DEFAULT_CLASSIFY_THRESHOLD = 0.6


class Router:
    """Compose a classifier + matcher into a single decision engine."""

    def __init__(
        self,
        classifier: TaskClassifier | None = None,
        matcher: ParentMatcher | None = None,
        *,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
        classify_threshold: float = DEFAULT_CLASSIFY_THRESHOLD,
        parent_search_label: str | None = None,
    ):
        """Args:
        classifier / matcher: swap for tests or alternate implementations.
            Defaults use LLM-backed impls.
        match_threshold: minimum parent-match score to trigger LINK_AS_CHILD.
            Below → CREATE_ORPHAN.
        classify_threshold: minimum classification confidence. Below →
            still attempt routing but tag the decision with a warning flag
            (caller shows a "review me" badge).
        parent_search_label: label applied to adapter.list_at_level() when
            searching for parent candidates. Typically ``"whilly:ready"`` or
            a custom filter; None = list everything.
        """
        self.classifier = classifier or LLMClassifier()
        self.matcher = matcher or LLMParentMatcher()
        self.match_threshold = match_threshold
        self.classify_threshold = classify_threshold
        self.parent_search_label = parent_search_label

    # ── Entry points ──────────────────────────────────────────────────────

    def route_text(
        self,
        title: str,
        body: str,
        adapter: HierarchyAdapter,
    ) -> RoutingDecision:
        """Classify + match + decide for raw text input."""
        classification = self.classifier.classify(title, body)
        return self._route_with_classification(title, body, classification, adapter)

    def route_item(
        self,
        item: WorkItem,
        adapter: HierarchyAdapter,
    ) -> RoutingDecision:
        """Classify + match + decide for an existing WorkItem.

        Useful when an inbox already has items placed at the wrong level
        (e.g., a Story-sized issue that really should be a Task under
        another Story). Returns a decision that re-routes the item.
        """
        return self.route_text(item.title, item.body, adapter)

    # ── Internals ─────────────────────────────────────────────────────────

    def _route_with_classification(
        self,
        title: str,
        body: str,
        classification: ClassificationResult,
        adapter: HierarchyAdapter,
    ) -> RoutingDecision:
        # Hard rejection comes from classifier flags.
        if "below-length-threshold" in classification.flags or "out-of-scope" in classification.flags:
            return RoutingDecision(
                action=RoutingAction.REJECT,
                classification=classification,
                title=title,
                body=body,
            )

        parent_level = classification.level.parent
        if parent_level is None:
            # Epic — roots have no parent. Create orphan at EPIC level.
            return RoutingDecision(
                action=RoutingAction.CREATE_ORPHAN,
                classification=classification,
                title=title,
                body=body,
            )

        # Fetch candidate parents at the level above.
        try:
            candidates = adapter.list_at_level(parent_level, label=self.parent_search_label)
        except Exception as exc:  # noqa: BLE001
            log.warning("candidate fetch failed (%r) — creating orphan", exc)
            return RoutingDecision(
                action=RoutingAction.CREATE_ORPHAN,
                classification=classification,
                title=title,
                body=body,
            )

        if not candidates:
            return RoutingDecision(
                action=RoutingAction.CREATE_ORPHAN,
                classification=classification,
                title=title,
                body=body,
            )

        matches = self.matcher.find_matches(title, body, candidates, max_matches=3)
        if not matches or matches[0].score < self.match_threshold:
            return RoutingDecision(
                action=RoutingAction.CREATE_ORPHAN,
                classification=classification,
                title=title,
                body=body,
                parent_confidence=matches[0].score if matches else 0.0,
            )

        top = matches[0]
        return RoutingDecision(
            action=RoutingAction.LINK_AS_CHILD,
            classification=classification,
            title=title,
            body=body,
            target_parent=top.parent,
            parent_confidence=top.score,
        )

    # ── Decision execution ────────────────────────────────────────────────

    def apply(self, decision: RoutingDecision, adapter: HierarchyAdapter) -> RoutingDecision:
        """Execute the decision via the hierarchy adapter.

        Mutates *decision* — sets ``applied=True`` on success. Returns
        the same decision object for chaining.
        """
        if decision.applied:
            return decision

        if decision.action is RoutingAction.REJECT:
            decision.applied = True
            return decision

        if decision.action is RoutingAction.LINK_AS_CHILD:
            if decision.target_parent is None:
                log.warning("LINK_AS_CHILD without target_parent — skipping apply")
                return decision
            adapter.create_child(
                decision.target_parent,
                title=decision.title,
                body=decision.body,
            )
            decision.applied = True
            return decision

        if decision.action is RoutingAction.CREATE_ORPHAN:
            # We don't have a "create at level X without parent" on the
            # Protocol because each tracker handles this differently
            # (GitHub: project draft or repo issue; Jira: Epic/Story
            # directly). The router exposes the decision and the caller
            # picks the creation path. For now we just flag applied — the
            # caller's CLI layer does the real creation.
            log.info(
                "CREATE_ORPHAN decision recorded for level=%s — caller applies creation",
                decision.classification.level,
            )
            return decision

        if decision.action is RoutingAction.PROMOTE_DRAFT:
            # Requires a WorkItem-shaped decision target; left to the
            # specific pipeline that uses this action.
            log.info("PROMOTE_DRAFT deferred — caller applies via adapter.promote()")
            return decision

        return decision


# ── Human-readable summary ────────────────────────────────────────────────────


def format_decision(d: RoutingDecision) -> str:
    """One-string summary — CLI output / event logs / JSONL trails."""
    lines = [
        f"action:         {d.action.value}",
        f"level:          {d.classification.level.value} "
        f"(confidence {d.classification.confidence:.2f}, complexity {d.classification.complexity_score}/10)",
        f"reasoning:      {d.classification.reasoning or '(none)'}",
    ]
    if d.classification.flags:
        lines.append(f"flags:          {', '.join(d.classification.flags)}")
    if d.target_parent is not None:
        lines.append(
            f"target parent:  {d.target_parent.level.value} {d.target_parent.id!r} (score {d.parent_confidence:.2f})"
        )
        lines.append(f"parent title:   {d.target_parent.title[:80]}")
    lines.append(f"applied:        {d.applied}")
    return "\n".join(lines)
