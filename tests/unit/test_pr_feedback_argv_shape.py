"""Argv-shape coverage for the M2 PR-feedback poller (VAL-PR-009).

Pins the exact subprocess argvs and the order in which they are
issued. Any deviation in flag set, ``--json`` field selection, or
ordering must fail this suite.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from whilly.adapters.db.repository import (
    PR_REVIEW_APPROVED_EVENT_TYPE,
    PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
)
from whilly.sources import github_pr_feedback as gpf

PLAN_ID = "PLAN-PR-FEEDBACK-1"
TASK_ID = "T-PR-FEEDBACK-1"


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


def _make_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "plan_id": PLAN_ID,
        "task_id": TASK_ID,
        "pr_number": 77,
        "pr_url": "https://github.com/foo/bar/pull/77",
        "branch": "whilly/T-PR-FEEDBACK-1",
        "head_sha": "deadbeef",
        "state": "open",
        "review_decision": None,
        "last_seen_review_id": None,
        "last_seen_check_run_id": None,
        "last_synced_at": None,
    }
    base.update(overrides)
    return base


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class _SubprocessRecorder:
    """Replay canned stdout for each ``gh`` invocation, recording argv order."""

    def __init__(self, *, view_json: dict[str, Any], reviews_json: list[Any], comments_json: list[Any]) -> None:
        self.view_json = view_json
        self.reviews_json = reviews_json
        self.comments_json = comments_json
        self.calls: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        *,
        timeout: int,  # noqa: ARG002
        capture_output: bool = True,  # noqa: ARG002
        text: bool = True,  # noqa: ARG002
        env: dict[str, str] | None = None,  # noqa: ARG002
        check: bool = False,  # noqa: ARG002
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[1:3] == ["pr", "view"]:
            return _proc(json.dumps(self.view_json))
        if cmd[1] == "api" and cmd[2].endswith("/reviews"):
            return _proc(json.dumps(self.reviews_json))
        if cmd[1] == "api" and cmd[2].endswith("/comments"):
            return _proc(json.dumps(self.comments_json))
        raise AssertionError(f"unexpected gh argv: {cmd!r}")


def test_argv_shape_and_order_for_one_open_pr() -> None:
    repo = _FakeRepo(rows=[_make_row()])
    rec = _SubprocessRecorder(
        view_json={
            "reviewDecision": None,
            "statusCheckRollup": [],
            "latestReviews": [],
            "reviewRequests": [],
            "headRefOid": "deadbeef",
            "state": "OPEN",
        },
        reviews_json=[],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled == 1
    assert len(rec.calls) == 3, f"expected 3 gh invocations, got {rec.calls!r}"
    view_argv, reviews_argv, comments_argv = rec.calls

    assert view_argv == [
        "gh",
        "pr",
        "view",
        "77",
        "--json",
        "reviewDecision,statusCheckRollup,latestReviews,reviewRequests,headRefOid,state",
    ]
    assert reviews_argv == ["gh", "api", "repos/foo/bar/pulls/77/reviews"]
    assert comments_argv == ["gh", "api", "repos/foo/bar/pulls/77/comments"]


def test_pr_view_json_field_set_is_canonical() -> None:
    repo = _FakeRepo(rows=[_make_row()])
    rec = _SubprocessRecorder(
        view_json={"reviewDecision": None, "state": "OPEN"},
        reviews_json=[],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert rec.calls[0][:5] == ["gh", "pr", "view", "77", "--json"]
    json_fields = rec.calls[0][5].split(",")
    assert json_fields == [
        "reviewDecision",
        "statusCheckRollup",
        "latestReviews",
        "reviewRequests",
        "headRefOid",
        "state",
    ]


def test_two_open_prs_each_get_three_invocations_in_row_order() -> None:
    repo = _FakeRepo(
        rows=[
            _make_row(id=1, pr_number=77, pr_url="https://github.com/foo/bar/pull/77"),
            _make_row(id=2, pr_number=88, pr_url="https://github.com/baz/quux/pull/88"),
        ]
    )
    rec = _SubprocessRecorder(
        view_json={"reviewDecision": None, "state": "OPEN"},
        reviews_json=[],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        polled = asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert polled == 2
    assert len(rec.calls) == 6
    assert rec.calls[0][3] == "77"
    assert rec.calls[1] == ["gh", "api", "repos/foo/bar/pulls/77/reviews"]
    assert rec.calls[2] == ["gh", "api", "repos/foo/bar/pulls/77/comments"]
    assert rec.calls[3][3] == "88"
    assert rec.calls[4] == ["gh", "api", "repos/baz/quux/pulls/88/reviews"]
    assert rec.calls[5] == ["gh", "api", "repos/baz/quux/pulls/88/comments"]


def test_approved_emits_one_pr_review_approved_event_with_reviewer_login() -> None:
    repo = _FakeRepo(rows=[_make_row()])
    rec = _SubprocessRecorder(
        view_json={
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "latestReviews": [
                {"state": "APPROVED", "author": {"login": "reviewer-bot"}},
            ],
            "reviewRequests": [],
            "headRefOid": "abc123",
            "state": "OPEN",
        },
        reviews_json=[{"id": 1001, "state": "APPROVED", "user": {"login": "reviewer-bot"}}],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    approved = [e for e in repo.events if e["event_type"] == PR_REVIEW_APPROVED_EVENT_TYPE]
    changes = [e for e in repo.events if e["event_type"] == PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE]
    assert len(approved) == 1, f"expected one pr.review.approved, got {repo.events!r}"
    assert changes == []
    payload = approved[0]["payload"]
    assert payload["pr_number"] == 77
    assert payload["pr_url"] == "https://github.com/foo/bar/pull/77"
    assert payload["head_sha"] == "abc123"
    assert payload["reviewer"] == "reviewer-bot"


def test_changes_requested_emits_one_event_with_verbatim_comment_bodies() -> None:
    repo = _FakeRepo(rows=[_make_row()])
    raw_body = "Please rename foo to bar and also <UNTRUSTED>nested</UNTRUSTED>"
    rec = _SubprocessRecorder(
        view_json={
            "reviewDecision": "CHANGES_REQUESTED",
            "statusCheckRollup": [],
            "latestReviews": [
                {"state": "CHANGES_REQUESTED", "author": {"login": "picky-reviewer"}},
            ],
            "reviewRequests": [],
            "headRefOid": "cafef00d",
            "state": "OPEN",
        },
        reviews_json=[
            {"id": 2001, "state": "CHANGES_REQUESTED", "user": {"login": "picky-reviewer"}},
        ],
        comments_json=[
            {
                "id": 9001,
                "body": raw_body,
                "path": "src/server.py",
                "line": 42,
                "user": {"login": "picky-reviewer"},
            },
            {
                "id": 9002,
                "body": "drop this dead code",
                "path": "src/util.py",
                "line": 7,
                "user": {"login": "another-reviewer"},
            },
        ],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    changes = [e for e in repo.events if e["event_type"] == PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE]
    assert len(changes) == 1, f"expected one pr.review.changes_requested, got {repo.events!r}"
    payload = changes[0]["payload"]
    assert payload["pr_number"] == 77
    assert payload["head_sha"] == "cafef00d"
    comments = payload["comments"]
    assert isinstance(comments, list) and len(comments) == 2
    assert comments[0] == {
        "body": raw_body,
        "path": "src/server.py",
        "line": 42,
        "author": "picky-reviewer",
    }
    assert comments[1] == {
        "body": "drop this dead code",
        "path": "src/util.py",
        "line": 7,
        "author": "another-reviewer",
    }


def test_cursor_advances_to_max_observed_review_id() -> None:
    repo = _FakeRepo(rows=[_make_row()])
    rec = _SubprocessRecorder(
        view_json={
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [{"databaseId": 555}],
            "latestReviews": [{"state": "APPROVED", "author": {"login": "x"}}],
            "reviewRequests": [],
            "headRefOid": "h",
            "state": "OPEN",
        },
        reviews_json=[{"id": 100, "state": "APPROVED"}, {"id": 250, "state": "APPROVED"}],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    assert repo.cursor_calls == [{"pr_id": 1, "last_seen_review_id": 250, "last_seen_check_run_id": 555}]


def test_second_poll_with_identical_response_emits_no_new_review_events() -> None:
    cursor = {"value": 0}

    class _Repo(_FakeRepo):
        async def list_open_pull_requests(self, plan_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
            return [_make_row(last_seen_review_id=cursor["value"])]

        async def advance_pull_request_cursor(
            self,
            pr_id: int,  # noqa: ARG002
            *,
            last_seen_review_id: int | None,
            last_seen_check_run_id: int | None,  # noqa: ARG002
        ) -> None:
            if last_seen_review_id is not None:
                cursor["value"] = last_seen_review_id

    repo = _Repo()
    rec = _SubprocessRecorder(
        view_json={
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "latestReviews": [{"state": "APPROVED", "author": {"login": "x"}}],
            "reviewRequests": [],
            "headRefOid": "h",
            "state": "OPEN",
        },
        reviews_json=[{"id": 100, "state": "APPROVED"}],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    approved = [e for e in repo.events if e["event_type"] == PR_REVIEW_APPROVED_EVENT_TYPE]
    assert len(approved) == 1, f"second poll re-emitted; events={repo.events!r}"


def test_merged_state_emits_pr_merged_and_updates_state_column() -> None:
    repo = _FakeRepo(rows=[_make_row()])
    rec = _SubprocessRecorder(
        view_json={
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "latestReviews": [],
            "reviewRequests": [],
            "headRefOid": "merged-sha",
            "state": "MERGED",
            "mergedAt": "2026-05-05T12:34:56Z",
        },
        reviews_json=[],
        comments_json=[],
    )

    with patch.object(gpf.subprocess, "run", side_effect=rec):
        asyncio.run(gpf.poll_pr_feedback(repo, PLAN_ID))

    merged = [e for e in repo.events if e["event_type"] == "pr.merged"]
    assert len(merged) == 1, f"expected one pr.merged event, got {repo.events!r}"
    payload = merged[0]["payload"]
    assert payload["pr_number"] == 77
    assert payload["pr_url"] == "https://github.com/foo/bar/pull/77"
    assert payload["head_sha"] == "merged-sha"
    assert payload["merged_at"] == "2026-05-05T12:34:56Z"
    assert repo.state_calls == [{"pr_id": 1, "state": "merged"}]
    review_events = [
        e
        for e in repo.events
        if e["event_type"] in (PR_REVIEW_APPROVED_EVENT_TYPE, PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE)
    ]
    assert review_events == [], f"merged cycle leaked review events: {review_events!r}"
