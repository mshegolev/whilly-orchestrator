"""Durable global admission for every model-engine subprocess.

``admitted_run_engine`` is the integration seam for all existing callers.
The advisory lock is held only for the short transaction that counts active
rows and inserts a reservation. The model subprocess runs after that
transaction, so five admitted calls can run concurrently. Active rows are
never reclaimed: if a process dies before releasing its row, admission fails
closed until an operator repairs the durable state.
"""

from __future__ import annotations

import os
import socket
from typing import Any

from whilly.swarm.agent import run_engine

__all__ = ["MAX_ACTIVE_MODEL_CALLS", "ModelCapacityExhausted", "admitted_run_engine"]

MAX_ACTIVE_MODEL_CALLS = 5
_ADVISORY_LOCK_KEY = 0x5748494C4C595F4D


class ModelCapacityExhausted(RuntimeError):
    """The global model-call admission limit is occupied."""

    def __init__(self) -> None:
        super().__init__("model_capacity_exhausted")


async def admitted_run_engine(pool: Any, *args: Any, **kwargs: Any) -> Any:
    """Admit one model call, delegate to ``agent.run_engine``, and release it.

    ``pool`` is intentionally explicit so the caller owns configuration and
    credentials. No fallback, queue, stale-row reclamation, or model-budget
    policy is performed here.
    """
    owner_host = socket.gethostname()
    owner_pid = os.getpid()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY)
            active = await connection.fetchval("SELECT COUNT(*) FROM swarm_model_admissions WHERE released_at IS NULL")
            if active >= MAX_ACTIVE_MODEL_CALLS:
                raise ModelCapacityExhausted()
            row_id = await connection.fetchval(
                """
                INSERT INTO swarm_model_admissions (advisory_key, owner_host, owner_pid)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                _ADVISORY_LOCK_KEY,
                owner_host,
                owner_pid,
            )

    try:
        return await run_engine(*args, **kwargs)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE swarm_model_admissions SET released_at = NOW() WHERE id = $1",
                row_id,
            )
