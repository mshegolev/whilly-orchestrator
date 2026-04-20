"""Tests for :mod:`whilly.workflow.sync` — the synchronous event→board mover."""

from __future__ import annotations

from whilly.workflow.base import BoardStatus, LifecycleEvent, WorkflowMapping
from whilly.workflow.sync import move_on_event


class _FakeBoard:
    def __init__(self, statuses, moves=None):
        self._statuses = list(statuses)
        self.moves = moves if moves is not None else []
        self._raise = False

    def list_statuses(self):
        if self._raise:
            raise RuntimeError("boom")
        return list(self._statuses)

    def move_item(self, ref, status):
        self.moves.append((ref, status))
        return True


def _mapping(events):
    return WorkflowMapping(board_kind="github_project", board_url="https://example/p/1", events=dict(events))


class TestMoveOnEvent:
    def test_moves_when_event_mapped(self):
        board = _FakeBoard([BoardStatus("o1", "In Progress")])
        ok = move_on_event(board, _mapping({"picked_up": "In Progress"}), "acme/repo#1", "picked_up")
        assert ok is True
        assert board.moves == [("acme/repo#1", BoardStatus("o1", "In Progress"))]

    def test_accepts_lifecycle_enum(self):
        board = _FakeBoard([BoardStatus("o1", "Done")])
        ok = move_on_event(board, _mapping({"done": "Done"}), "acme/r#1", LifecycleEvent.DONE)
        assert ok is True

    def test_no_board_returns_false_silent(self):
        # Disabled integration: caller passes board=None and move_on_event is a no-op.
        assert move_on_event(None, _mapping({"done": "Done"}), "x#1", "done") is False

    def test_no_mapping_returns_false(self):
        board = _FakeBoard([BoardStatus("o1", "Done")])
        assert move_on_event(board, None, "x#1", "done") is False
        assert board.moves == []

    def test_unmapped_event_returns_false(self):
        board = _FakeBoard([BoardStatus("o1", "Done")])
        ok = move_on_event(board, _mapping({"done": "Done"}), "x#1", "failed")
        assert ok is False
        assert board.moves == []

    def test_case_insensitive_status_lookup(self):
        # Mapping says "in progress" but board has "In Progress" — still matches.
        board = _FakeBoard([BoardStatus("o1", "In Progress")])
        ok = move_on_event(board, _mapping({"picked_up": "in progress"}), "x#1", "picked_up")
        assert ok is True

    def test_status_not_on_board_returns_false(self):
        board = _FakeBoard([BoardStatus("o1", "Done")])
        ok = move_on_event(board, _mapping({"picked_up": "Archived"}), "x#1", "picked_up")
        assert ok is False
        assert board.moves == []

    def test_transport_error_returns_false(self):
        board = _FakeBoard([BoardStatus("o1", "Done")])
        board._raise = True
        ok = move_on_event(board, _mapping({"done": "Done"}), "x#1", "done")
        assert ok is False
