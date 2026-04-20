"""Reconstruct an Epic/Story/Task hierarchy from a flat item list.

Given a pile of tracker items with no parent-child relationships
(common after an import, a project migration, or just neglect), this
module:

1. **Classifies** every item into Epic / Story / Task via
   :class:`whilly.classifier.llm.LLMClassifier` (or whatever classifier
   the caller injects).
2. **Matches parents** bottom-up: each Task gets the best Story as
   parent via :class:`whilly.classifier.matcher.LLMParentMatcher`;
   each Story gets the best Epic.
3. **Returns a :class:`HierarchyTree`** — the proposal. Each
   assignment carries a confidence score so the caller can gate on it.
4. **Optionally applies** the proposal by calling
   :meth:`whilly.hierarchy.HierarchyAdapter.link` for every
   above-threshold assignment.

Costs scale linearly with *items* — one classifier call per item,
plus one matcher call per non-root item. For ~80 items that's ~160
haiku calls, typically under $0.20 total.

The rebuilder does *not* invent missing parents. If you have 20 Tasks
and zero Stories, no Task gets a parent — the unparented list surfaces
them so a human can create Stories or run the Epic→Stories pipeline
(Phase 2) to materialise them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from whilly.classifier.base import (
    ClassificationResult,
    ParentMatcher,
    TaskClassifier,
)
from whilly.classifier.llm import LLMClassifier
from whilly.classifier.matcher import LLMParentMatcher
from whilly.hierarchy.base import HierarchyAdapter, HierarchyLevel, WorkItem

log = logging.getLogger("whilly.classifier.rebuilder")


DEFAULT_MATCH_THRESHOLD = 0.55


# ── Proposal data types ──────────────────────────────────────────────────────


@dataclass
class HierarchyAssignment:
    """One proposed parent assignment.

    Exists so the caller can inspect, filter, or gate on confidence
    before applying — the threshold the rebuilder uses is a default,
    not a hard rule. Applied-state mutation happens in-place when
    :func:`apply_tree` succeeds.
    """

    child: WorkItem
    parent: WorkItem
    score: float
    reasoning: str = ""
    applied: bool = False


@dataclass
class HierarchyTree:
    """Full rebuild proposal for a flat item list.

    Fields:
        epics / stories / tasks: items grouped by classified level.
            Each item carries the *classified* level (which may differ
            from whatever the adapter reported) — see :meth:`classified_level_of`.
        assignments: proposed parent links (Story→Epic, Task→Story).
        unparented: items that couldn't find a good-enough parent.
            Epics are intentionally here too — they have no parent level.
        classifications: keyed by item id, the full classifier output
            (for downstream tooling that wants confidence/flags/reasoning).
    """

    epics: list[WorkItem] = field(default_factory=list)
    stories: list[WorkItem] = field(default_factory=list)
    tasks: list[WorkItem] = field(default_factory=list)
    assignments: list[HierarchyAssignment] = field(default_factory=list)
    unparented: list[WorkItem] = field(default_factory=list)
    classifications: dict[str, ClassificationResult] = field(default_factory=dict)

    def classified_level_of(self, item_id: str) -> HierarchyLevel | None:
        classification = self.classifications.get(item_id)
        return classification.level if classification else None

    @property
    def counts(self) -> dict[str, int]:
        """Headline numbers for dashboards / CLI output."""
        return {
            "epics": len(self.epics),
            "stories": len(self.stories),
            "tasks": len(self.tasks),
            "assignments": len(self.assignments),
            "unparented": len(self.unparented),
        }


# ── The rebuilder ────────────────────────────────────────────────────────────


def rebuild_hierarchy(
    items: list[WorkItem],
    *,
    classifier: TaskClassifier | None = None,
    matcher: ParentMatcher | None = None,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> HierarchyTree:
    """Classify + match + return a proposed hierarchy.

    The *items* are treated as opaque — the rebuilder doesn't trust
    :attr:`WorkItem.level` because it's often wrong on imported data
    (that's why we're reconstructing). Classification is authoritative.

    Args:
        items: the flat list to organise.
        classifier: injectable for tests / alternative impls. Defaults
            to :class:`whilly.classifier.llm.LLMClassifier`.
        matcher: same — defaults to
            :class:`whilly.classifier.matcher.LLMParentMatcher`.
        match_threshold: minimum parent-match score to record an
            assignment. Below this the child goes to *unparented*.

    Never raises — a classifier or matcher that fails internally just
    yields lower-confidence results (they're written to never raise
    themselves; see their docstrings).
    """
    cls = classifier or LLMClassifier()
    mch = matcher or LLMParentMatcher()

    tree = HierarchyTree()
    if not items:
        return tree

    # Step 1: classify every item. Store the classified level back on a
    # copy of each WorkItem so downstream code works off one consistent
    # source of truth.
    reclassified: list[WorkItem] = []
    for item in items:
        classification = cls.classify(item.title, item.body)
        tree.classifications[item.id] = classification
        reclassified.append(
            WorkItem(
                id=item.id,
                level=classification.level,
                title=item.title,
                body=item.body,
                parent_id=item.parent_id,  # preserved if already set
                children_ids=list(item.children_ids),
                external_ref=dict(item.external_ref),
                labels=list(item.labels),
                status=item.status,
            )
        )

    # Step 2: bucket by classified level.
    epics = [i for i in reclassified if i.level is HierarchyLevel.EPIC]
    stories = [i for i in reclassified if i.level is HierarchyLevel.STORY]
    tasks = [i for i in reclassified if i.level is HierarchyLevel.TASK]
    tree.epics = epics
    tree.stories = stories
    tree.tasks = tasks

    # Step 3: Task → Story assignments.
    if stories:
        for task in tasks:
            if task.parent_id:
                # Already has a parent — respect it (don't re-route).
                continue
            matches = mch.find_matches(task.title, task.body, stories, max_matches=1)
            if matches and matches[0].score >= match_threshold:
                best = matches[0]
                tree.assignments.append(
                    HierarchyAssignment(
                        child=task,
                        parent=best.parent,
                        score=best.score,
                        reasoning=best.reasoning,
                    )
                )
            else:
                tree.unparented.append(task)
    else:
        tree.unparented.extend(t for t in tasks if not t.parent_id)

    # Step 4: Story → Epic assignments.
    if epics:
        for story in stories:
            if story.parent_id:
                continue
            matches = mch.find_matches(story.title, story.body, epics, max_matches=1)
            if matches and matches[0].score >= match_threshold:
                best = matches[0]
                tree.assignments.append(
                    HierarchyAssignment(
                        child=story,
                        parent=best.parent,
                        score=best.score,
                        reasoning=best.reasoning,
                    )
                )
            else:
                tree.unparented.append(story)
    else:
        tree.unparented.extend(s for s in stories if not s.parent_id)

    # Epics themselves: roots, no parent. They're neither assigned nor
    # unparented — don't add to unparented (clutters output). The counts
    # dict has them separately.

    return tree


# ── Applying the proposal via the adapter ───────────────────────────────────


def apply_tree(tree: HierarchyTree, adapter: HierarchyAdapter) -> int:
    """Call :meth:`HierarchyAdapter.link` for every assignment.

    Mutates each :class:`HierarchyAssignment` in-place (``applied=True``
    on success). Returns the count of successfully applied links.

    On any adapter-side failure (the adapter's ``link`` returns False
    or raises), the assignment is left with ``applied=False`` and the
    error is logged — the rebuilder is best-effort.
    """
    success = 0
    for assignment in tree.assignments:
        if assignment.applied:
            success += 1
            continue
        try:
            ok = adapter.link(assignment.parent, assignment.child)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "adapter.link failed for %s → %s: %r",
                assignment.child.id,
                assignment.parent.id,
                exc,
            )
            ok = False
        if ok:
            assignment.applied = True
            success += 1
    return success


# ── Pretty rendering ────────────────────────────────────────────────────────


def format_tree(tree: HierarchyTree, *, max_title: int = 60) -> str:
    """Produce a human-readable indented tree for CLI output.

    Epics at the left margin, Stories indented one level, Tasks two.
    Unparented items printed in a separate footer section so the
    reviewer knows what still needs attention.
    """
    lines: list[str] = []

    def _title(item: WorkItem) -> str:
        t = (item.title or "(no title)").strip().replace("\n", " ")
        return t[:max_title] + ("…" if len(t) > max_title else "")

    # Index assignments by parent for fast lookup.
    children_of: dict[str, list[HierarchyAssignment]] = {}
    for a in tree.assignments:
        children_of.setdefault(a.parent.id, []).append(a)

    lines.append(
        f"Hierarchy: {tree.counts['epics']} epic(s), "
        f"{tree.counts['stories']} story/stories, "
        f"{tree.counts['tasks']} task(s), "
        f"{tree.counts['assignments']} assignments, "
        f"{tree.counts['unparented']} unparented"
    )
    lines.append("")

    # Epics + their stories + tasks
    for epic in tree.epics:
        lines.append(f"EPIC: {_title(epic)}  [{epic.id}]")
        for story_a in children_of.get(epic.id, []):
            story = story_a.child
            lines.append(f"  └── STORY: {_title(story)}  " f"(score {story_a.score:.2f})  [{story.id}]")
            for task_a in children_of.get(story.id, []):
                task = task_a.child
                lines.append(f"        └── TASK: {_title(task)}  " f"(score {task_a.score:.2f})  [{task.id}]")
        lines.append("")

    # Orphan stories (classified Story but no epic matched)
    orphan_stories = [s for s in tree.stories if s in tree.unparented]
    if orphan_stories:
        lines.append("UNPARENTED STORIES (no matching epic):")
        for s in orphan_stories:
            for task_a in children_of.get(s.id, []):
                pass  # rendered below the story
            lines.append(f"  STORY: {_title(s)}  [{s.id}]")
            for task_a in children_of.get(s.id, []):
                lines.append(f"    └── TASK: {_title(task_a.child)}  " f"(score {task_a.score:.2f})")
        lines.append("")

    # Orphan tasks (no matching story)
    orphan_tasks = [t for t in tree.tasks if t in tree.unparented]
    if orphan_tasks:
        lines.append("UNPARENTED TASKS (no matching story):")
        for t in orphan_tasks:
            lines.append(f"  TASK: {_title(t)}  [{t.id}]")
        lines.append("")

    return "\n".join(lines).rstrip()
