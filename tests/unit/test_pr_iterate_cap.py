"""Unit: WHILLY_MAX_REVIEW_ITERATIONS cap firing semantics (VAL-PR-017, VAL-PR-027).

Drives :func:`whilly.workflow.pr_iterate.spawn_followup` with a fake
asyncpg-shaped connection so the cap branch can be exercised without
booting a Postgres testcontainer. The integration test under
``tests/integration/test_pr_iterate_followup.py`` covers the
real-DB path with the same code under test.

Parametrized over ``WHILLY_MAX_REVIEW_ITERATIONS=0``, ``=1``, and
``=3`` per the validation contract:

* ``0`` — first ever ``pr.review.changes_requested`` for any task
  spawns zero rev tasks (VAL-PR-027).
* ``1`` — first event spawns ``-rev-1``, second event hits the cap.
* ``3`` — fourth event hits the cap (VAL-PR-017).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from whilly.adapters.db.repository import PR_ITERATION_REQUESTED_EVENT_TYPE
from whilly.workflow.pr_iterate import (
    DEFAULT_MAX_REVIEW_ITERATIONS,
    MAX_REVIEW_ITERATIONS_ENV,
    get_max_review_iterations,
    spawn_followup,
)


PLAN_ID = "PLAN-ITERATE-CAP"
ORIG_TASK_ID = "task-42"
PR_URL = "https://github.com/foo/bar/pull/42"


class _FakeConn:
    """In-memory asyncpg-shaped connection just rich enough for spawn_followup."""

    def __init__(self, rev_count: int = 0, orig_priority: str = "high") -> None:
        self.rev_count = rev_count
        self.orig_priority = orig_priority
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.events: list[dict[str, Any]] = []
        self.inserted_tasks: list[dict[str, Any]] = []
        self._next_event_id = 1

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "COUNT(*)" in query:
            return self.rev_count
        if "INSERT INTO events" in query:
            event_id = self._next_event_id
            self._next_event_id += 1
            self.events.append(
                {
                    "id": event_id,
                    "task_id": args[0],
                    "plan_id": args[1],
                    "event_type": args[2],
                    "payload": json.loads(args[3]),
                }
            )
            return event_id
        raise NotImplementedError(f"unexpected fetchval query: {query!r}")

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "FROM tasks WHERE id" in query:
            return {
                "priority": self.orig_priority,
                "key_files": json.dumps(["src/a.py"]),
            }
        raise NotImplementedError(f"unexpected fetchrow query: {query!r}")

    async def execute(self, query: str, *args: Any) -> Any:
        self.executed.append((query, args))
        if "INSERT INTO tasks" in query:
            self.inserted_tasks.append({"id": args[0], "plan_id": args[1], "args": args})
        return "INSERT 0 1"


# ---------------------------------------------------------------------------
# get_max_review_iterations env parsing
# ---------------------------------------------------------------------------


def test_default_when_env_unset() -> None:
    assert get_max_review_iterations({}) == DEFAULT_MAX_REVIEW_ITERATIONS


@pytest.mark.parametrize("raw", ["0", "1", "3", "10"])
def test_parses_positive_integer(raw: str) -> None:
    assert get_max_review_iterations({MAX_REVIEW_ITERATIONS_ENV: raw}) == int(raw)


def test_negative_clamped_to_zero() -> None:
    assert get_max_review_iterations({MAX_REVIEW_ITERATIONS_ENV: "-3"}) == 0


def test_garbage_falls_back_to_default() -> None:
    assert get_max_review_iterations({MAX_REVIEW_ITERATIONS_ENV: "not-a-number"}) == DEFAULT_MAX_REVIEW_ITERATIONS


# ---------------------------------------------------------------------------
# spawn_followup cap behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "rev_count", "should_spawn", "expected_iteration"),
    [
        # MAX=0 — VAL-PR-027: first ever event refuses immediately
        (0, 0, False, 0),
        # MAX=1 — first event spawns rev-1, second refuses
        (1, 0, True, 1),
        (1, 1, False, 1),
        # MAX=3 — VAL-PR-017: fourth event refuses
        (3, 0, True, 1),
        (3, 1, True, 2),
        (3, 2, True, 3),
        (3, 3, False, 3),
    ],
)
async def test_cap_branch_decides_spawn_or_refuse(
    cap: int,
    rev_count: int,
    should_spawn: bool,
    expected_iteration: int,
) -> None:
    fake_conn = _FakeConn(rev_count=rev_count)
    env = {MAX_REVIEW_ITERATIONS_ENV: str(cap)}
    comments = [{"body": "please rotate AKIAIOSFODNN7EXAMPLE now"}]

    result = await spawn_followup(
        orig_task_id=ORIG_TASK_ID,
        pr_url=PR_URL,
        comments=comments,
        plan_id=PLAN_ID,
        conn=fake_conn,  # type: ignore[arg-type]
        env=env,
    )

    if should_spawn:
        assert result is not None
        assert result.id == f"{ORIG_TASK_ID}-rev-{expected_iteration}"
        assert len(fake_conn.inserted_tasks) == 1
        assert len(fake_conn.events) == 1
        evt = fake_conn.events[0]
        assert evt["event_type"] == PR_ITERATION_REQUESTED_EVENT_TYPE
        payload = evt["payload"]
        assert payload["orig_task_id"] == ORIG_TASK_ID
        assert payload["new_task_id"] == result.id
        assert payload["pr_url"] == PR_URL
        assert payload["iteration"] == expected_iteration
        assert "refused" not in payload
    else:
        assert result is None
        assert fake_conn.inserted_tasks == []
        assert len(fake_conn.events) == 1
        evt = fake_conn.events[0]
        assert evt["event_type"] == PR_ITERATION_REQUESTED_EVENT_TYPE
        payload = evt["payload"]
        assert payload["refused"] is True
        assert payload["iteration"] == expected_iteration
        assert payload["orig_task_id"] == ORIG_TASK_ID
        assert payload["pr_url"] == PR_URL
        # Sanitized comment payload must be embedded so the audit trail
        # records the offending review (VAL-CROSS-006).
        assert "comments" in payload
        assert "AKIAIOSFODNN7EXAMPLE" not in payload["comments"]
        assert "<UNTRUSTED kind=pr_review_comment>" in payload["comments"]


async def test_cap_zero_first_event_does_not_insert_task_row() -> None:
    """VAL-PR-027 boundary: MAX=0 spawns zero rev tasks ever."""
    fake_conn = _FakeConn(rev_count=0)
    env = {MAX_REVIEW_ITERATIONS_ENV: "0"}

    result = await spawn_followup(
        orig_task_id=ORIG_TASK_ID,
        pr_url=PR_URL,
        comments=[{"body": "fix this please"}],
        plan_id=PLAN_ID,
        conn=fake_conn,  # type: ignore[arg-type]
        env=env,
    )

    assert result is None
    assert fake_conn.inserted_tasks == []
    assert len(fake_conn.events) == 1
    payload = fake_conn.events[0]["payload"]
    assert payload["iteration"] == 0
    assert payload["refused"] is True


async def test_malformed_orig_task_id_raises_before_any_db_io() -> None:
    fake_conn = _FakeConn(rev_count=0)
    bad_id = 'x"; rm -rf $HOME; #'
    with pytest.raises(ValueError):
        await spawn_followup(
            orig_task_id=bad_id,
            pr_url=PR_URL,
            comments=[{"body": "anything"}],
            plan_id=PLAN_ID,
            conn=fake_conn,  # type: ignore[arg-type]
            env={MAX_REVIEW_ITERATIONS_ENV: "3"},
        )
    assert fake_conn.inserted_tasks == []
    assert fake_conn.events == []
    assert fake_conn.executed == []


async def test_path_traversal_orig_task_id_raises() -> None:
    fake_conn = _FakeConn(rev_count=0)
    with pytest.raises(ValueError):
        await spawn_followup(
            orig_task_id="../escape",
            pr_url=PR_URL,
            comments=[{"body": "anything"}],
            plan_id=PLAN_ID,
            conn=fake_conn,  # type: ignore[arg-type]
        )
    assert fake_conn.inserted_tasks == []
    assert fake_conn.events == []
