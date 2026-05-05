"""Failure-path coverage for the M2 PR-feedback poller (VAL-PR-013).

* ``gh`` non-zero exit → WARNING with the offending PR number, the
  poll returns without raising, no event is emitted, and the cursor
  is NOT advanced.
* ``subprocess.TimeoutExpired`` → same contract.
* A subsequent successful poll resumes against the same cursor.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from whilly.adapters.db.repository import (
    PR_REVIEW_APPROVED_EVENT_TYPE,
)
from whilly.sources import github_pr_feedback as gpf

PLAN_ID = "PLAN-PR-FEEDBACK-FAIL"
TASK_ID = "T-PR-FEEDBACK-FAIL"


@dataclass
class _FakeRepo:
    rows: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    cursor_calls: list[dict[str, Any]] = field(default_factory=list)
    state_calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_open_pull_requests(self, plan_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        return [dict(r) for r in self.rows]

    async def update_pull_request_state(self, pr_id: int, state: str) -> None:
        self.state_calls.append({"pr_id": pr_id, "state": state})

    async def advance_pull_request_cursor(
        self,
        pr_id: int,
        *,
        last_seen_review_id: int | None,
        last_seen_check_run_id: int | None,
    ) -> None:
        self.cursor_calls.append(
            {
                "pr_id": pr_id,
                "last_seen_review_id": last_seen_review_id,
                "last_seen_check_run_id": last_seen_check_run_id,
            }
        )

    async def emit_pr_event(
        self,
        event_type: str,
        *,
        plan_id: str | None,
        task_id: str | None,
        payload: dict[str, Any],
    ) -> int:
        self.events.append(
            {
                "event_type": event_type,
                "plan_id": plan_id,
                "task_id": task_id,
                "payload": dict(payload),
            }
        )
        return len(self.events)


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "plan_id": PLAN_ID,
        "task_id": TASK_ID,
        "pr_number": 99,
        "pr_url": "https://github.com/foo/bar/pull/99",
        "branch": "whilly/T-X",
        "head_sha": "h",
        "state": "open",
        "review_decision": None,
        "last_seen_review_id": None,
        "last_seen_check_run_id": None,
        "last_synced_at": None,
    }
    base.update(overrides)
    return base


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_gh_pr_view_nonzero_exit_logs_warning_and_skips_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = _FakeRepo(rows=[_row()])

    def fake_run(cmd, **_kwargs):
        return _proc(returncode=1, stderr="API rate limited")

    with caplog.at_level(logging.WARNING, logger="whilly.sources.github_pr_feedback"):
        with patch.object(gpf.subprocess, "run", side_effect=fake_run):
            polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled == 0
    assert repo.events == []
    assert repo.cursor_calls == [], f"cursor advanced after gh failure: {repo.cursor_calls!r}"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("99" in r.getMessage() for r in warnings), (
        f"WARNING did not mention PR number 99: {[r.getMessage() for r in warnings]!r}"
    )


def test_gh_api_reviews_nonzero_exit_logs_warning_and_skips_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = _FakeRepo(rows=[_row()])

    def fake_run(cmd, **_kwargs):
        if cmd[1:3] == ["pr", "view"]:
            return _proc(stdout='{"reviewDecision": null, "state": "OPEN"}')
        return _proc(returncode=2, stderr="boom")

    with caplog.at_level(logging.WARNING, logger="whilly.sources.github_pr_feedback"):
        with patch.object(gpf.subprocess, "run", side_effect=fake_run):
            polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled == 0
    assert repo.events == []
    assert repo.cursor_calls == []
    assert any("99" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_subprocess_timeout_logs_warning_and_skips_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = _FakeRepo(rows=[_row()])

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 60))

    with caplog.at_level(logging.WARNING, logger="whilly.sources.github_pr_feedback"):
        with patch.object(gpf.subprocess, "run", side_effect=fake_run):
            polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled == 0
    assert repo.events == []
    assert repo.cursor_calls == []
    assert any("99" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_failure_then_recovery_resumes_from_same_cursor() -> None:
    repo = _FakeRepo(rows=[_row(last_seen_review_id=42)])

    state = {"phase": "fail"}

    def fake_run(cmd, **_kwargs):
        if state["phase"] == "fail":
            return _proc(returncode=1, stderr="transient")
        if cmd[1:3] == ["pr", "view"]:
            return _proc(
                stdout=(
                    '{"reviewDecision": "APPROVED",'
                    ' "statusCheckRollup": [],'
                    ' "latestReviews": [{"state": "APPROVED", "author": {"login": "u"}}],'
                    ' "reviewRequests": [],'
                    ' "headRefOid": "x",'
                    ' "state": "OPEN"}'
                )
            )
        if cmd[1] == "api" and cmd[2].endswith("/reviews"):
            return _proc(stdout='[{"id": 100, "state": "APPROVED"}]')
        return _proc(stdout="[]")

    with patch.object(gpf.subprocess, "run", side_effect=fake_run):
        polled_fail = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))
        assert polled_fail == 0
        assert repo.cursor_calls == []
        assert repo.events == []

        state["phase"] = "ok"
        polled_ok = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled_ok == 1
    assert repo.cursor_calls == [{"pr_id": 1, "last_seen_review_id": 100, "last_seen_check_run_id": None}]
    approved = [e for e in repo.events if e["event_type"] == PR_REVIEW_APPROVED_EVENT_TYPE]
    assert len(approved) == 1


def test_failure_for_one_pr_does_not_block_other_prs() -> None:
    repo = _FakeRepo(
        rows=[
            _row(id=1, pr_number=11, pr_url="https://github.com/foo/bar/pull/11"),
            _row(id=2, pr_number=22, pr_url="https://github.com/baz/quux/pull/22"),
        ]
    )

    def fake_run(cmd, **_kwargs):
        if cmd[1:3] == ["pr", "view"]:
            target = cmd[3]
            if target == "11":
                return _proc(returncode=1, stderr="boom on 11")
            return _proc(stdout='{"reviewDecision": null, "state": "OPEN"}')
        return _proc(stdout="[]")

    with patch.object(gpf.subprocess, "run", side_effect=fake_run):
        polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled == 1
    assert {c["pr_id"] for c in repo.cursor_calls} == {2}, (
        f"expected only PR id=2 cursor advance, got {repo.cursor_calls!r}"
    )


def test_one_hundred_open_prs_one_cycle_completes_under_five_seconds() -> None:
    rows = [_row(id=i, pr_number=i, pr_url=f"https://github.com/foo/bar/pull/{i}") for i in range(1, 101)]
    repo = _FakeRepo(rows=rows)

    def fake_run(cmd, **_kwargs):
        if cmd[1:3] == ["pr", "view"]:
            return _proc(stdout='{"reviewDecision": null, "state": "OPEN"}')
        return _proc(stdout="[]")

    import time

    with patch.object(gpf.subprocess, "run", side_effect=fake_run):
        start = time.perf_counter()
        polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))
        elapsed = time.perf_counter() - start

    assert polled == 100
    assert elapsed < 5.0, f"100-PR cycle took {elapsed:.2f}s (>5s budget)"
