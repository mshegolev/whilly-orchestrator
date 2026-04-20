"""Tests for the Whilly Wiggum TRIZ+PRD self-hosting pipeline.

Focus: *orchestration* — every external effect (Decision Gate LLM, TRIZ
LLM, PRD generation, whilly execution, quality gate, PR opening, board
movement) is monkey-patched. What we verify is:

* stage ordering (issue → gate → challenge → PRD → tasks → execute → gate → PR),
* short-circuit on refuse / reject / failed execution / failed gate,
* workflow event emission with the right event names so ADR-015 Syncer
  will light up without pipeline changes,
* body builder includes Challenge verdict + PRD path + tasks count.

No network, no subprocess, no LLM. Runs in under a second.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from whilly.decision_gate import PROCEED, REFUSE
from whilly.workflow.base import BoardStatus, WorkflowMapping


# ── Load the script as a module ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def pipeline():
    """Import the pipeline module from scripts/ without executing main()."""
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    spec = importlib.util.spec_from_file_location(
        "whilly_e2e_triz_prd",
        repo_root / "scripts" / "whilly_e2e_triz_prd.py",
    )
    module = importlib.util.module_from_spec(spec)
    # Need the module in sys.modules for dataclass() to resolve its __module__.
    sys.modules["whilly_e2e_triz_prd"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("whilly_e2e_triz_prd", None)


def _issue(pipeline, number=42, labels=None):
    return pipeline.IssueTask(
        id=f"GH-{number}",
        number=number,
        title=f"Issue {number}",
        body="Do the thing and make it good.",
        url=f"https://github.com/acme/repo/issues/{number}",
        labels=labels or ["whilly:ready"],
    )


# ── Build PR body ────────────────────────────────────────────────────────────


class TestBuildPRBody:
    def test_body_embeds_challenge_verdict(self, pipeline, tmp_path):
        issue = _issue(pipeline)
        prd = tmp_path / "PRD-GH-42.md"
        prd.write_text("PRD content")
        plan = tmp_path / "tasks.json"
        plan.write_text(json.dumps({"tasks": [{"id": "T1"}, {"id": "T2"}]}))
        challenge = {
            "verdict": "approve",
            "summary": "Issue is clear and actionable.",
            "challenges": [{"severity": "medium", "question": "Why now?", "alternative": "defer 1 week"}],
        }
        body = pipeline.build_pr_body(issue, challenge, prd, plan, "pytest + ruff clean")
        assert "Closes #42" in body
        assert "**approve**" in body
        assert "Why now?" in body
        assert "PRD-GH-42.md" in body
        assert "2 decomposed tasks" in body
        assert "pytest + ruff clean" in body


# ── Task count helper ────────────────────────────────────────────────────────


class TestTaskCount:
    def test_counts_tasks(self, pipeline, tmp_path):
        p = tmp_path / "tasks.json"
        p.write_text(json.dumps({"tasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}))
        assert pipeline._task_count(p) == 3

    def test_corrupted_returns_zero(self, pipeline, tmp_path):
        p = tmp_path / "tasks.json"
        p.write_text("{not json")
        assert pipeline._task_count(p) == 0


# ── Full process_one happy path ──────────────────────────────────────────────


class TestProcessOne:
    def _board_and_mapping(self):
        statuses = [
            BoardStatus("o_ready", "Todo"),
            BoardStatus("o_prog", "In Progress"),
            BoardStatus("o_rev", "In Review"),
            BoardStatus("o_done", "Done"),
            BoardStatus("o_blocked", "Blocked"),
            BoardStatus("o_failed", "Failed"),
        ]
        board = MagicMock()
        board.list_statuses.return_value = statuses
        board.move_item.return_value = True
        mapping = WorkflowMapping(
            board_kind="github_project",
            board_url="https://example/p/1",
            events={
                "picked_up": "In Progress",
                "in_review": "In Review",
                "done": "Done",
                "refused": "Blocked",
                "failed": "Failed",
            },
        )
        return board, mapping

    def test_happy_path_moves_cards_and_opens_pr(self, pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issue = _issue(pipeline, number=99)
        board, mapping = self._board_and_mapping()

        # Stub all external effects.
        monkeypatch.setattr(
            pipeline,
            "run_decision_gate",
            lambda issue: PROCEED,
        )
        monkeypatch.setattr(
            pipeline,
            "run_challenge",
            lambda issue: {"verdict": "approve", "summary": "ok", "challenges": []},
        )
        monkeypatch.setattr(
            pipeline,
            "run_prd_generation",
            lambda issue, challenge: tmp_path / f"PRD-GH-{issue.number}.md",
        )
        monkeypatch.setattr(
            pipeline,
            "run_tasks_decomposition",
            lambda issue, prd: tmp_path / f"whilly_GH-{issue.number}_tasks.json",
        )
        # Create the artefact files so build_pr_body stat() succeeds.
        (tmp_path / f"PRD-GH-{issue.number}.md").write_text("PRD body")
        (tmp_path / f"whilly_GH-{issue.number}_tasks.json").write_text(json.dumps({"tasks": [{"id": "T1"}]}))
        monkeypatch.setattr(pipeline, "run_execution", lambda plan, issue: True)
        monkeypatch.setattr(pipeline, "run_quality_gate", lambda: (True, "clean"))

        pr_opened = []
        monkeypatch.setattr(
            pipeline,
            "open_pr",
            lambda issue, body, base="main": pr_opened.append({"issue": issue, "body": body})
            or "https://github.com/acme/repo/pull/123",
        )

        pipeline.process_one(issue, board, mapping)

        # Card moved at least twice — picked_up and in_review.
        move_calls = board.move_item.call_args_list
        moved_to_names = [call.args[1].name for call in move_calls]
        assert "In Progress" in moved_to_names
        assert "In Review" in moved_to_names
        # PR was opened with the right body markers.
        assert len(pr_opened) == 1
        assert "Closes #99" in pr_opened[0]["body"]

    def test_refuse_short_circuits(self, pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issue = _issue(pipeline, number=51)
        board, mapping = self._board_and_mapping()

        monkeypatch.setattr(pipeline, "run_decision_gate", lambda issue: REFUSE)

        # Any downstream stage that gets called is a test failure.
        def _never(*a, **kw):
            raise AssertionError("downstream stage called after REFUSE")

        monkeypatch.setattr(pipeline, "run_challenge", _never)
        monkeypatch.setattr(pipeline, "run_prd_generation", _never)
        monkeypatch.setattr(pipeline, "run_tasks_decomposition", _never)

        pipeline.process_one(issue, board, mapping)
        move_calls = board.move_item.call_args_list
        moved_to_names = [call.args[1].name for call in move_calls]
        # Moved to In Progress (picked_up) then Blocked (refused).
        assert "Blocked" in moved_to_names

    def test_challenge_reject_stops_before_prd(self, pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issue = _issue(pipeline, number=77)
        board, mapping = self._board_and_mapping()

        monkeypatch.setattr(pipeline, "run_decision_gate", lambda issue: PROCEED)
        monkeypatch.setattr(
            pipeline,
            "run_challenge",
            lambda issue: {"verdict": "reject", "summary": "bad scope", "challenges": []},
        )
        prd_called = []
        monkeypatch.setattr(pipeline, "run_prd_generation", lambda *a, **kw: prd_called.append(1))

        pipeline.process_one(issue, board, mapping)
        assert prd_called == []
        moved_to_names = [c.args[1].name for c in board.move_item.call_args_list]
        assert "Blocked" in moved_to_names
        assert "In Review" not in moved_to_names

    def test_execution_failure_moves_to_failed(self, pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issue = _issue(pipeline, number=88)
        board, mapping = self._board_and_mapping()

        monkeypatch.setattr(pipeline, "run_decision_gate", lambda issue: PROCEED)
        monkeypatch.setattr(
            pipeline, "run_challenge", lambda issue: {"verdict": "approve", "summary": "", "challenges": []}
        )
        monkeypatch.setattr(pipeline, "run_prd_generation", lambda issue, c: tmp_path / "PRD.md")
        (tmp_path / "PRD.md").write_text("prd")
        monkeypatch.setattr(pipeline, "run_tasks_decomposition", lambda issue, prd: tmp_path / "plan.json")
        (tmp_path / "plan.json").write_text(json.dumps({"tasks": []}))
        monkeypatch.setattr(pipeline, "run_execution", lambda plan, issue: False)

        # pr should NOT be opened
        pr_opened = []
        monkeypatch.setattr(
            pipeline,
            "open_pr",
            lambda issue, body, base="main": pr_opened.append(1) or "",
        )

        pipeline.process_one(issue, board, mapping)
        assert pr_opened == []
        moved_to_names = [c.args[1].name for c in board.move_item.call_args_list]
        assert "Failed" in moved_to_names

    def test_quality_gate_failure_leaves_for_triage(self, pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        issue = _issue(pipeline, number=89)
        board, mapping = self._board_and_mapping()

        monkeypatch.setattr(pipeline, "run_decision_gate", lambda issue: PROCEED)
        monkeypatch.setattr(
            pipeline, "run_challenge", lambda issue: {"verdict": "approve", "summary": "", "challenges": []}
        )
        monkeypatch.setattr(pipeline, "run_prd_generation", lambda issue, c: tmp_path / "PRD.md")
        (tmp_path / "PRD.md").write_text("prd")
        monkeypatch.setattr(pipeline, "run_tasks_decomposition", lambda issue, prd: tmp_path / "plan.json")
        (tmp_path / "plan.json").write_text(json.dumps({"tasks": []}))
        monkeypatch.setattr(pipeline, "run_execution", lambda plan, issue: True)
        monkeypatch.setattr(pipeline, "run_quality_gate", lambda: (False, "pytest failed"))

        pr_opened = []
        monkeypatch.setattr(
            pipeline,
            "open_pr",
            lambda issue, body, base="main": pr_opened.append(1) or "",
        )

        pipeline.process_one(issue, board, mapping)
        assert pr_opened == []
        moved_to_names = [c.args[1].name for c in board.move_item.call_args_list]
        assert "Failed" in moved_to_names

    def test_no_workflow_still_processes(self, pipeline, tmp_path, monkeypatch):
        """With board=None, pipeline still runs — just no card movement."""
        monkeypatch.chdir(tmp_path)
        issue = _issue(pipeline, number=101)

        monkeypatch.setattr(pipeline, "run_decision_gate", lambda issue: PROCEED)
        monkeypatch.setattr(
            pipeline, "run_challenge", lambda issue: {"verdict": "approve", "summary": "", "challenges": []}
        )
        monkeypatch.setattr(pipeline, "run_prd_generation", lambda issue, c: tmp_path / "PRD.md")
        (tmp_path / "PRD.md").write_text("prd")
        monkeypatch.setattr(pipeline, "run_tasks_decomposition", lambda issue, prd: tmp_path / "plan.json")
        (tmp_path / "plan.json").write_text(json.dumps({"tasks": []}))
        monkeypatch.setattr(pipeline, "run_execution", lambda plan, issue: True)
        monkeypatch.setattr(pipeline, "run_quality_gate", lambda: (True, "clean"))

        pr_opened = []
        monkeypatch.setattr(
            pipeline,
            "open_pr",
            lambda issue, body, base="main": pr_opened.append(1) or "https://example/pr/1",
        )

        pipeline.process_one(issue, board=None, mapping=None)
        assert pr_opened == [1]
