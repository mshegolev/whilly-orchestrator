from __future__ import annotations

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401,F811
from whilly.swarm import admission

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_stale_active_rows_fail_closed_without_reclamation(db_pool, monkeypatch):  # noqa: F811
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("TRUNCATE swarm_model_admissions")
            await conn.executemany(
                "INSERT INTO swarm_model_admissions (advisory_key, owner_host, owner_pid) VALUES ($1, $2, $3)",
                [
                    (1, "dead-host", 1),
                    (2, "dead-host", 2),
                    (3, "dead-host", 3),
                    (4, "dead-host", 4),
                    (5, "dead-host", 5),
                ],
            )

        async def forbidden(*args, **kwargs):
            raise AssertionError("run_engine must not be called")

        monkeypatch.setattr(admission, "run_engine", forbidden)
        with pytest.raises(admission.ModelCapacityExhausted, match="model_capacity_exhausted"):
            await admission.admitted_run_engine(db_pool, "cheap", object())

        async with db_pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM swarm_model_admissions WHERE released_at IS NULL") == 5
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("TRUNCATE swarm_model_admissions")


@pytest.mark.asyncio
async def test_five_model_calls_run_concurrently_and_sixth_is_rejected(db_pool, monkeypatch):  # noqa: F811
    import asyncio

    await db_pool.execute("TRUNCATE swarm_model_admissions")
    entered = 0
    entered_all = asyncio.Event()
    release = asyncio.Event()
    guard = asyncio.Lock()

    async def fake_run_engine(*args, **kwargs):
        nonlocal entered
        async with guard:
            entered += 1
            if entered == 5:
                entered_all.set()
        await release.wait()
        return "ok"

    monkeypatch.setattr(admission, "run_engine", fake_run_engine)
    calls = [asyncio.create_task(admission.admitted_run_engine(db_pool, "cheap", object())) for _ in range(5)]
    try:
        await asyncio.wait_for(entered_all.wait(), timeout=2)
        sixth = asyncio.create_task(admission.admitted_run_engine(db_pool, "cheap", object()))
        with pytest.raises(admission.ModelCapacityExhausted):
            await sixth
        release.set()
        assert await asyncio.gather(*calls) == ["ok"] * 5
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        await db_pool.execute("TRUNCATE swarm_model_admissions")
