"""Tests for :mod:`whilly.workflow.proposer`: mode resolution, interactive
gap resolution, and add/map/skip accounting.
"""

from __future__ import annotations

import pytest

from whilly.workflow.base import BoardStatus, GapReport, WorkflowMapping
from whilly.workflow.proposer import _humanize, _resolve_mode, propose


# ── Board double ──────────────────────────────────────────────────────────────


class _FakeBoard:
    kind = "github_project"

    def __init__(self, url="https://example/p/1", allow_add=True):
        self.url = url
        self.allow_add = allow_add
        self.added: list[str] = []

    def list_statuses(self):  # pragma: no cover — proposer consumes the report
        return []

    def add_status(self, name):
        if not self.allow_add:
            raise NotImplementedError("not supported by this board")
        self.added.append(name)
        return BoardStatus(id=f"opt_{name}", name=name)

    def move_item(self, ref, status):  # pragma: no cover
        return True


# ── Mode resolution ──────────────────────────────────────────────────────────


class TestResolveMode:
    def test_explicit_modes_pass_through(self):
        assert _resolve_mode("interactive") == "interactive"
        assert _resolve_mode("apply") == "apply"
        assert _resolve_mode("report") == "report"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown mode"):
            _resolve_mode("yolo")


class TestHumanize:
    def test_simple(self):
        assert _humanize("picked_up") == "Picked Up"

    def test_already_spaced(self):
        assert _humanize("in_review") == "In Review"


# ── Clean report short-circuits ──────────────────────────────────────────────


class TestCleanReport:
    def test_no_prompts_when_clean(self):
        board = _FakeBoard()
        report = GapReport(
            board_url=board.url,
            board_statuses=[BoardStatus("o1", "Done")],
            matched={"done": BoardStatus("o1", "Done")},
        )
        proposal, mapping = propose(report, board, mode="report")
        assert proposal.cancelled is False
        assert proposal.to_add == []
        assert proposal.to_skip == []
        assert mapping.events["done"] == "Done"


# ── report mode (dry-run) ────────────────────────────────────────────────────


class TestReportMode:
    def test_gaps_become_skips_no_prompts(self):
        board = _FakeBoard()
        statuses = [BoardStatus("o1", "Todo"), BoardStatus("o2", "Done")]
        report = GapReport(
            board_url=board.url,
            board_statuses=list(statuses),
            matched={"ready": statuses[0], "done": statuses[1]},
            missing=["in_review", "failed"],
        )
        proposal, mapping = propose(report, board, mode="report")
        assert set(proposal.to_skip) == {"in_review", "failed"}
        assert proposal.to_add == []
        assert proposal.to_map == {}
        # Mapping has matches but NOT the skipped events.
        assert "in_review" not in mapping.events
        assert "failed" not in mapping.events

    def test_ambiguous_skipped(self):
        board = _FakeBoard()
        statuses = [BoardStatus("o1", "Done"), BoardStatus("o2", "Done (archive)")]
        report = GapReport(
            board_url=board.url,
            board_statuses=list(statuses),
            ambiguous={"done": list(statuses)},
        )
        proposal, mapping = propose(report, board, mode="report")
        assert "done" in proposal.to_skip
        assert "done" not in mapping.events


# ── apply mode (non-interactive add) ─────────────────────────────────────────


class TestApplyMode:
    def test_missing_gets_added(self):
        board = _FakeBoard()
        report = GapReport(
            board_url=board.url,
            board_statuses=[BoardStatus("o1", "Todo")],
            missing=["failed"],
        )
        proposal, mapping = propose(report, board, mode="apply")
        assert proposal.to_add == ["failed"]
        assert board.added == ["Failed"]
        assert mapping.events["failed"] == "Failed"

    def test_missing_skipped_when_add_unsupported(self):
        board = _FakeBoard(allow_add=False)
        report = GapReport(
            board_url=board.url,
            board_statuses=[],
            missing=["failed"],
        )
        proposal, mapping = propose(report, board, mode="apply")
        assert proposal.to_add == []
        assert proposal.to_skip == ["failed"]
        assert "failed" not in mapping.events

    def test_ambiguous_picks_first_candidate(self):
        board = _FakeBoard()
        candidates = [BoardStatus("o1", "Done A"), BoardStatus("o2", "Done B")]
        report = GapReport(
            board_url=board.url,
            board_statuses=list(candidates),
            ambiguous={"done": list(candidates)},
        )
        proposal, mapping = propose(report, board, mode="apply")
        assert proposal.to_map["done"].name == "Done A"
        assert mapping.events["done"] == "Done A"


# ── interactive mode with fake input ─────────────────────────────────────────


def _reader(answers):
    """Yield answers one at a time — AssertionError on over-read so tests fail loudly."""
    it = iter(answers)

    def _read(_prompt):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("proposer asked for more input than the test supplied")

    return _read


class TestInteractive:
    def test_add_new_status(self):
        board = _FakeBoard()
        report = GapReport(
            board_url=board.url,
            board_statuses=[BoardStatus("o1", "Todo")],
            missing=["failed"],
        )
        proposal, mapping = propose(report, board, mode="interactive", reader=_reader(["a"]))
        assert proposal.to_add == ["failed"]
        assert board.added == ["Failed"]
        assert mapping.events["failed"] == "Failed"

    def test_map_to_existing(self):
        board = _FakeBoard()
        todo = BoardStatus("o1", "Todo")
        report = GapReport(
            board_url=board.url,
            board_statuses=[todo],
            missing=["ready"],
        )
        # Answer 'm' then pick #1.
        proposal, mapping = propose(report, board, mode="interactive", reader=_reader(["m", "1"]))
        assert proposal.to_map["ready"].name == "Todo"
        assert mapping.events["ready"] == "Todo"

    def test_skip_keeps_mapping_empty(self):
        board = _FakeBoard()
        report = GapReport(
            board_url=board.url,
            board_statuses=[BoardStatus("o1", "Todo")],
            missing=["failed"],
        )
        proposal, mapping = propose(report, board, mode="interactive", reader=_reader(["s"]))
        assert proposal.to_skip == ["failed"]
        assert "failed" not in mapping.events

    def test_map_choice_zero_is_skip(self):
        board = _FakeBoard()
        report = GapReport(
            board_url=board.url,
            board_statuses=[BoardStatus("o1", "Todo")],
            missing=["ready"],
        )
        proposal, _m = propose(report, board, mode="interactive", reader=_reader(["m", "0"]))
        assert proposal.to_skip == ["ready"]

    def test_add_unsupported_falls_back_to_skip(self):
        board = _FakeBoard(allow_add=False)
        report = GapReport(
            board_url=board.url,
            board_statuses=[],
            missing=["failed"],
        )
        proposal, mapping = propose(report, board, mode="interactive", reader=_reader(["a"]))
        assert proposal.to_skip == ["failed"]
        assert "failed" not in mapping.events

    def test_ambiguous_pick(self):
        board = _FakeBoard()
        candidates = [BoardStatus("o1", "Done A"), BoardStatus("o2", "Done B")]
        report = GapReport(
            board_url=board.url,
            board_statuses=list(candidates),
            ambiguous={"done": list(candidates)},
        )
        # Pick #2.
        proposal, mapping = propose(report, board, mode="interactive", reader=_reader(["2"]))
        assert proposal.to_map["done"].name == "Done B"
        assert mapping.events["done"] == "Done B"


# ── Existing mapping merge ───────────────────────────────────────────────────


class TestMappingMerge:
    def test_preserves_aliases_and_prior_events(self):
        board = _FakeBoard()
        existing = WorkflowMapping(
            board_kind="github_project",
            board_url=board.url,
            events={"triz_challenge": "In Review"},  # custom event previously set
            aliases={"done": ["закрыто"]},
        )
        report = GapReport(
            board_url=board.url,
            board_statuses=[BoardStatus("o1", "Done")],
            matched={"done": BoardStatus("o1", "Done")},
        )
        _prop, mapping = propose(report, board, existing=existing, mode="report")
        assert mapping.events["triz_challenge"] == "In Review"
        assert mapping.aliases["done"] == ["закрыто"]
        assert mapping.events["done"] == "Done"
