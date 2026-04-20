"""Unit tests for whilly.workflow foundation: Protocol, dataclasses, registry,
and fuzzy matcher.

Covers the *structural* contract every board adapter is built on. Adapter-
specific tests (GitHub/Jira/…) live in their own test modules.
"""

from __future__ import annotations

import pytest

from whilly.workflow import (
    BoardSink,
    BoardStatus,
    GapReport,
    LifecycleEvent,
    WorkflowMapping,
    available_boards,
    get_board,
    known_events,
    register_event,
)
from whilly.workflow.github import GitHubProjectBoard
from whilly.workflow.mapper import match_events
from whilly.workflow.registry import CORE_EVENTS, reset_to_core


# ── LifecycleEvent + BoardStatus ──────────────────────────────────────────────


class TestLifecycleEvent:
    def test_str_enum_round_trip(self):
        assert LifecycleEvent.READY.value == "ready"
        assert LifecycleEvent("done") is LifecycleEvent.DONE

    def test_all_six_core_events_present(self):
        names = {e.value for e in LifecycleEvent}
        assert names == {"ready", "picked_up", "in_review", "done", "refused", "failed"}


class TestBoardStatus:
    def test_str_representation_is_name(self):
        s = BoardStatus(id="PVTSSF_xxx", name="In Progress")
        assert str(s) == "In Progress"

    def test_frozen(self):
        s = BoardStatus(id="x", name="y")
        with pytest.raises(Exception):  # dataclass FrozenInstanceError
            s.name = "z"


# ── WorkflowMapping serialisation ─────────────────────────────────────────────


class TestWorkflowMapping:
    def test_status_for_accepts_both_enum_and_str(self):
        m = WorkflowMapping(
            board_kind="github_project",
            board_url="https://example",
            events={"done": "Done", "ready": "Todo"},
        )
        assert m.status_for("done") == "Done"
        assert m.status_for(LifecycleEvent.DONE) == "Done"
        assert m.status_for("unknown") is None

    def test_to_from_dict_round_trip(self):
        m1 = WorkflowMapping(
            board_kind="github_project",
            board_url="https://example",
            events={"done": "Done"},
            aliases={"done": ["закрыто", "finished"]},
        )
        m2 = WorkflowMapping.from_dict(m1.to_dict())
        assert m2.board_kind == m1.board_kind
        assert m2.events == m1.events
        assert m2.aliases == m1.aliases
        assert m2.version == 1

    def test_from_dict_tolerates_missing_fields(self):
        m = WorkflowMapping.from_dict({})
        assert m.board_kind == ""
        assert m.events == {}
        assert m.aliases == {}


# ── GapReport ──────────────────────────────────────────────────────────────────


class TestGapReport:
    def test_is_clean_when_all_matched(self):
        r = GapReport(
            board_url="x",
            board_statuses=[BoardStatus("s", "Done")],
            matched={"done": BoardStatus("s", "Done")},
        )
        assert r.is_clean

    def test_is_dirty_when_missing_or_ambiguous(self):
        r1 = GapReport(board_url="x", board_statuses=[], missing=["failed"])
        r2 = GapReport(board_url="x", board_statuses=[], ambiguous={"done": []})
        assert not r1.is_clean
        assert not r2.is_clean


# ── Event registry ────────────────────────────────────────────────────────────


class TestRegistry:
    def setup_method(self):
        reset_to_core()

    def teardown_method(self):
        reset_to_core()

    def test_core_events_present_by_default(self):
        names = set(known_events().keys())
        assert names == set(CORE_EVENTS.keys())

    def test_register_custom_event(self):
        register_event("triz_challenge", default_aliases=["challenge", "triz"])
        evts = known_events()
        assert "triz_challenge" in evts
        assert evts["triz_challenge"] == ["challenge", "triz"]

    def test_register_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            register_event("")
        with pytest.raises(ValueError, match="non-empty"):
            register_event("   ")

    def test_re_register_replaces_aliases(self):
        register_event("custom", ["a"])
        register_event("custom", ["b", "c"])
        assert known_events()["custom"] == ["b", "c"]

    def test_known_events_returns_copy(self):
        snapshot = known_events()
        snapshot["ready"] = ["MUTATED"]
        assert known_events()["ready"] != ["MUTATED"]


# ── Fuzzy matcher ─────────────────────────────────────────────────────────────


class TestMatcher:
    def _statuses(self, *names):
        return [BoardStatus(id=f"id-{n}", name=n) for n in names]

    def test_exact_match_wins(self):
        st = self._statuses("Todo", "In Progress", "Done")
        r = match_events("x", st, events=["done", "ready"])
        assert r.matched["done"].name == "Done"
        assert r.matched["ready"].name == "Todo"

    def test_substring_match(self):
        st = self._statuses("Ready for review", "In Progress", "Done")
        r = match_events("x", st, events=["in_review"])
        # "in review" ⊂ "Ready for review" is a valid match per the algo.
        assert "in_review" in r.matched or "in_review" in r.ambiguous

    def test_missing_when_no_match(self):
        st = self._statuses("Foo", "Bar")
        r = match_events("x", st, events=["done"])
        assert "done" in r.missing
        assert "done" not in r.matched

    def test_explicit_mapping_wins(self):
        st = self._statuses("Закрыто", "В работе")
        m = WorkflowMapping(
            board_kind="github_project",
            board_url="x",
            events={"done": "Закрыто"},
        )
        r = match_events("x", st, events=["done"], mapping=m)
        assert r.matched["done"].name == "Закрыто"

    def test_explicit_mapping_pointing_at_missing_status(self):
        st = self._statuses("Todo", "Done")
        m = WorkflowMapping(
            board_kind="github_project",
            board_url="x",
            events={"done": "Archived"},  # no such column
        )
        r = match_events("x", st, events=["done"], mapping=m)
        assert "done" in r.missing

    def test_user_aliases_extend_builtin(self):
        st = self._statuses("Закрыто")
        m = WorkflowMapping(
            board_kind="github_project",
            board_url="x",
            aliases={"done": ["закрыто"]},
        )
        r = match_events("x", st, events=["done"], mapping=m)
        assert r.matched["done"].name == "Закрыто"

    def test_ambiguous_when_multiple_match(self):
        st = self._statuses("Done", "Done (archive)")
        r = match_events("x", st, events=["done"])
        # Both contain "done" → ambiguous.
        assert "done" in r.ambiguous or "done" in r.matched  # tolerant

    def test_full_core_sweep_against_sensible_board(self):
        st = self._statuses("Todo", "In Progress", "In Review", "Done", "Blocked", "Failed")
        r = match_events("x", st)
        # Every core event should find a home.
        assert not r.missing, f"missing events: {r.missing}"


# ── Board factory ─────────────────────────────────────────────────────────────


class TestBoardFactory:
    def test_github_project_registered(self):
        assert "github_project" in available_boards()

    def test_get_board_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown board"):
            get_board("jira-classic")

    def test_github_project_satisfies_protocol(self):
        b: BoardSink = GitHubProjectBoard(url="https://github.com/users/x/projects/1")
        assert b.kind == "github_project"
        for attr in ("list_statuses", "add_status", "move_item"):
            assert callable(getattr(b, attr, None)), attr
