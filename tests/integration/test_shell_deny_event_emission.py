"""Integration coverage for v6 A2 shell-deny event emission."""

from __future__ import annotations

import json

import asyncpg

from whilly.adapters.db.repository import TaskRepository
from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.agent_runner import SHELL_COMMAND_BLOCKED_EVENT_TYPE, SHELL_COMMAND_FAIL_REASON
from whilly.core.models import Plan, Task, TaskStatus, WorkerId
from whilly.worker.local import run_local_worker


PLAN_ID = "plan-shelldeny-int"
TASK_ID = "TASK-SHELLDENY-INT"
WORKER_ID: WorkerId = "w-shelldeny-int"


def _decode(raw: object) -> dict[str, object]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(decoded, dict)
    return decoded


async def _seed_shell_deny_task(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", PLAN_ID, "Shell Deny Integration")
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            WORKER_ID,
            "shelldeny-host",
            "hash-shelldeny",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'medium', $3, '[]'::jsonb, '[]'::jsonb, '', 0)
            """,
            TASK_ID,
            PLAN_ID,
            "Run cleanup: rm -rf /",
        )


async def test_shell_deny_emits_block_event_and_fails_task(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    await _seed_shell_deny_task(db_pool)
    plan = Plan(id=PLAN_ID, name="Shell Deny Integration")

    async def runner(task: Task, prompt: str) -> AgentResult:  # pragma: no cover
        raise AssertionError("shell deny-list must fail before runner invocation")

    stats = await run_local_worker(task_repo, runner, plan, WORKER_ID, idle_wait=0, max_iterations=1)

    assert stats.failed == 1
    async with db_pool.acquire() as conn:
        task_row = await conn.fetchrow("SELECT status FROM tasks WHERE id = $1", TASK_ID)
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id",
            TASK_ID,
        )

    assert task_row is not None
    assert task_row["status"] == TaskStatus.FAILED.value
    event_types = [row["event_type"] for row in events]
    assert event_types == ["CLAIM", "START", SHELL_COMMAND_BLOCKED_EVENT_TYPE, "FAIL"]

    block_payload = _decode(events[2]["payload"])
    assert block_payload["pattern_matched"] == "rm-rf-root"
    assert block_payload["task_id"] == TASK_ID
    assert block_payload["plan_id"] == PLAN_ID

    fail_payload = _decode(events[3]["payload"])
    assert fail_payload["reason"] == SHELL_COMMAND_FAIL_REASON
