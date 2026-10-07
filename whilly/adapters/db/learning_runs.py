"""PostgreSQL adapter for disabled-by-default daily research schedules and runs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any
import asyncpg

from whilly.swarm.learning.schedules import ResearchRun, Schedule

_TERMINAL_OUTCOMES = {"completed", "failed", "cancelled", "timed_out"}
_RUN_TERMINAL = {"completed", "blocked", "skipped", "partial_failure", "stopped"}


class LearningRunBlocked(RuntimeError):
    """A named durable guard prevented a new model call."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PostgresLearningRunStore:
    """Product-pinned persistence; callers own the transaction for call methods."""

    def __init__(self, pool: asyncpg.Pool, *, product_id: str) -> None:
        if not isinstance(product_id, str) or not product_id.strip():
            raise ValueError("product_id must be non-empty")
        self.pool = pool
        self.product_id = product_id

    async def save_schedule(self, schedule: Schedule) -> None:
        """Persist a validated schedule; activation remains caller-controlled and explicit."""

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO swarm_learning_schedules
                    (product_id,id,timezone,run_window,enabled,model_profiles,limits,source_policy,retention_policy)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9::jsonb)
                ON CONFLICT (product_id,id) DO UPDATE SET
                    timezone=EXCLUDED.timezone, run_window=EXCLUDED.run_window,
                    enabled=EXCLUDED.enabled, model_profiles=EXCLUDED.model_profiles,
                    limits=EXCLUDED.limits, source_policy=EXCLUDED.source_policy,
                    retention_policy=EXCLUDED.retention_policy, updated_at=NOW()
                """,
                self.product_id,
                schedule.id,
                schedule.timezone,
                schedule.run_window,
                schedule.enabled,
                _json(schedule.model_profiles),
                _json(schedule.limits.__dict__),
                _json(schedule.source_policy),
                _json(schedule.retention_policy),
            )

    async def claim_due(self, schedule: Schedule, *, window_date, window_key: str, now: datetime) -> ResearchRun | None:
        """Claim only the current local window; conflict means an existing run/orphan, never reclaim it."""

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not schedule.enabled or schedule.activation_blockers():
            return None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"learning-run:{self.product_id}:{schedule.id}:{window_date.isoformat()}:{window_key}",
                )
                stored = await conn.fetchrow(
                    """SELECT enabled, timezone, run_window, model_profiles, limits, source_policy, retention_policy
                    FROM swarm_learning_schedules WHERE product_id=$1 AND id=$2 FOR SHARE""",
                    self.product_id,
                    schedule.id,
                )
                if stored is None or not stored["enabled"]:
                    return None
                run_id = str(uuid.uuid4())
                limits = schedule.limits
                row = await conn.fetchrow(
                    """INSERT INTO swarm_learning_runs
                    (id, product_id, schedule_id, window_date, window_key, lease_expires_at,
                     max_calls, max_seconds, max_documents, max_bytes)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (product_id, schedule_id, window_date, window_key) DO NOTHING
                    RETURNING id, product_id, schedule_id, window_date, window_key, status, blocker""",
                    run_id,
                    self.product_id,
                    schedule.id,
                    window_date,
                    window_key,
                    now + timedelta(seconds=limits.max_seconds),
                    limits.max_calls,
                    limits.max_seconds,
                    limits.max_documents,
                    limits.max_bytes,
                )
        return _run(row) if row is not None else None

    async def reserve_call(self, conn: Any, run_id: str, *, admission_id: int, seconds: int) -> int:
        """Reserve call count/time under the caller's global-admission transaction.

        Requested timeout seconds are reserved conservatively before model launch. Actual
        provider cost is not inferred and reservations are never automatically refunded.
        """

        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
            raise ValueError("seconds must be positive")
        row = await conn.fetchrow(
            """SELECT status, stop_requested, lease_expires_at, call_count, reserved_seconds,
                      max_calls, max_seconds
               FROM swarm_learning_runs WHERE id=$1 AND product_id=$2 FOR UPDATE""",
            run_id,
            self.product_id,
        )
        if row is None:
            raise LearningRunBlocked("run_not_found")
        if row["status"] in _RUN_TERMINAL:
            raise LearningRunBlocked(f"run_{row['status']}")
        if row["stop_requested"]:
            raise LearningRunBlocked("stop_requested")
        if row["lease_expires_at"] <= datetime.now(row["lease_expires_at"].tzinfo):
            raise LearningRunBlocked("ambiguous_orphan_lease")
        if row["call_count"] >= row["max_calls"]:
            raise LearningRunBlocked("max_calls_exhausted")
        if row["reserved_seconds"] + seconds > row["max_seconds"]:
            raise LearningRunBlocked("max_seconds_exhausted")
        admitted = await conn.fetchval(
            "SELECT 1 FROM swarm_model_admissions WHERE id=$1 AND released_at IS NULL",
            admission_id,
        )
        if admitted is None:
            raise LearningRunBlocked("admission_not_active")
        call_id = await conn.fetchval(
            """INSERT INTO swarm_learning_run_calls
                (product_id, run_id, admission_id, reserved_seconds)
                VALUES ($1,$2,$3,$4) RETURNING id""",
            self.product_id,
            run_id,
            admission_id,
            seconds,
        )
        await conn.execute(
            """UPDATE swarm_learning_runs SET call_count=call_count+1,
                reserved_seconds=reserved_seconds+$2 WHERE id=$1 AND product_id=$3""",
            run_id,
            seconds,
            self.product_id,
        )
        return int(call_id)

    async def finish_call(self, conn: Any, call_id: int, *, outcome: str) -> None:
        """Finish one call with a bounded, visible outcome; no budget refund is performed."""

        if outcome not in _TERMINAL_OUTCOMES:
            raise ValueError("outcome is not a bounded terminal state")
        row = await conn.fetchrow(
            """SELECT c.status, c.outcome, c.run_id FROM swarm_learning_run_calls c
               JOIN swarm_learning_runs r ON r.id=c.run_id AND r.product_id=c.product_id
               WHERE c.id=$1 AND c.product_id=$2 FOR UPDATE""",
            call_id,
            self.product_id,
        )
        if row is None:
            raise LearningRunBlocked("call_not_found")
        if row["status"] == "finished":
            if row["outcome"] != outcome:
                raise LearningRunBlocked("call_already_finished")
            return
        await conn.execute(
            """UPDATE swarm_learning_run_calls
               SET status='finished', outcome=$3, finished_at=NOW()
               WHERE id=$1 AND product_id=$2""",
            call_id,
            self.product_id,
            outcome,
        )
        if outcome in {"failed", "timed_out"}:
            await conn.execute(
                """UPDATE swarm_learning_runs SET status=$3, blocker=$4, finished_at=NOW()
                   WHERE id=$1 AND product_id=$2 AND status NOT IN ('completed','stopped')""",
                row["run_id"],
                self.product_id,
                "partial_failure",
                outcome,
            )
        elif outcome == "cancelled":
            await conn.execute(
                """UPDATE swarm_learning_runs SET status='stopped', blocker='cancelled', finished_at=NOW()
                   WHERE id=$1 AND product_id=$2 AND status NOT IN ('completed','stopped')""",
                row["run_id"],
                self.product_id,
            )

    async def stop(self, run_id: str, *, reason: str = "stop_requested") -> bool:
        """Atomically prevent future reservations; this does not kill an in-flight process."""

        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE swarm_learning_runs SET stop_requested=TRUE, status='stopped', blocker=$3,
                   finished_at=COALESCE(finished_at,NOW())
                   WHERE id=$1 AND product_id=$2 AND status NOT IN ('completed','stopped')""",
                run_id,
                self.product_id,
                reason,
            )
        return result.endswith("1")


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), separators=(",", ":"), ensure_ascii=False)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _run(row: Any) -> ResearchRun:
    return ResearchRun(
        id=row["id"],
        product_id=row["product_id"],
        schedule_id=row["schedule_id"],
        window_date=row["window_date"],
        window_key=row["window_key"],
        status=row["status"],
        blocker=row["blocker"],
    )


__all__ = ["LearningRunBlocked", "PostgresLearningRunStore"]
