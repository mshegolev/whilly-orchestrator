"""Tests for :mod:`whilly.workflow.analyzer`: orchestration around a mocked
BoardSink, mapping file I/O, and report formatting.
"""

from __future__ import annotations

from pathlib import Path


from whilly.workflow.analyzer import (
    DEFAULT_MAPPING_PATH,
    analyze,
    format_report,
    load_mapping,
    save_mapping,
)
from whilly.workflow.base import BoardStatus, WorkflowMapping


class _FakeBoard:
    """Minimal BoardSink stub — returns canned statuses."""

    kind = "github_project"

    def __init__(self, statuses, url="https://example/project/1"):
        self._statuses = statuses
        self.url = url

    def list_statuses(self):
        return list(self._statuses)

    def add_status(self, name):  # pragma: no cover — analyzer never mutates
        raise NotImplementedError

    def move_item(self, ref, status):  # pragma: no cover
        return False


# ── analyze() ─────────────────────────────────────────────────────────────────


class TestAnalyze:
    def test_clean_board(self):
        board = _FakeBoard(
            [
                BoardStatus("o1", "Todo"),
                BoardStatus("o2", "In Progress"),
                BoardStatus("o3", "In Review"),
                BoardStatus("o4", "Done"),
                BoardStatus("o5", "Blocked"),
                BoardStatus("o6", "Failed"),
            ]
        )
        report = analyze(board)
        assert report.is_clean
        assert len(report.matched) >= 6
        assert report.missing == []

    def test_missing_events_flagged(self):
        board = _FakeBoard(
            [
                BoardStatus("o1", "Todo"),
                BoardStatus("o2", "In Progress"),
                BoardStatus("o3", "Done"),
            ]
        )
        report = analyze(board)
        # Missing one or more of: in_review, refused, failed.
        assert not report.is_clean
        missing = set(report.missing) | set(report.ambiguous.keys())
        assert any(evt in missing for evt in ("in_review", "refused", "failed"))

    def test_explicit_mapping_overrides_fuzzy(self):
        board = _FakeBoard([BoardStatus("o1", "Закрыто"), BoardStatus("o2", "В работе")])
        mapping = WorkflowMapping(
            board_kind="github_project",
            board_url=board.url,
            events={"done": "Закрыто", "picked_up": "В работе"},
        )
        report = analyze(board, mapping=mapping, events=["done", "picked_up"])
        assert report.matched["done"].name == "Закрыто"
        assert report.matched["picked_up"].name == "В работе"


# ── Mapping file I/O ─────────────────────────────────────────────────────────


class TestMappingIO:
    def test_load_missing_returns_none(self, tmp_path):
        path = tmp_path / "nope.json"
        assert load_mapping(path) is None

    def test_save_then_load_round_trip(self, tmp_path):
        path = tmp_path / ".whilly" / "workflow.json"
        original = WorkflowMapping(
            board_kind="github_project",
            board_url="https://example/p/1",
            events={"done": "Done"},
            aliases={"done": ["closed"]},
        )
        save_mapping(original, path)
        assert path.is_file()
        loaded = load_mapping(path)
        assert loaded is not None
        assert loaded.board_kind == original.board_kind
        assert loaded.events == original.events
        assert loaded.aliases == original.aliases

    def test_load_corrupted_returns_none(self, tmp_path):
        path = tmp_path / "workflow.json"
        path.write_text("{not json")
        assert load_mapping(path) is None

    def test_default_path_is_gitignorable(self):
        # Sanity — we picked .whilly/workflow.json deliberately.
        assert DEFAULT_MAPPING_PATH == Path(".whilly") / "workflow.json"


# ── format_report ────────────────────────────────────────────────────────────


class TestFormatReport:
    def test_clean_report_mentions_no_gaps(self):
        board = _FakeBoard(
            [
                BoardStatus("o1", "Todo"),
                BoardStatus("o2", "In Progress"),
                BoardStatus("o3", "In Review"),
                BoardStatus("o4", "Done"),
                BoardStatus("o5", "Blocked"),
                BoardStatus("o6", "Failed"),
            ]
        )
        report = analyze(board)
        text = format_report(report)
        assert "Clean — no gaps." in text
        assert "Project:" in text
        assert "6 options" in text

    def test_dirty_report_lists_gaps(self):
        board = _FakeBoard(
            [
                BoardStatus("o1", "Todo"),
                BoardStatus("o2", "Done"),
            ]
        )
        report = analyze(board)
        text = format_report(report)
        assert "✗ missing" in text
        assert "Gaps:" in text or "missing" in text

    def test_custom_title_rendered(self):
        board = _FakeBoard([BoardStatus("o1", "Done")])
        report = analyze(board)
        text = format_report(report, title="Workflow analysis")
        assert text.startswith("Workflow analysis")
