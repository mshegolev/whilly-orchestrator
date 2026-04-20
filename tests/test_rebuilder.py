"""Tests for :mod:`whilly.classifier.rebuilder` — classify + match pipeline
over a flat list. Classifier and matcher are stubbed so the suite runs
offline in milliseconds.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from whilly.classifier import (
    HierarchyAssignment,
    HierarchyTree,
    apply_tree,
    format_tree,
    rebuild_hierarchy,
)
from whilly.classifier.base import ClassificationResult, ParentMatch
from whilly.hierarchy.base import HierarchyLevel, WorkItem


# ── Stubs ────────────────────────────────────────────────────────────────────


class _FakeClassifier:
    """Classifier that reads a pre-set mapping of id → level."""

    kind = "fake"

    def __init__(self, by_id: dict[str, HierarchyLevel]):
        self._by_id = by_id

    def classify(self, title: str, body: str) -> ClassificationResult:
        # Tests pass the id in the title to keep stubs trivial.
        level = self._by_id.get(title, HierarchyLevel.TASK)
        return ClassificationResult(level=level, confidence=0.9, reasoning=f"stub:{title}")


class _FakeMatcher:
    """Matcher that returns ``matches_by_child_title[title]`` or []."""

    kind = "fake"

    def __init__(self, matches_by_title: dict[str, list[ParentMatch]]):
        self._by_title = matches_by_title

    def find_matches(self, candidate_title, candidate_body, candidates, *, max_matches=3):
        return list(self._by_title.get(candidate_title, []))[:max_matches]


def _item(level, title, parent_id=None):
    # id == title for test ergonomics — easy to look up and stub.
    return WorkItem(id=title, level=level, title=title, body="", parent_id=parent_id)


# ── Core rebuild flow ────────────────────────────────────────────────────────


class TestRebuild:
    def test_empty_input_returns_empty_tree(self):
        tree = rebuild_hierarchy([])
        assert tree.counts == {
            "epics": 0,
            "stories": 0,
            "tasks": 0,
            "assignments": 0,
            "unparented": 0,
            "inferred_epics": 0,
        }

    def test_reclassifies_by_classifier_not_input_level(self):
        """Input may have wrong levels (imported data). Classifier wins."""
        # Input items claim STORY level, classifier says TASK.
        items = [
            _item(HierarchyLevel.STORY, "t1"),
            _item(HierarchyLevel.STORY, "t2"),
        ]
        cls = _FakeClassifier({"t1": HierarchyLevel.TASK, "t2": HierarchyLevel.TASK})
        matcher = _FakeMatcher({})
        tree = rebuild_hierarchy(items, classifier=cls, matcher=matcher)
        assert tree.counts["tasks"] == 2
        assert tree.counts["stories"] == 0

    def test_task_linked_to_best_story(self):
        story = _item(HierarchyLevel.STORY, "OAuth story")
        task = _item(HierarchyLevel.TASK, "Fix callback")
        cls = _FakeClassifier({"OAuth story": HierarchyLevel.STORY, "Fix callback": HierarchyLevel.TASK})
        matcher = _FakeMatcher(
            {
                "Fix callback": [ParentMatch(parent=story, score=0.9, reasoning="oauth")],
            }
        )
        tree = rebuild_hierarchy([story, task], classifier=cls, matcher=matcher)
        assert tree.counts["assignments"] == 1
        assignment = tree.assignments[0]
        assert assignment.child.id == "Fix callback"
        assert assignment.parent.id == "OAuth story"
        assert assignment.score == 0.9

    def test_below_threshold_goes_to_unparented(self):
        story = _item(HierarchyLevel.STORY, "OAuth story")
        task = _item(HierarchyLevel.TASK, "Unrelated")
        cls = _FakeClassifier({"OAuth story": HierarchyLevel.STORY, "Unrelated": HierarchyLevel.TASK})
        matcher = _FakeMatcher(
            {
                "Unrelated": [ParentMatch(parent=story, score=0.1)],
            }
        )
        tree = rebuild_hierarchy([story, task], classifier=cls, matcher=matcher)
        assert tree.assignments == []
        # Task is orphaned because match score below threshold.
        # Story also ends up orphaned (no epics at all) — that's covered by
        # test_story_without_epic_goes_unparented; this test only asserts task.
        assert task in tree.unparented

    def test_story_linked_to_best_epic(self):
        epic = _item(HierarchyLevel.EPIC, "Auth initiative")
        story = _item(HierarchyLevel.STORY, "OAuth story")
        cls = _FakeClassifier({"Auth initiative": HierarchyLevel.EPIC, "OAuth story": HierarchyLevel.STORY})
        matcher = _FakeMatcher({"OAuth story": [ParentMatch(parent=epic, score=0.75, reasoning="auth")]})
        tree = rebuild_hierarchy([epic, story], classifier=cls, matcher=matcher)
        assert tree.counts["assignments"] == 1
        assert tree.assignments[0].parent.id == "Auth initiative"

    def test_story_without_epic_goes_unparented(self):
        story = _item(HierarchyLevel.STORY, "lonely story")
        cls = _FakeClassifier({"lonely story": HierarchyLevel.STORY})
        matcher = _FakeMatcher({})
        tree = rebuild_hierarchy([story], classifier=cls, matcher=matcher)
        # No epics at all → story in unparented bucket.
        assert story in tree.unparented

    def test_existing_parent_id_respected(self):
        story = _item(HierarchyLevel.STORY, "target")
        # Task already has parent_id set — rebuilder should NOT re-route.
        task = _item(HierarchyLevel.TASK, "already-linked", parent_id="other-story")
        cls = _FakeClassifier({"target": HierarchyLevel.STORY, "already-linked": HierarchyLevel.TASK})
        matcher = _FakeMatcher({"already-linked": [ParentMatch(parent=story, score=0.95)]})
        tree = rebuild_hierarchy([story, task], classifier=cls, matcher=matcher)
        assert tree.assignments == []  # respected existing parent
        assert task not in tree.unparented  # neither re-linked nor orphaned

    def test_full_three_level_tree(self):
        epic = _item(HierarchyLevel.EPIC, "Auth initiative")
        story = _item(HierarchyLevel.STORY, "OAuth story")
        task1 = _item(HierarchyLevel.TASK, "Fix callback")
        task2 = _item(HierarchyLevel.TASK, "Rename var")
        cls = _FakeClassifier(
            {
                "Auth initiative": HierarchyLevel.EPIC,
                "OAuth story": HierarchyLevel.STORY,
                "Fix callback": HierarchyLevel.TASK,
                "Rename var": HierarchyLevel.TASK,
            }
        )
        matcher = _FakeMatcher(
            {
                "OAuth story": [ParentMatch(parent=epic, score=0.8)],
                "Fix callback": [ParentMatch(parent=story, score=0.9)],
                "Rename var": [ParentMatch(parent=story, score=0.3)],  # below threshold
            }
        )
        tree = rebuild_hierarchy([epic, story, task1, task2], classifier=cls, matcher=matcher)
        assert tree.counts == {
            "epics": 1,
            "stories": 1,
            "tasks": 2,
            "assignments": 2,  # story→epic + task1→story
            "unparented": 1,  # task2 below threshold
            "inferred_epics": 0,
        }


# ── apply_tree ───────────────────────────────────────────────────────────────


class TestApply:
    def test_apply_calls_link(self):
        epic = _item(HierarchyLevel.EPIC, "E")
        story = _item(HierarchyLevel.STORY, "S")
        assignment = HierarchyAssignment(child=story, parent=epic, score=0.9)
        tree = HierarchyTree(epics=[epic], stories=[story], assignments=[assignment])
        adapter = MagicMock()
        adapter.link.return_value = True
        count = apply_tree(tree, adapter)
        assert count == 1
        assert assignment.applied is True
        adapter.link.assert_called_once_with(epic, story)

    def test_link_returning_false_leaves_applied_false(self):
        epic = _item(HierarchyLevel.EPIC, "E")
        story = _item(HierarchyLevel.STORY, "S")
        assignment = HierarchyAssignment(child=story, parent=epic, score=0.9)
        tree = HierarchyTree(epics=[epic], stories=[story], assignments=[assignment])
        adapter = MagicMock()
        adapter.link.return_value = False
        count = apply_tree(tree, adapter)
        assert count == 0
        assert assignment.applied is False

    def test_adapter_raising_is_swallowed(self):
        epic = _item(HierarchyLevel.EPIC, "E")
        story = _item(HierarchyLevel.STORY, "S")
        assignment = HierarchyAssignment(child=story, parent=epic, score=0.9)
        tree = HierarchyTree(epics=[epic], stories=[story], assignments=[assignment])
        adapter = MagicMock()
        adapter.link.side_effect = RuntimeError("boom")
        count = apply_tree(tree, adapter)
        assert count == 0
        assert assignment.applied is False

    def test_already_applied_counted_not_recalled(self):
        epic = _item(HierarchyLevel.EPIC, "E")
        story = _item(HierarchyLevel.STORY, "S")
        assignment = HierarchyAssignment(child=story, parent=epic, score=0.9, applied=True)
        tree = HierarchyTree(epics=[epic], stories=[story], assignments=[assignment])
        adapter = MagicMock()
        count = apply_tree(tree, adapter)
        assert count == 1
        adapter.link.assert_not_called()


# ── format_tree ──────────────────────────────────────────────────────────────


class TestFormatTree:
    def test_renders_full_tree(self):
        epic = _item(HierarchyLevel.EPIC, "Auth initiative")
        story = _item(HierarchyLevel.STORY, "OAuth story")
        task = _item(HierarchyLevel.TASK, "Fix callback")
        tree = HierarchyTree(
            epics=[epic],
            stories=[story],
            tasks=[task],
            assignments=[
                HierarchyAssignment(child=story, parent=epic, score=0.8),
                HierarchyAssignment(child=task, parent=story, score=0.9),
            ],
        )
        text = format_tree(tree)
        assert "EPIC: Auth initiative" in text
        assert "STORY: OAuth story" in text
        assert "TASK: Fix callback" in text
        assert "0.80" in text or "0.8" in text
        assert "0.90" in text or "0.9" in text

    def test_unparented_task_footer(self):
        task = _item(HierarchyLevel.TASK, "orphan")
        tree = HierarchyTree(tasks=[task], unparented=[task])
        text = format_tree(tree)
        assert "UNPARENTED TASKS" in text
        assert "orphan" in text

    def test_header_counts(self):
        tree = HierarchyTree(
            epics=[_item(HierarchyLevel.EPIC, "e")],
            stories=[_item(HierarchyLevel.STORY, "s")],
            tasks=[_item(HierarchyLevel.TASK, "t")],
        )
        text = format_tree(tree)
        assert "1 epic" in text
        assert "1 story" in text or "1 stor" in text
        assert "1 task" in text
