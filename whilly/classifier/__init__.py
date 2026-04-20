"""Smart task routing — classify + match + route.

When a new idea arrives (CLI flag, webhook, inbox item), the router decides:

1. Is it an Epic, Story, or Task?
2. Does an open parent at the level above fit well?
3. Should we link it as a child, create an orphan, or reject?

Typical use::

    from whilly.classifier import Router
    from whilly.hierarchy import get_adapter

    adapter = get_adapter("github", project_url=..., repo="me/api")
    router = Router(parent_search_label="whilly:ready")
    decision = router.route_text(title, body, adapter)
    print(format_decision(decision))
    if decision.classification.is_high_confidence:
        router.apply(decision, adapter)

See ADR-018 for the design rationale (why LLM over embeddings, why the
classify+match pipeline is kept as two separate LLM calls rather than one,
confidence/threshold discipline).
"""

from __future__ import annotations

from whilly.classifier.base import (
    ClassificationResult,
    ParentMatch,
    ParentMatcher,
    RoutingAction,
    RoutingDecision,
    TaskClassifier,
)
from whilly.classifier.epic_inferrer import InferredEpic, infer_epics
from whilly.classifier.heuristic import HeuristicClassifier
from whilly.classifier.llm import LLMClassifier
from whilly.classifier.matcher import LLMParentMatcher, NoopParentMatcher
from whilly.classifier.rebuilder import (
    HierarchyAssignment,
    HierarchyTree,
    apply_tree,
    format_tree,
    rebuild_hierarchy,
)
from whilly.classifier.router import Router, format_decision

__all__ = [
    "ClassificationResult",
    "ParentMatch",
    "ParentMatcher",
    "RoutingAction",
    "RoutingDecision",
    "TaskClassifier",
    "HeuristicClassifier",
    "LLMClassifier",
    "LLMParentMatcher",
    "NoopParentMatcher",
    "Router",
    "format_decision",
    "HierarchyAssignment",
    "HierarchyTree",
    "rebuild_hierarchy",
    "apply_tree",
    "format_tree",
    "InferredEpic",
    "infer_epics",
]
