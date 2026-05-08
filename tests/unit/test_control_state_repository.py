from __future__ import annotations

import asyncpg

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import ControlState, TaskRepository

pytestmark = DOCKER_REQUIRED


async def test_get_control_state_bootstraps_unpaused_singleton(task_repo: TaskRepository) -> None:
    state = await task_repo.get_control_state()

    assert state == ControlState(
        id="global",
        paused=False,
        pause_reason=None,
        paused_by=None,
        paused_at=None,
        updated_at=state.updated_at,
    )
    assert await task_repo.is_workers_paused() is False


async def test_pause_and_resume_workers_persist_global_state(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    paused = await task_repo.pause_workers(reason="deploy gate", operator="lead@example.com")

    assert paused.id == "global"
    assert paused.paused is True
    assert paused.pause_reason == "deploy gate"
    assert paused.paused_by == "lead@example.com"
    assert paused.paused_at is not None
    assert await task_repo.is_workers_paused() is True

    async with db_pool.acquire() as conn:
        persisted = await conn.fetchrow("SELECT paused, pause_reason, paused_by FROM control_state WHERE id = 'global'")
    assert persisted is not None
    assert persisted["paused"] is True
    assert persisted["pause_reason"] == "deploy gate"
    assert persisted["paused_by"] == "lead@example.com"

    resumed = await task_repo.resume_workers(operator="lead@example.com")

    assert resumed.paused is False
    assert resumed.pause_reason is None
    assert resumed.paused_by is None
    assert resumed.paused_at is None
    assert await task_repo.is_workers_paused() is False
