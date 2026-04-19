"""Unit tests for whilly.decision_gate."""

from __future__ import annotations


import pytest

from whilly.agent_runner import AgentResult, AgentUsage
from whilly.decision_gate import (
    PROCEED,
    REFUSE,
    Decision,
    build_prompt,
    evaluate,
    label_flip_for_gh_task,
    parse_decision,
)
from whilly.task_manager import Task


def _make_task(**overrides) -> Task:
    base = dict(
        id="GH-1",
        phase="GH-Issues",
        category="github-issue",
        priority="medium",
        description="Add a /health endpoint to the FastAPI server returning ok",
        status="pending",
        dependencies=[],
        key_files=["app/server.py"],
        acceptance_criteria=["GET /health returns 200"],
        test_steps=["curl localhost"],
        prd_requirement="https://github.com/foo/bar/issues/1",
    )
    base.update(overrides)
    return Task(**base)


# ── parse_decision ─────────────────────────────────────────────────────────────


class TestParseDecision:
    def test_clean_proceed(self):
        d, r = parse_decision('{"decision":"proceed","reason":"clear"}')
        assert d == PROCEED
        assert r == "clear"

    def test_clean_refuse(self):
        d, r = parse_decision('{"decision":"refuse","reason":"empty"}')
        assert d == REFUSE
        assert r == "empty"

    def test_decision_with_padding(self):
        d, _ = parse_decision('  {"decision":"proceed","reason":"x"}  \n')
        assert d == PROCEED

    def test_extra_text_around(self):
        text = 'Sure, here you go: {"decision":"refuse","reason":"missing acceptance"}\n--end'
        d, r = parse_decision(text)
        assert d == REFUSE
        assert "missing" in r

    def test_invalid_decision_value_falls_open(self):
        d, _ = parse_decision('{"decision":"maybe","reason":"x"}')
        assert d == PROCEED

    def test_empty_text_falls_open(self):
        d, r = parse_decision("")
        assert d == PROCEED
        assert "fail-open" in r

    def test_keyword_fallback_refuse(self):
        d, _ = parse_decision("the gate would refuse this one")
        assert d == REFUSE

    def test_keyword_fallback_no_match_proceeds(self):
        d, _ = parse_decision("nonsense without keywords")
        assert d == PROCEED


# ── build_prompt ───────────────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_includes_id_priority_acceptance(self):
        p = build_prompt(_make_task())
        assert "GH-1" in p
        assert "medium" in p
        assert "GET /health returns 200" in p
        assert "app/server.py" in p

    def test_empty_acceptance_renders_marker(self):
        p = build_prompt(_make_task(acceptance_criteria=[]))
        assert "не задано" in p

    def test_empty_files_renders_marker(self):
        p = build_prompt(_make_task(key_files=[]))
        assert "не указаны" in p


# ── evaluate ───────────────────────────────────────────────────────────────────


class TestEvaluateAutoRefuseShortDescription:
    def test_short_description_auto_refuses_without_runner_call(self):
        called = []

        def runner(*args, **kwargs):
            called.append(args)
            raise AssertionError("runner should not be called for short descriptions")

        d = evaluate(_make_task(description="x"), runner=runner)
        assert d.decision == REFUSE
        assert "too short" in d.reason
        assert d.cost_usd == 0.0
        assert called == []


class TestEvaluateProceedFromRunner:
    def test_proceed_returned_with_cost(self):
        result = AgentResult(
            result_text='{"decision":"proceed","reason":"clear"}',
            usage=AgentUsage(cost_usd=0.003),
            exit_code=0,
        )

        def runner(prompt, model, timeout):
            return result

        d = evaluate(_make_task(), runner=runner)
        assert d.decision == PROCEED
        assert d.reason == "clear"
        assert d.cost_usd == pytest.approx(0.003)


class TestEvaluateRefuseFromRunner:
    def test_refuse_returned(self):
        result = AgentResult(
            result_text='{"decision":"refuse","reason":"no acceptance"}',
            usage=AgentUsage(cost_usd=0.002),
            exit_code=0,
        )

        d = evaluate(_make_task(), runner=lambda p, m, t: result)
        assert d.decision == REFUSE
        assert d.reason == "no acceptance"


class TestEvaluateFailOpen:
    def test_runner_exception_fails_open_to_proceed(self):
        def runner(*args, **kwargs):
            raise RuntimeError("network down")

        d = evaluate(_make_task(), runner=runner)
        assert d.decision == PROCEED
        assert "network down" in d.reason

    def test_runner_nonzero_exit_fails_open(self):
        result = AgentResult(result_text="", exit_code=2)

        d = evaluate(_make_task(), runner=lambda p, m, t: result)
        assert d.decision == PROCEED
        assert "exit 2" in d.reason

    def test_unparsable_response_falls_open(self):
        result = AgentResult(result_text="just plain text", exit_code=0)

        d = evaluate(_make_task(), runner=lambda p, m, t: result)
        assert d.decision == PROCEED


# ── label_flip_for_gh_task ─────────────────────────────────────────────────────


class TestLabelFlip:
    def test_no_flip_when_decision_is_proceed(self):
        d = Decision(decision=PROCEED, reason="ok")
        flipped = label_flip_for_gh_task(_make_task(), d, runner=lambda args: 0)
        assert flipped is False

    def test_no_flip_when_not_a_gh_task(self):
        d = Decision(decision=REFUSE, reason="x")
        task = _make_task(id="TASK-001", prd_requirement="")
        assert label_flip_for_gh_task(task, d, runner=lambda args: 0) is False

    def test_no_flip_when_url_lacks_issue_number(self):
        d = Decision(decision=REFUSE, reason="x")
        task = _make_task(prd_requirement="https://github.com/foo/bar/wiki/page")
        assert label_flip_for_gh_task(task, d, runner=lambda args: 0) is False

    def test_flip_called_with_correct_args(self):
        d = Decision(decision=REFUSE, reason="x")
        captured = []

        def fake_runner(args):
            captured.append(args)
            return 0

        flipped = label_flip_for_gh_task(_make_task(), d, runner=fake_runner)
        assert flipped is True
        assert captured
        args = captured[0]
        assert "issue" in args and "edit" in args
        assert "1" in args  # issue number
        assert "--add-label" in args and "needs-clarification" in args
        assert "--remove-label" in args and "whilly:ready" in args

    def test_flip_runner_failure_returns_false(self):
        d = Decision(decision=REFUSE, reason="x")
        flipped = label_flip_for_gh_task(_make_task(), d, runner=lambda args: 1)
        assert flipped is False
