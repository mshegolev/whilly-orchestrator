"""Tests for :mod:`whilly.classifier.epic_inferrer` + integration with
the rebuilder + apply_tree materialisation path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from whilly.agents import ClaudeBackend, OpenCodeBackend
from whilly.agents.base import AgentResult, AgentUsage
from whilly.classifier import (
    HierarchyTree,
    InferredEpic,
    apply_tree,
    infer_epics,
    rebuild_hierarchy,
)
from whilly.classifier.base import ClassificationResult
from whilly.hierarchy.base import HierarchyLevel, WorkItem


# ── Helpers ──────────────────────────────────────────────────────────────────


def _story(id_, title, body=""):
    return WorkItem(id=id_, level=HierarchyLevel.STORY, title=title, body=body)


def _patch_llm(monkeypatch, response):
    def fake(self, prompt, model=None, timeout=None, cwd=None):
        return AgentResult(result_text=response, exit_code=0, usage=AgentUsage())

    monkeypatch.setattr(ClaudeBackend, "run", fake)
    monkeypatch.setattr(OpenCodeBackend, "run", fake)
    monkeypatch.delenv("WHILLY_AGENT_BACKEND", raising=False)


# ── InferredEpic dataclass ──────────────────────────────────────────────────


class TestInferredEpic:
    def test_confidence_clamps(self):
        e = InferredEpic(title="x", confidence=1.5)
        assert e.confidence == 1.0

    def test_high_confidence_threshold(self):
        assert InferredEpic(title="x", confidence=0.7).is_high_confidence
        assert not InferredEpic(title="x", confidence=0.4).is_high_confidence


# ── infer_epics ─────────────────────────────────────────────────────────────


class TestInferEpics:
    def test_below_min_stories_returns_empty(self, monkeypatch):
        # Not patching LLM — if infer_epics called it, test would explode
        # because there's no backend installed on the test runner.
        result = infer_epics([_story("s1", "alone")])
        assert result == []

    def test_happy_path_groups_stories(self, monkeypatch):
        stories = [
            _story("s1", "OAuth Google", "google login broken"),
            _story("s2", "OAuth GitHub", "github login flow"),
            _story("s3", "Metrics dashboard", "p95 latency chart"),
        ]
        _patch_llm(
            monkeypatch,
            '{"epics":['
            '{"title":"Auth rebuild","body":"Unify OAuth flows.","child_story_ids":["s1","s2"],"confidence":0.82,"reasoning":"both OAuth"},'
            '{"title":"Observability","body":"Metrics + traces.","child_story_ids":["s3"],"confidence":0.5,"reasoning":"metrics-focused"}'
            "]}",
        )
        epics = infer_epics(stories, min_stories_per_epic=2)
        # Only the OAuth cluster qualifies (singleton "Observability" filtered out).
        assert len(epics) == 1
        assert epics[0].title == "Auth rebuild"
        assert set(epics[0].child_story_ids) == {"s1", "s2"}

    def test_respects_max_epics(self, monkeypatch):
        stories = [_story(f"s{i}", f"t{i}") for i in range(6)]
        _patch_llm(
            monkeypatch,
            '{"epics":['
            '{"title":"A","body":"","child_story_ids":["s0","s1"],"confidence":0.8,"reasoning":""},'
            '{"title":"B","body":"","child_story_ids":["s2","s3"],"confidence":0.8,"reasoning":""},'
            '{"title":"C","body":"","child_story_ids":["s4","s5"],"confidence":0.8,"reasoning":""}'
            "]}",
        )
        epics = infer_epics(stories, max_epics=2)
        assert len(epics) == 2

    def test_unknown_child_ids_filtered(self, monkeypatch):
        stories = [_story("s1", "t1"), _story("s2", "t2")]
        _patch_llm(
            monkeypatch,
            '{"epics":[{"title":"X","body":"","child_story_ids":["s1","s2","ghost"],"confidence":0.8,"reasoning":""}]}',
        )
        epics = infer_epics(stories, min_stories_per_epic=2)
        assert epics[0].child_story_ids == ["s1", "s2"]

    def test_missing_title_skipped(self, monkeypatch):
        stories = [_story("s1", "t1"), _story("s2", "t2")]
        _patch_llm(
            monkeypatch,
            '{"epics":[{"title":"","body":"","child_story_ids":["s1","s2"],"confidence":0.9,"reasoning":""}]}',
        )
        assert infer_epics(stories, min_stories_per_epic=2) == []

    def test_llm_exception_returns_empty(self, monkeypatch):
        def boom(self, prompt, model=None, timeout=None, cwd=None):
            raise RuntimeError("transport exploded")

        monkeypatch.setattr(ClaudeBackend, "run", boom)
        monkeypatch.delenv("WHILLY_AGENT_BACKEND", raising=False)
        stories = [_story("s1", "t1"), _story("s2", "t2")]
        assert infer_epics(stories, min_stories_per_epic=2) == []

    def test_unparseable_returns_empty(self, monkeypatch):
        _patch_llm(monkeypatch, "definitely not json")
        stories = [_story("s1", "t1"), _story("s2", "t2")]
        assert infer_epics(stories, min_stories_per_epic=2) == []


# ── rebuild_hierarchy(infer_missing_epics=True) ─────────────────────────────


class _FakeClassifier:
    kind = "fake"

    def __init__(self, by_id):
        self._by_id = by_id

    def classify(self, title, body):
        level = self._by_id.get(title, HierarchyLevel.TASK)
        return ClassificationResult(level=level, confidence=0.9)


class _FakeMatcher:
    kind = "fake"

    def find_matches(self, candidate_title, candidate_body, candidates, *, max_matches=3):
        return []


class TestRebuildWithInference:
    def test_infer_epics_produces_proposals(self, monkeypatch):
        # Two orphan stories, no epics → inference should kick in.
        stories = [_story("oauth-google", "OAuth Google"), _story("oauth-gh", "OAuth GitHub")]
        cls = _FakeClassifier({"OAuth Google": HierarchyLevel.STORY, "OAuth GitHub": HierarchyLevel.STORY})
        matcher = _FakeMatcher()
        _patch_llm(
            monkeypatch,
            '{"epics":[{"title":"Auth rebuild","body":"OAuth unify","child_story_ids":["oauth-google","oauth-gh"],"confidence":0.82,"reasoning":"both OAuth"}]}',
        )
        tree = rebuild_hierarchy(stories, classifier=cls, matcher=matcher, infer_missing_epics=True)
        assert len(tree.inferred_epics) == 1
        assert tree.inferred_epics[0].title == "Auth rebuild"
        assert tree.counts["inferred_epics"] == 1

    def test_infer_skipped_when_flag_off(self):
        stories = [_story("s1", "s1"), _story("s2", "s2")]
        cls = _FakeClassifier({"s1": HierarchyLevel.STORY, "s2": HierarchyLevel.STORY})
        tree = rebuild_hierarchy(stories, classifier=cls, matcher=_FakeMatcher(), infer_missing_epics=False)
        assert tree.inferred_epics == []


# ── apply_tree with inferred epics ──────────────────────────────────────────


class TestApplyWithInference:
    def test_materialises_epic_and_links_stories(self):
        s1 = _story("s1", "t1")
        s2 = _story("s2", "t2")
        proposal = InferredEpic(
            title="New Epic",
            body="body",
            child_story_ids=["s1", "s2"],
            confidence=0.8,
        )
        tree = HierarchyTree(stories=[s1, s2], inferred_epics=[proposal])

        adapter = MagicMock()
        # create_at_level returns a new epic WorkItem.
        new_epic = WorkItem(id="EPIC_new", level=HierarchyLevel.EPIC, title="New Epic")
        adapter.create_at_level.return_value = new_epic
        adapter.link.return_value = True

        count = apply_tree(tree, adapter)
        assert count == 2  # two story-to-epic links
        assert proposal.applied is True
        adapter.create_at_level.assert_called_once_with(HierarchyLevel.EPIC, "New Epic", "body")
        assert adapter.link.call_count == 2

    def test_low_confidence_inferred_epic_skipped(self):
        s1 = _story("s1", "t1")
        proposal = InferredEpic(title="Shaky", confidence=0.3, child_story_ids=["s1"])
        tree = HierarchyTree(stories=[s1], inferred_epics=[proposal])

        adapter = MagicMock()
        adapter.link.return_value = True

        apply_tree(tree, adapter, inferred_confidence_threshold=0.5)
        assert proposal.applied is False
        adapter.create_at_level.assert_not_called()

    def test_create_at_level_exception_skips_epic(self):
        s1 = _story("s1", "t1")
        s2 = _story("s2", "t2")
        proposal = InferredEpic(
            title="New Epic",
            child_story_ids=["s1", "s2"],
            confidence=0.8,
        )
        tree = HierarchyTree(stories=[s1, s2], inferred_epics=[proposal])

        adapter = MagicMock()
        adapter.create_at_level.side_effect = RuntimeError("permissions")
        adapter.link.return_value = True

        count = apply_tree(tree, adapter)
        assert count == 0
        assert proposal.applied is False
        # link never called because epic creation failed.
        adapter.link.assert_not_called()

    def test_unknown_child_id_logged_not_fatal(self):
        s1 = _story("s1", "t1")
        proposal = InferredEpic(title="E", child_story_ids=["s1", "ghost"], confidence=0.8)
        tree = HierarchyTree(stories=[s1], inferred_epics=[proposal])

        new_epic = WorkItem(id="EPIC_e", level=HierarchyLevel.EPIC, title="E")
        adapter = MagicMock()
        adapter.create_at_level.return_value = new_epic
        adapter.link.return_value = True

        count = apply_tree(tree, adapter)
        # Only the known child linked; ghost skipped, not fatal.
        assert count == 1
        assert proposal.applied is True
        assert adapter.link.call_count == 1

    def test_materialise_flag_off_leaves_proposals(self):
        s1 = _story("s1", "t1")
        s2 = _story("s2", "t2")
        proposal = InferredEpic(title="E", child_story_ids=["s1", "s2"], confidence=0.9)
        tree = HierarchyTree(stories=[s1, s2], inferred_epics=[proposal])

        adapter = MagicMock()
        apply_tree(tree, adapter, materialise_inferred=False)
        adapter.create_at_level.assert_not_called()
        assert proposal.applied is False
