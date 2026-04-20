"""Tests for :mod:`whilly.classifier` — heuristic, LLM fallback paths,
matcher, router decisions. No network — LLM calls are stubbed at the
agent-backend boundary.
"""

from __future__ import annotations

import pytest

from whilly.agents import ClaudeBackend, OpenCodeBackend
from whilly.agents.base import AgentResult, AgentUsage
from whilly.classifier import (
    ClassificationResult,
    HeuristicClassifier,
    LLMClassifier,
    LLMParentMatcher,
    NoopParentMatcher,
    ParentMatch,
    Router,
    RoutingAction,
    RoutingDecision,
    format_decision,
)
from whilly.hierarchy.base import HierarchyLevel, WorkItem


# ── Data classes ─────────────────────────────────────────────────────────────


class TestClassificationResult:
    def test_defaults_and_clamping(self):
        r = ClassificationResult(
            level=HierarchyLevel.TASK,
            confidence=1.5,  # clamped to 1.0
            complexity_score=42,  # clamped to 10
            estimated_children=-3,  # clamped to 0
        )
        assert r.confidence == 1.0
        assert r.complexity_score == 10
        assert r.estimated_children == 0

    def test_level_string_coerces(self):
        r = ClassificationResult(level="story")  # type: ignore[arg-type]
        assert r.level is HierarchyLevel.STORY

    def test_high_confidence_threshold(self):
        assert ClassificationResult(level=HierarchyLevel.TASK, confidence=0.8).is_high_confidence
        assert not ClassificationResult(level=HierarchyLevel.TASK, confidence=0.5).is_high_confidence


class TestRoutingDecision:
    def test_action_coerces_from_string(self):
        c = ClassificationResult(level=HierarchyLevel.TASK)
        d = RoutingDecision(action="link_as_child", classification=c, title="t", body="b")  # type: ignore[arg-type]
        assert d.action is RoutingAction.LINK_AS_CHILD


# ── Heuristic classifier ─────────────────────────────────────────────────────


class TestHeuristic:
    def test_task_keywords(self):
        c = HeuristicClassifier().classify("Fix typo in README.md", "Line 42 has 'recieve' → 'receive'.")
        assert c.level is HierarchyLevel.TASK
        assert c.confidence < 0.65

    def test_story_keywords(self):
        c = HeuristicClassifier().classify(
            "Add OAuth login flow",
            "As a user I want to sign in with Google. Support for new OAuth integration.",
        )
        assert c.level is HierarchyLevel.STORY

    def test_epic_keywords_and_length(self):
        body = (
            "North-star initiative to replatform the authentication layer. "
            "KPI: reduce p95 login latency by 40%. Strategic for Q3 roadmap. " * 5
        )
        c = HeuristicClassifier().classify("Replatform auth", body)
        assert c.level is HierarchyLevel.EPIC

    def test_too_short_flagged(self):
        c = HeuristicClassifier().classify("x", "")
        assert "below-length-threshold" in c.flags
        assert c.confidence <= 0.3

    def test_no_keywords_low_confidence(self):
        c = HeuristicClassifier().classify(
            "Something unclear",
            "qwerty asdf zxcv nothing matches here at all.",
        )
        assert "no-keywords-matched" in c.flags


# ── LLM classifier — mock the backend ────────────────────────────────────────


class TestLLMClassifier:
    def _patch_backend(self, monkeypatch, response_text, exit_code=0):
        def fake_run(self, prompt, model=None, timeout=None, cwd=None):
            return AgentResult(
                result_text=response_text,
                usage=AgentUsage(cost_usd=0.0001),
                exit_code=exit_code,
            )

        monkeypatch.setattr(ClaudeBackend, "run", fake_run)
        monkeypatch.setattr(OpenCodeBackend, "run", fake_run)
        monkeypatch.delenv("WHILLY_AGENT_BACKEND", raising=False)

    def test_happy_json(self, monkeypatch):
        self._patch_backend(
            monkeypatch,
            '{"level":"story","confidence":0.82,"reasoning":"clear feature",'
            '"estimated_children":4,"complexity_score":6,"flags":[]}',
        )
        c = LLMClassifier().classify("Add CSV export", "As user I want CSV export.")
        assert c.level is HierarchyLevel.STORY
        assert c.confidence == pytest.approx(0.82)
        assert c.estimated_children == 4
        assert c.complexity_score == 6

    def test_markdown_fences_tolerated(self, monkeypatch):
        self._patch_backend(
            monkeypatch,
            "```json\n"
            '{"level":"task","confidence":0.9,"reasoning":"ok","estimated_children":0,"complexity_score":2,"flags":[]}'
            "\n```",
        )
        c = LLMClassifier().classify("Bump pytest", "pytest 7 → 8")
        assert c.level is HierarchyLevel.TASK

    def test_unparseable_falls_back_to_heuristic(self, monkeypatch):
        self._patch_backend(monkeypatch, "this is not json at all")
        c = LLMClassifier().classify("Add OAuth login", "As a user I want to sign in.")
        assert any(f.startswith("llm-") for f in c.flags)

    def test_bad_level_falls_back(self, monkeypatch):
        self._patch_backend(
            monkeypatch,
            '{"level":"initiative","confidence":0.9,"reasoning":"x","estimated_children":3,"complexity_score":7,"flags":[]}',
        )
        c = LLMClassifier().classify("Replatform auth", "Big work")
        assert "llm-bad-level" in c.flags

    def test_non_zero_exit_falls_back(self, monkeypatch):
        self._patch_backend(monkeypatch, "", exit_code=1)
        c = LLMClassifier().classify("Add feature", "body")
        assert any(f.startswith("llm-") for f in c.flags)

    def test_backend_exception_falls_back(self, monkeypatch):
        def boom(self, prompt, model=None, timeout=None, cwd=None):
            raise RuntimeError("transport exploded")

        monkeypatch.setattr(ClaudeBackend, "run", boom)
        monkeypatch.delenv("WHILLY_AGENT_BACKEND", raising=False)
        c = LLMClassifier().classify("Add feature", "body that mentions new feature and integration")
        assert any(f.startswith("llm-failed:") for f in c.flags)


# ── Matcher ──────────────────────────────────────────────────────────────────


def _make_items(*titles):
    return [
        WorkItem(
            id=f"id-{i}",
            level=HierarchyLevel.STORY,
            title=t,
            body="",
        )
        for i, t in enumerate(titles, 1)
    ]


class TestLLMMatcher:
    def _patch(self, monkeypatch, response):
        def fake(self, prompt, model=None, timeout=None, cwd=None):
            return AgentResult(result_text=response, exit_code=0, usage=AgentUsage())

        monkeypatch.setattr(ClaudeBackend, "run", fake)
        monkeypatch.delenv("WHILLY_AGENT_BACKEND", raising=False)

    def test_empty_candidate_list_is_empty(self):
        assert LLMParentMatcher().find_matches("t", "b", []) == []

    def test_ordered_by_score(self, monkeypatch):
        # id-1 = "Irrelevant" with low score, id-2 = "OAuth Story" with high score.
        # Return them in the wrong order to check matcher re-sorts.
        self._patch(
            monkeypatch,
            '{"matches":['
            '{"id":"id-1","score":0.3,"reasoning":"meh"},'
            '{"id":"id-2","score":0.9,"reasoning":"perfect fit"}]}',
        )
        items = _make_items("Irrelevant", "OAuth Story")
        matches = LLMParentMatcher().find_matches("Sign-in bug", "Google OAuth broken", items)
        assert [m.parent.title for m in matches] == ["OAuth Story", "Irrelevant"]
        assert matches[0].score == pytest.approx(0.9)

    def test_unknown_id_filtered(self, monkeypatch):
        self._patch(
            monkeypatch,
            '{"matches":[{"id":"id-42","score":0.9,"reasoning":"x"},{"id":"id-1","score":0.7,"reasoning":"y"}]}',
        )
        items = _make_items("Real")
        matches = LLMParentMatcher().find_matches("t", "b", items)
        assert len(matches) == 1
        assert matches[0].parent.id == "id-1"

    def test_unparseable_returns_empty(self, monkeypatch):
        self._patch(monkeypatch, "no json here")
        items = _make_items("Story")
        assert LLMParentMatcher().find_matches("t", "b", items) == []


class TestNoopMatcher:
    def test_always_empty(self):
        items = _make_items("x", "y")
        assert NoopParentMatcher().find_matches("a", "b", items) == []


# ── Router ───────────────────────────────────────────────────────────────────


class _FakeClassifier:
    kind = "fake"

    def __init__(self, result):
        self.result = result

    def classify(self, title, body):
        return self.result


class _FakeMatcher:
    kind = "fake"

    def __init__(self, matches):
        self.matches = matches

    def find_matches(self, candidate_title, candidate_body, candidates, *, max_matches=3):
        return self.matches[:max_matches]


class _FakeAdapter:
    kind = "fake"

    def __init__(self, candidates):
        self._candidates = candidates

    def list_at_level(self, level, *, parent=None, label=None):
        return list(self._candidates)

    def get(self, id):
        return None

    def promote(self, item):
        return item

    def create_child(self, parent, title, body="", *, labels=None):
        self.last_created = {"parent": parent, "title": title, "body": body}
        return WorkItem(id="new", level=HierarchyLevel.TASK, title=title, parent_id=parent.id)

    def link(self, parent, child):
        return True


class TestRouter:
    def _classifier(self, level=HierarchyLevel.TASK, confidence=0.85, flags=()):
        return _FakeClassifier(ClassificationResult(level=level, confidence=confidence, flags=list(flags)))

    def test_rejects_on_below_length_flag(self):
        router = Router(
            classifier=self._classifier(flags=["below-length-threshold"]),
            matcher=NoopParentMatcher(),
        )
        decision = router.route_text("x", "", _FakeAdapter([]))
        assert decision.action is RoutingAction.REJECT

    def test_epic_creates_orphan(self):
        router = Router(
            classifier=self._classifier(level=HierarchyLevel.EPIC),
            matcher=NoopParentMatcher(),
        )
        decision = router.route_text("Big thing", "body", _FakeAdapter([]))
        assert decision.action is RoutingAction.CREATE_ORPHAN
        assert decision.target_parent is None

    def test_no_candidates_creates_orphan(self):
        router = Router(
            classifier=self._classifier(level=HierarchyLevel.TASK),
            matcher=_FakeMatcher([]),
        )
        decision = router.route_text("fix", "tiny task", _FakeAdapter([]))
        assert decision.action is RoutingAction.CREATE_ORPHAN

    def test_below_match_threshold_creates_orphan(self):
        candidates = _make_items("Unrelated Story")
        router = Router(
            classifier=self._classifier(level=HierarchyLevel.TASK),
            matcher=_FakeMatcher([ParentMatch(parent=candidates[0], score=0.2)]),
            match_threshold=0.55,
        )
        decision = router.route_text("t", "b", _FakeAdapter(candidates))
        assert decision.action is RoutingAction.CREATE_ORPHAN
        assert decision.parent_confidence == pytest.approx(0.2)

    def test_above_threshold_links_as_child(self):
        candidates = _make_items("OAuth Story")
        router = Router(
            classifier=self._classifier(level=HierarchyLevel.TASK),
            matcher=_FakeMatcher([ParentMatch(parent=candidates[0], score=0.9, reasoning="fits")]),
        )
        decision = router.route_text("Fix sign-in", "OAuth callback broken", _FakeAdapter(candidates))
        assert decision.action is RoutingAction.LINK_AS_CHILD
        assert decision.target_parent is candidates[0]
        assert decision.parent_confidence == pytest.approx(0.9)

    def test_adapter_error_creates_orphan(self):
        class Boom(_FakeAdapter):
            def list_at_level(self, level, *, parent=None, label=None):
                raise RuntimeError("no network")

        router = Router(classifier=self._classifier(level=HierarchyLevel.TASK), matcher=NoopParentMatcher())
        decision = router.route_text("x", "y", Boom([]))
        assert decision.action is RoutingAction.CREATE_ORPHAN

    def test_apply_link_invokes_create_child(self):
        candidates = _make_items("Story")
        adapter = _FakeAdapter(candidates)
        router = Router(
            classifier=self._classifier(level=HierarchyLevel.TASK),
            matcher=_FakeMatcher([ParentMatch(parent=candidates[0], score=0.9)]),
        )
        decision = router.route_text("Fix", "body", adapter)
        router.apply(decision, adapter)
        assert decision.applied is True
        assert adapter.last_created["parent"] is candidates[0]
        assert adapter.last_created["title"] == "Fix"

    def test_apply_is_idempotent(self):
        candidates = _make_items("Story")
        adapter = _FakeAdapter(candidates)
        router = Router(
            classifier=self._classifier(level=HierarchyLevel.TASK),
            matcher=_FakeMatcher([ParentMatch(parent=candidates[0], score=0.9)]),
        )
        decision = router.route_text("Fix", "body", adapter)
        router.apply(decision, adapter)
        adapter.last_created = {"parent": None}  # reset sentinel
        router.apply(decision, adapter)
        assert adapter.last_created == {"parent": None}  # no double-create


# ── format_decision ──────────────────────────────────────────────────────────


class TestFormatDecision:
    def test_rejection_rendered(self):
        c = ClassificationResult(level=HierarchyLevel.TASK, flags=["below-length-threshold"])
        d = RoutingDecision(action=RoutingAction.REJECT, classification=c, title="x", body="y")
        text = format_decision(d)
        assert "reject" in text.lower()
        assert "below-length-threshold" in text

    def test_link_includes_parent_and_score(self):
        parent = WorkItem(id="p1", level=HierarchyLevel.STORY, title="OAuth rebuild")
        c = ClassificationResult(level=HierarchyLevel.TASK, confidence=0.9, reasoning="fits OAuth")
        d = RoutingDecision(
            action=RoutingAction.LINK_AS_CHILD,
            classification=c,
            title="Fix callback",
            body="",
            target_parent=parent,
            parent_confidence=0.87,
        )
        text = format_decision(d)
        assert "OAuth rebuild" in text
        assert "0.87" in text
        assert "fits OAuth" in text
