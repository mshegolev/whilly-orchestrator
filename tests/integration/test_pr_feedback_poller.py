"""Integration: PR-feedback poller against real Postgres + JSONL sink (VAL-PR-009..013, VAL-PR-024).

Boots an ephemeral Postgres-15 testcontainer (via the shared
``db_pool`` / ``task_repo`` fixtures), seeds one ``plans`` row plus one
``pull_requests`` row, monkeypatches ``subprocess.run`` so the three
``gh`` invocations return canned JSON, and asserts:

* The three argvs are issued in the documented order.
* ``reviewDecision='APPROVED'`` ⇒ exactly one ``pr.review.approved``
  event in both the Postgres ``events`` table AND the JSONL mirror.
* ``reviewDecision='CHANGES_REQUESTED'`` ⇒ exactly one
  ``pr.review.changes_requested`` event whose ``detail.comments`` is
  the verbatim list from the comments API.
* After the first successful poll, the row's ``last_seen_review_id`` /
  ``last_synced_at`` are updated; a second poll against an identical
  response inserts zero new ``pr.review.*`` events.
* ``state='MERGED'`` ⇒ exactly one ``pr.merged`` event AND
  ``pull_requests.state`` flips to ``'merged'`` AND a follow-up poll
  emits no further ``pr.merged`` event (the row no longer matches the
  ``state='open'`` filter).
* Transient ``gh`` failure (non-zero exit) preserves the cursor and
  emits no events.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import (
    PR_MERGED_EVENT_TYPE,
    PR_REVIEW_APPROVED_EVENT_TYPE,
    PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE,
    TaskRepository,
)
from whilly.audit import DEFAULT_JSONL_FILENAME, JsonlEventSink, LOG_DIR_ENV
from whilly.sources import github_pr_feedback as gpf

pytestmark = DOCKER_REQUIRED

PLAN_ID = "PLAN-PR-FEEDBACK-INT"
TASK_ID = "T-PR-FEEDBACK-INT"
PR_NUMBER = 77
PR_URL = "https://github.com/foo/bar/pull/77"


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    log_dir = tmp_path / "whilly_logs"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    yield log_dir


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


async def _seed_plan_and_pr(pool: asyncpg.Pool, *, head_sha: str = "deadbeef") -> int:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name, github_issue_ref) VALUES ($1, $2, $3)",
            PLAN_ID,
            f"plan {PLAN_ID}",
            "foo/bar/42",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'DONE', '[]'::jsonb, '[]'::jsonb,
                    'medium', 'desc', '[]'::jsonb, '[]'::jsonb, '', 0)
            """,
            TASK_ID,
            PLAN_ID,
        )
        pr_id = await conn.fetchval(
            """
            INSERT INTO pull_requests
                (plan_id, task_id, pr_number, pr_url, branch, head_sha, state)
            VALUES ($1, $2, $3, $4, $5, $6, 'open')
            RETURNING id
            """,
            PLAN_ID,
            TASK_ID,
            PR_NUMBER,
            PR_URL,
            "whilly/T-PR-FEEDBACK-INT",
            head_sha,
        )
    return int(pr_id)


def _read_jsonl_lines(jsonl_path: Path) -> list[dict[str, Any]]:
    if not jsonl_path.is_file():
        return []
    return [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# VAL-PR-010 + VAL-PR-009: APPROVED → one event in Postgres + JSONL
# ---------------------------------------------------------------------------


async def test_approved_review_emits_one_event_to_both_sinks(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr_id = await _seed_plan_and_pr(db_pool, head_sha="abc123")
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    captured: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        captured.append(list(cmd))
        if cmd[1:3] == ["pr", "view"]:
            return _proc(
                stdout=json.dumps(
                    {
                        "reviewDecision": "APPROVED",
                        "statusCheckRollup": [],
                        "latestReviews": [{"state": "APPROVED", "author": {"login": "reviewer-bot"}}],
                        "reviewRequests": [],
                        "headRefOid": "abc123",
                        "state": "OPEN",
                    }
                )
            )
        if cmd[1] == "api" and cmd[2].endswith("/reviews"):
            return _proc(stdout=json.dumps([{"id": 1001, "state": "APPROVED"}]))
        return _proc(stdout="[]")

    monkeypatch.setattr(gpf.subprocess, "run", fake_run)

    polled = await gpf.poll_pr_feedback(repo, PLAN_ID)
    assert polled == 1

    assert captured[0] == [
        "gh",
        "pr",
        "view",
        str(PR_NUMBER),
        "--json",
        "reviewDecision,statusCheckRollup,latestReviews,reviewRequests,headRefOid,state",
    ]
    assert captured[1] == ["gh", "api", f"repos/foo/bar/pulls/{PR_NUMBER}/reviews"]
    assert captured[2] == ["gh", "api", f"repos/foo/bar/pulls/{PR_NUMBER}/comments"]

    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id",
            TASK_ID,
        )
        pr_row = await conn.fetchrow("SELECT * FROM pull_requests WHERE id = $1", pr_id)

    approved = [e for e in events if e["event_type"] == PR_REVIEW_APPROVED_EVENT_TYPE]
    assert len(approved) == 1, f"expected exactly one pr.review.approved event, got {events!r}"
    assert all(e["event_type"] != PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE for e in events)
    payload = approved[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    for key in ("pr_number", "pr_url", "head_sha", "reviewer"):
        assert key in payload, f"detail missing {key!r}: {payload!r}"
    assert payload["pr_number"] == PR_NUMBER
    assert payload["reviewer"] == "reviewer-bot"

    assert pr_row["last_seen_review_id"] == 1001
    assert pr_row["last_synced_at"] is not None
    assert pr_row["state"] == "open"

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    jsonl_approved = [line for line in jsonl_lines if line["event_type"] == PR_REVIEW_APPROVED_EVENT_TYPE]
    assert len(jsonl_approved) == 1, f"expected one pr.review.approved JSONL line, got {jsonl_lines!r}"
    assert jsonl_approved[0]["payload"] == payload


# ---------------------------------------------------------------------------
# VAL-PR-011: CHANGES_REQUESTED → comments verbatim in detail
# ---------------------------------------------------------------------------


async def test_changes_requested_event_carries_verbatim_comment_bodies(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr_id = await _seed_plan_and_pr(db_pool, head_sha="cafef00d")
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    raw_body = "Please rename foo to bar and also AKIAIOSFODNN7EXAMPLE — verbatim"

    def fake_run(cmd, **_kwargs):
        if cmd[1:3] == ["pr", "view"]:
            return _proc(
                stdout=json.dumps(
                    {
                        "reviewDecision": "CHANGES_REQUESTED",
                        "statusCheckRollup": [],
                        "latestReviews": [{"state": "CHANGES_REQUESTED", "author": {"login": "picky"}}],
                        "reviewRequests": [],
                        "headRefOid": "cafef00d",
                        "state": "OPEN",
                    }
                )
            )
        if cmd[1] == "api" and cmd[2].endswith("/reviews"):
            return _proc(stdout=json.dumps([{"id": 2001, "state": "CHANGES_REQUESTED"}]))
        return _proc(
            stdout=json.dumps(
                [
                    {
                        "id": 9001,
                        "body": raw_body,
                        "path": "src/server.py",
                        "line": 42,
                        "user": {"login": "picky"},
                    }
                ]
            )
        )

    monkeypatch.setattr(gpf.subprocess, "run", fake_run)

    await gpf.poll_pr_feedback(repo, PLAN_ID)

    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id",
            TASK_ID,
        )
        pr_row = await conn.fetchrow("SELECT last_seen_review_id FROM pull_requests WHERE id = $1", pr_id)

    cr_events = [e for e in events if e["event_type"] == PR_REVIEW_CHANGES_REQUESTED_EVENT_TYPE]
    assert len(cr_events) == 1, f"expected one pr.review.changes_requested, got {events!r}"
    payload = cr_events[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    comments = payload["comments"]
    assert isinstance(comments, list) and len(comments) == 1
    assert comments[0] == {
        "body": raw_body,
        "path": "src/server.py",
        "line": 42,
        "author": "picky",
    }
    assert pr_row["last_seen_review_id"] == 2001


# ---------------------------------------------------------------------------
# VAL-PR-012: cursor advances; identical second poll = zero new events
# ---------------------------------------------------------------------------


async def test_second_poll_with_identical_response_emits_no_new_events(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_plan_and_pr(db_pool)
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    def fake_run(cmd, **_kwargs):
        if cmd[1:3] == ["pr", "view"]:
            return _proc(
                stdout=json.dumps(
                    {
                        "reviewDecision": "APPROVED",
                        "statusCheckRollup": [],
                        "latestReviews": [{"state": "APPROVED", "author": {"login": "u"}}],
                        "reviewRequests": [],
                        "headRefOid": "h",
                        "state": "OPEN",
                    }
                )
            )
        if cmd[1] == "api" and cmd[2].endswith("/reviews"):
            return _proc(stdout=json.dumps([{"id": 555, "state": "APPROVED"}]))
        return _proc(stdout="[]")

    monkeypatch.setattr(gpf.subprocess, "run", fake_run)

    await gpf.poll_pr_feedback(repo, PLAN_ID)
    await gpf.poll_pr_feedback(repo, PLAN_ID)

    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type FROM events WHERE task_id = $1",
            TASK_ID,
        )
    approved = [e for e in events if e["event_type"] == PR_REVIEW_APPROVED_EVENT_TYPE]
    assert len(approved) == 1, f"second poll re-emitted; events={[e['event_type'] for e in events]!r}"


# ---------------------------------------------------------------------------
# VAL-PR-013: gh non-zero exit → WARNING + cursor unchanged
# ---------------------------------------------------------------------------


async def test_gh_failure_logs_warning_and_does_not_advance_cursor(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pr_id = await _seed_plan_and_pr(db_pool)
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    def fake_run(cmd, **_kwargs):  # noqa: ARG001
        return _proc(returncode=1, stderr="API rate limited")

    monkeypatch.setattr(gpf.subprocess, "run", fake_run)

    import logging

    with caplog.at_level(logging.WARNING, logger="whilly.sources.github_pr_feedback"):
        polled = await gpf.poll_pr_feedback(repo, PLAN_ID)

    assert polled == 0
    async with db_pool.acquire() as conn:
        events = await conn.fetch("SELECT event_type FROM events WHERE task_id = $1", TASK_ID)
        pr_row = await conn.fetchrow(
            "SELECT last_seen_review_id, last_synced_at FROM pull_requests WHERE id = $1",
            pr_id,
        )
    pr_event_types = [e["event_type"] for e in events if e["event_type"].startswith("pr.")]
    assert pr_event_types == [], f"unexpected pr.* events on failure: {pr_event_types!r}"
    assert pr_row["last_seen_review_id"] is None
    assert pr_row["last_synced_at"] is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(PR_NUMBER) in r.getMessage() for r in warnings), (
        f"WARNING did not mention PR #{PR_NUMBER}: {[r.getMessage() for r in warnings]!r}"
    )


# ---------------------------------------------------------------------------
# VAL-PR-024: state=MERGED → one pr.merged + state column flips
# ---------------------------------------------------------------------------


async def test_merged_state_emits_pr_merged_once_and_updates_state_column(
    db_pool: asyncpg.Pool,
    whilly_log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr_id = await _seed_plan_and_pr(db_pool, head_sha="merge-sha")
    repo = TaskRepository(db_pool)
    repo.attach_jsonl_sink(JsonlEventSink(log_dir=whilly_log_dir))

    def fake_run(cmd, **_kwargs):
        if cmd[1:3] == ["pr", "view"]:
            return _proc(
                stdout=json.dumps(
                    {
                        "reviewDecision": "APPROVED",
                        "statusCheckRollup": [],
                        "latestReviews": [],
                        "reviewRequests": [],
                        "headRefOid": "merge-sha",
                        "state": "MERGED",
                        "mergedAt": "2026-05-05T12:34:56Z",
                    }
                )
            )
        return _proc(stdout="[]")

    monkeypatch.setattr(gpf.subprocess, "run", fake_run)

    await gpf.poll_pr_feedback(repo, PLAN_ID)

    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id",
            TASK_ID,
        )
        pr_row = await conn.fetchrow("SELECT state FROM pull_requests WHERE id = $1", pr_id)

    merged = [e for e in events if e["event_type"] == PR_MERGED_EVENT_TYPE]
    assert len(merged) == 1, f"expected one pr.merged event, got {events!r}"
    payload = merged[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    for key in ("pr_number", "pr_url", "head_sha", "merged_at"):
        assert key in payload, f"merged detail missing {key!r}: {payload!r}"
    assert payload["pr_number"] == PR_NUMBER
    assert payload["head_sha"] == "merge-sha"
    assert payload["merged_at"] == "2026-05-05T12:34:56Z"
    assert pr_row["state"] == "merged"

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    jsonl_merged = [line for line in jsonl_lines if line["event_type"] == PR_MERGED_EVENT_TYPE]
    assert len(jsonl_merged) == 1

    await gpf.poll_pr_feedback(repo, PLAN_ID)
    async with db_pool.acquire() as conn:
        events_after = await conn.fetch(
            "SELECT event_type FROM events WHERE task_id = $1",
            TASK_ID,
        )
    merged_after = [e for e in events_after if e["event_type"] == PR_MERGED_EVENT_TYPE]
    assert len(merged_after) == 1, "merged event was re-emitted on second poll"
