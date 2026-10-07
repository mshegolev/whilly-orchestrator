"""PostgreSQL persistence for swarm-specific state (migration 029).

Queue state lives in Whilly's ``plans`` / ``tasks`` / ``events`` tables and
moves through :class:`whilly.adapters.db.repository.TaskRepository`
(claim/start/complete/fail/release with version guards). This module owns
only swarm metadata plus three queue operations that must be atomic with
that metadata:

* applying a plan revision (all task rows + context in one transaction);
* dependency gating via ``tasks.required_tags`` — every swarm task requires
  the session tag (so generic ``whilly run`` workers never claim it) and a
  task with unfinished dependencies additionally requires
  :data:`WAITING_TAG`, which no worker carries;
* explicit operator reruns of ``FAILED`` tasks.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

__all__ = [
    "COORDINATOR",
    "USER",
    "WAITING_TAG",
    "CoordinatorActiveError",
    "RevisionConflictError",
    "SwarmStore",
    "SwarmStoreError",
    "new_session_id",
    "plan_id_for",
    "session_tag",
    "task_id_for",
]

WAITING_TAG = "swarm:waiting"
USER = "user"
COORDINATOR = "coordinator"
RESERVED_PARTICIPANTS = frozenset({USER, COORDINATOR})
_MAX_MESSAGE = 8000


class SwarmStoreError(RuntimeError):
    pass


class CoordinatorActiveError(SwarmStoreError):
    pass


class RevisionConflictError(SwarmStoreError):
    pass


def new_session_id() -> str:
    return "s" + secrets.token_hex(5)


def plan_id_for(session_id: str) -> str:
    return f"swarm-{session_id}"


def session_tag(session_id: str) -> str:
    return f"swarm:{session_id}"


def task_id_for(session_id: str, revision: int, local_id: str) -> str:
    return f"{session_id}.r{revision}.{local_id}"


@dataclass(frozen=True)
class PreparedTask:
    local_id: str
    project_id: str
    role: str
    description: str
    depends_on: tuple[str, ...]
    verification: tuple[tuple[str, ...], ...]
    acceptance: tuple[str, ...]
    base_ref: str
    base_sha: str


_LIVE_LEASE = "(coordinator_id IS NOT NULL AND lease_expires_at > NOW())"

_SESSION_TASKS_SQL = """
SELECT t.id AS task_id, t.status, t.version, t.dependencies, t.required_tags, t.claimed_by,
       c.session_id, c.revision, c.local_id, c.project_id, c.role, c.depends_on, c.verification,
       c.base_ref, c.base_sha, c.max_attempts, c.outcome, c.blocker,
       t.description, t.acceptance_criteria
FROM swarm_task_context c
JOIN tasks t ON t.id = c.task_id
WHERE c.session_id = $1 {revision_filter}
ORDER BY c.revision, t.id
"""


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _record(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            value = float(value)
        out[key] = _jsonb(value) if key in _JSON_COLUMNS else value
    return out


_JSON_COLUMNS = {
    "plan",
    "registry_snapshot",
    "dependencies",
    "depends_on",
    "verification",
    "result",
    "review",
    "acceptance_criteria",
}


class SwarmStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ── sessions ────────────────────────────────────────────────────────
    async def create_session(self, session_id: str, *, registry_path: str, title: str = "") -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO swarm_sessions (id, title, registry_path) VALUES ($1, $2, $3) RETURNING *",
                session_id,
                title,
                registry_path,
            )
        return _record(row)  # type: ignore[return-value]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT *, {_LIVE_LEASE} AS coordinator_live FROM swarm_sessions WHERE id = $1", session_id
            )
        return _record(row)

    async def list_sessions(
        self, limit: int = 50, *, include_test: bool = False, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT *, {_LIVE_LEASE} AS coordinator_live FROM swarm_sessions "
                "WHERE ($2 OR NOT is_test) AND ($3 OR NOT archived) ORDER BY created_at DESC LIMIT $1",
                limit,
                include_test,
                include_archived,
            )
        return [_record(row) for row in rows]  # type: ignore[misc]

    async def set_session_flags(
        self, session_id: str, *, archived: bool | None = None, is_test: bool | None = None
    ) -> dict[str, Any] | None:
        """Only organize display; never cancel jobs or modify conversation history."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE swarm_sessions SET archived=COALESCE($2, archived), is_test=COALESCE($3, is_test) "
                "WHERE id=$1 RETURNING *",
                session_id,
                archived,
                is_test,
            )
        return _record(row)

    # ── conversation ────────────────────────────────────────────────────
    async def add_chat_message(self, session_id: str, sender: str, body: str) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO swarm_messages (session_id, kind, sender, body) VALUES ($1, 'chat', $2, $3) RETURNING id",
                session_id,
                sender,
                body,
            )

    async def chat_history(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM (
                    SELECT id, sender, body, created_at FROM swarm_messages
                    WHERE session_id = $1 AND kind = 'chat' ORDER BY id DESC LIMIT $2
                ) recent ORDER BY id
                """,
                session_id,
                limit,
            )
        return [_record(row) for row in rows]  # type: ignore[misc]

    # ── plan revisions ──────────────────────────────────────────────────
    async def add_revision(
        self,
        session_id: str,
        *,
        status: str,
        source: str,
        plan: dict[str, Any] | None,
        raw_text: str,
        error: str | None,
        registry_snapshot: dict[str, Any] | None,
    ) -> int:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchval("SELECT id FROM swarm_sessions WHERE id = $1 FOR UPDATE", session_id)
                if locked is None:
                    raise SwarmStoreError(f"unknown session {session_id!r}")
                revision = await conn.fetchval(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM swarm_plan_revisions WHERE session_id = $1",
                    session_id,
                )
                await conn.execute(
                    """
                    INSERT INTO swarm_plan_revisions
                        (session_id, revision, status, source, plan, raw_text, error, registry_snapshot)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb)
                    """,
                    session_id,
                    revision,
                    status,
                    source,
                    json.dumps(plan) if plan is not None else None,
                    raw_text,
                    error,
                    json.dumps(registry_snapshot) if registry_snapshot is not None else None,
                )
        return int(revision)

    async def get_revision(self, session_id: str, revision: int) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM swarm_plan_revisions WHERE session_id = $1 AND revision = $2", session_id, revision
            )
        return _record(row)

    async def list_revisions(self, session_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT session_id, revision, status, source, error, created_at, applied_at, plan "
                "FROM swarm_plan_revisions WHERE session_id = $1 ORDER BY revision",
                session_id,
            )
        return [_record(row) for row in rows]  # type: ignore[misc]

    async def latest_proposed_revision(self, session_id: str) -> int | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT MAX(revision) FROM swarm_plan_revisions WHERE session_id = $1 AND status = 'proposed'",
                session_id,
            )

    async def apply_revision(
        self,
        session_id: str,
        revision: int,
        tasks: list[PreparedTask],
        *,
        max_attempts: int,
        registry_snapshot: dict[str, Any],
    ) -> list[str]:
        """Atomically import a proposed revision into the Whilly queue.

        Refuses while a coordinator holds a live lease, while any session
        task is ``CLAIMED``/``IN_PROGRESS``, for non-``proposed`` revisions,
        and for revisions older than the applied one. Unfinished tasks of
        the previously applied revision are ``SKIPPED`` in the same
        transaction; ``DONE`` tasks keep their results.
        """
        plan_id = plan_id_for(session_id)
        tag = session_tag(session_id)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    f"SELECT *, {_LIVE_LEASE} AS live FROM swarm_sessions WHERE id = $1 FOR UPDATE", session_id
                )
                if session is None:
                    raise SwarmStoreError(f"unknown session {session_id!r}")
                if session["live"]:
                    raise CoordinatorActiveError(
                        f"session {session_id} has a running coordinator ({session['coordinator_id']}); "
                        "stop it before applying a new revision"
                    )
                rev = await conn.fetchrow(
                    "SELECT status FROM swarm_plan_revisions WHERE session_id = $1 AND revision = $2 FOR UPDATE",
                    session_id,
                    revision,
                )
                if rev is None:
                    raise RevisionConflictError(f"revision {revision} does not exist in session {session_id}")
                if rev["status"] != "proposed":
                    raise RevisionConflictError(
                        f"revision {revision} is {rev['status']}, only proposed revisions apply"
                    )
                applied = session["applied_revision"]
                if applied is not None and revision < applied:
                    raise RevisionConflictError(
                        f"revision {revision} is older than applied revision {applied}; propose a new revision"
                    )
                busy = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM swarm_task_context c JOIN tasks t ON t.id = c.task_id
                    WHERE c.session_id = $1 AND t.status IN ('CLAIMED', 'IN_PROGRESS')
                    """,
                    session_id,
                )
                if busy:
                    raise RevisionConflictError(
                        f"{busy} task(s) of session {session_id} are claimed or in progress; "
                        "stop and recover them before applying a new revision"
                    )
                await conn.execute(
                    "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                    plan_id,
                    f"swarm session {session_id}",
                )
                superseded = await conn.fetch(
                    """
                    UPDATE tasks t SET status = 'SKIPPED', version = t.version + 1, updated_at = NOW()
                    FROM swarm_task_context c
                    WHERE c.task_id = t.id AND c.session_id = $1 AND t.status = 'PENDING'
                    RETURNING t.id, t.version
                    """,
                    session_id,
                )
                # FAILED is terminal in the task FSM; keep the status and its
                # evidence, only mark the context as superseded.
                await conn.execute(
                    """
                    UPDATE swarm_task_context c SET outcome = 'superseded'
                    FROM tasks t WHERE t.id = c.task_id AND c.session_id = $1 AND t.status = 'FAILED'
                    """,
                    session_id,
                )
                for row in superseded:
                    await conn.execute(
                        "INSERT INTO events (task_id, event_type, payload) VALUES ($1, 'SKIP', $2::jsonb)",
                        row["id"],
                        json.dumps(
                            {"reason": "superseded_by_revision", "revision": revision, "version": row["version"]}
                        ),
                    )
                    await conn.execute(
                        "UPDATE swarm_task_context SET outcome = 'superseded' WHERE task_id = $1", row["id"]
                    )
                await conn.execute(
                    "UPDATE swarm_plan_revisions SET status = 'superseded' WHERE session_id = $1 AND status = 'applied'",
                    session_id,
                )
                inserted: list[str] = []
                for task in tasks:
                    task_id = task_id_for(session_id, revision, task.local_id)
                    deps = [task_id_for(session_id, revision, dep) for dep in task.depends_on]
                    tags = [tag, WAITING_TAG] if deps else [tag]
                    await conn.execute(
                        """
                        INSERT INTO tasks (id, plan_id, status, dependencies, key_files, priority, description,
                                           acceptance_criteria, test_steps, prd_requirement, version, required_tags)
                        VALUES ($1, $2, 'PENDING', $3::jsonb, '[]'::jsonb, 'medium', $4, $5::jsonb, $6::jsonb,
                                $7, 0, $8::text[])
                        """,
                        task_id,
                        plan_id,
                        json.dumps(deps),
                        task.description,
                        json.dumps(list(task.acceptance)),
                        json.dumps([" ".join(cmd) for cmd in task.verification]),
                        f"swarm {session_id} r{revision} {task.project_id}/{task.role}",
                        tags,
                    )
                    await conn.execute(
                        """
                        INSERT INTO swarm_task_context (task_id, session_id, revision, local_id, project_id, role,
                                                        depends_on, verification, base_ref, base_sha, max_attempts)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11)
                        """,
                        task_id,
                        session_id,
                        revision,
                        task.local_id,
                        task.project_id,
                        task.role,
                        json.dumps(list(task.depends_on)),
                        json.dumps([list(cmd) for cmd in task.verification]),
                        task.base_ref,
                        task.base_sha,
                        max_attempts,
                    )
                    await conn.execute(
                        "INSERT INTO events (task_id, plan_id, event_type, payload) VALUES ($1, $2, 'task.created', $3::jsonb)",
                        task_id,
                        plan_id,
                        json.dumps({"source": "swarm", "session": session_id, "revision": revision}),
                    )
                    inserted.append(task_id)
                await conn.execute(
                    "UPDATE swarm_plan_revisions SET status = 'applied', applied_at = NOW(), registry_snapshot = $3::jsonb "
                    "WHERE session_id = $1 AND revision = $2",
                    session_id,
                    revision,
                    json.dumps(registry_snapshot),
                )
                await conn.execute(
                    "UPDATE swarm_sessions SET plan_id = $2, applied_revision = $3, updated_at = NOW() WHERE id = $1",
                    session_id,
                    plan_id,
                    revision,
                )
        return inserted

    # ── coordinator lease ───────────────────────────────────────────────
    async def acquire_lease(
        self, session_id: str, coordinator_id: str, *, host: str, pid: int, lease_seconds: int
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE swarm_sessions
                SET coordinator_id = $2, coordinator_host = $3, coordinator_pid = $4,
                    lease_expires_at = NOW() + make_interval(secs => $5), stop_requested = FALSE,
                    status = 'running', updated_at = NOW()
                WHERE id = $1 AND NOT {_LIVE_LEASE}
                RETURNING id
                """,
                session_id,
                coordinator_id,
                host,
                pid,
                float(lease_seconds),
            )
        return row is not None

    async def renew_lease(self, session_id: str, coordinator_id: str, lease_seconds: int) -> tuple[bool, bool]:
        """Return ``(still_owner, stop_requested)``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE swarm_sessions SET lease_expires_at = NOW() + make_interval(secs => $3), updated_at = NOW()
                WHERE id = $1 AND coordinator_id = $2
                RETURNING stop_requested
                """,
                session_id,
                coordinator_id,
                float(lease_seconds),
            )
        if row is None:
            return False, True
        return True, bool(row["stop_requested"])

    async def release_lease(self, session_id: str, coordinator_id: str, *, status: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE swarm_sessions
                SET coordinator_id = NULL, coordinator_host = NULL, coordinator_pid = NULL,
                    lease_expires_at = NULL, stop_requested = FALSE, status = $3, updated_at = NOW()
                WHERE id = $1 AND coordinator_id = $2
                """,
                session_id,
                coordinator_id,
                status,
            )

    async def request_stop(self, session_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE swarm_sessions
                SET stop_requested = {_LIVE_LEASE},
                    status = CASE WHEN {_LIVE_LEASE} THEN 'stopping' ELSE 'stopped' END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *, {_LIVE_LEASE} AS coordinator_live
                """,
                session_id,
            )
        return _record(row)

    # ── tasks ───────────────────────────────────────────────────────────
    async def session_tasks(self, session_id: str, *, revision: int | None = None) -> list[dict[str, Any]]:
        sql = _SESSION_TASKS_SQL.format(revision_filter="AND c.revision = $2" if revision is not None else "")
        args: list[Any] = [session_id] if revision is None else [session_id, revision]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [_record(row) for row in rows]  # type: ignore[misc]

    async def task_context(self, task_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT c.*, t.description, t.acceptance_criteria, t.status, t.version "
                "FROM swarm_task_context c JOIN tasks t ON t.id = c.task_id WHERE c.task_id = $1",
                task_id,
            )
        return _record(row)

    async def unblock_ready(self, session_id: str) -> list[str]:
        """Drop :data:`WAITING_TAG` from pending tasks whose dependencies are all ``DONE``."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE tasks t SET required_tags = array_remove(t.required_tags, $2), updated_at = NOW()
                FROM swarm_task_context c
                WHERE c.task_id = t.id AND c.session_id = $1 AND t.status = 'PENDING'
                  AND $2 = ANY(t.required_tags)
                  AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(t.dependencies) dep(id)
                      LEFT JOIN tasks d ON d.id = dep.id
                      WHERE d.status IS DISTINCT FROM 'DONE'
                         OR NOT EXISTS (SELECT 1 FROM swarm_task_attempts a
                                        WHERE a.task_id=d.id AND a.status='accepted')
                  )
                RETURNING t.id
                """,
                session_id,
                WAITING_TAG,
            )
        return [row["id"] for row in rows]

    async def refresh_claims(self, task_ids: list[str], worker_id: str) -> None:
        """Keep live swarm claims fresh so a shared visibility sweep does not release them."""
        if not task_ids:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET claimed_at = NOW() WHERE id = ANY($1::text[]) AND claimed_by = $2 "
                "AND status IN ('CLAIMED', 'IN_PROGRESS')",
                task_ids,
                worker_id,
            )

    async def set_outcome(self, task_id: str, outcome: str | None, blocker: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE swarm_task_context SET outcome = $2, blocker = $3 WHERE task_id = $1", task_id, outcome, blocker
            )

    async def rerun_task(self, session_id: str, local_id: str) -> str:
        """Explicitly return a ``FAILED`` task of the applied revision to ``PENDING``."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    "SELECT applied_revision FROM swarm_sessions WHERE id = $1 FOR UPDATE", session_id
                )
                if session is None or session["applied_revision"] is None:
                    raise SwarmStoreError(f"session {session_id!r} has no applied revision")
                task_id = task_id_for(session_id, session["applied_revision"], local_id)
                row = await conn.fetchrow(
                    """
                    UPDATE tasks t
                    SET status = 'PENDING', claimed_by = NULL, claimed_at = NULL, version = t.version + 1,
                        updated_at = NOW(),
                        required_tags = CASE WHEN jsonb_array_length(t.dependencies) > 0
                                             THEN array_append(array_remove(t.required_tags, $2), $2)
                                             ELSE t.required_tags END
                    WHERE t.id = $1 AND t.status = 'FAILED'
                    RETURNING t.version
                    """,
                    task_id,
                    WAITING_TAG,
                )
                if row is None:
                    raise RevisionConflictError(f"task {local_id!r} is not FAILED in the applied revision")
                attempts = await self._counted_attempts(conn, task_id)
                await conn.execute(
                    "UPDATE swarm_task_context SET max_attempts = GREATEST(max_attempts, $2), outcome = NULL, "
                    "blocker = NULL WHERE task_id = $1",
                    task_id,
                    attempts + 1,
                )
                await conn.execute(
                    "INSERT INTO events (task_id, event_type, payload) VALUES ($1, 'RESET', $2::jsonb)",
                    task_id,
                    json.dumps({"reason": "swarm_rerun", "version": row["version"]}),
                )
        return task_id

    async def reserve_agent_call(
        self,
        session_id: str,
        *,
        engine: str,
        mode: str,
        task_id: str | None = None,
        prompt_chars: int,
        max_calls: int,
    ) -> int:
        """Reserve before spawning; concurrent callers cannot overspend the call cap."""
        if max_calls < 1 or prompt_chars < 0:
            raise SwarmStoreError("invalid call budget or prompt size")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if not await conn.fetchval("SELECT id FROM swarm_sessions WHERE id=$1 FOR UPDATE", session_id):
                    raise SwarmStoreError("unknown session")
                from whilly.swarm.product_workflow import check_feature_budget

                feature_cap = await check_feature_budget(conn, session_id, mode)
                if feature_cap is not None:
                    max_calls = min(max_calls, feature_cap)
                count = await conn.fetchval("SELECT count(*) FROM swarm_agent_calls WHERE session_id=$1", session_id)
                if count >= max_calls:
                    raise SwarmStoreError("agent_call_budget_exhausted")
                if task_id is not None and not await conn.fetchval(
                    "SELECT 1 FROM swarm_task_context WHERE task_id=$1 AND session_id=$2", task_id, session_id
                ):
                    raise SwarmStoreError("task does not belong to session")
                return int(
                    await conn.fetchval(
                        "INSERT INTO swarm_agent_calls(session_id,engine,mode,task_id,prompt_chars) "
                        "VALUES($1,$2,$3,$4,$5) RETURNING id",
                        session_id,
                        engine,
                        mode,
                        task_id,
                        prompt_chars,
                    )
                )

    async def finish_agent_call(
        self,
        call_id: int,
        *,
        usage: dict | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE swarm_agent_calls SET usage=$2::jsonb,cost_usd=$3,error=$4,finished_at=NOW() "
                "WHERE id=$1 AND finished_at IS NULL",
                call_id,
                json.dumps(usage) if usage is not None else None,
                Decimal(str(cost_usd)) if cost_usd is not None else None,
                error,
            )

    async def agent_usage(self, session_id: str) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT usage,cost_usd FROM swarm_agent_calls WHERE session_id=$1", session_id)
        summary = dict(
            calls=len(rows),
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=0,
            known_cost_usd=0.0,
            unknown_cost_calls=0,
        )
        token_keys = ("input_tokens", "output_tokens", "cached_input_tokens")
        for key in token_keys:
            summary[f"known_{key}"] = 0
            summary[f"unknown_{key}_calls"] = 0
        for row in rows:
            usage = row["usage"] or {}
            if isinstance(usage, str):
                usage = json.loads(usage)
            for key in token_keys:
                value = usage.get(key)
                if value is None:
                    summary[f"unknown_{key}_calls"] += 1
                else:
                    summary[f"known_{key}"] += int(value)
            if row["cost_usd"] is None:
                summary["unknown_cost_calls"] += 1
            else:
                summary["known_cost_usd"] += float(row["cost_usd"])
        for key in token_keys:
            summary[key] = None if summary[f"unknown_{key}_calls"] else summary[f"known_{key}"]
        return summary

    async def accept_task(
        self,
        repo: Any,
        task_id: str,
        version: int,
        attempt_id: int,
        *,
        head_sha: str,
        result: dict,
        verification: list[dict],
        review: dict,
        cost_usd: float | None = 0,
    ) -> None:
        """Commit queue completion and its acceptance evidence in one transaction."""
        if (
            not head_sha
            or review.get("verdict") != "approve"
            or not verification
            or not all(
                item.get("passed") is True
                and (item.get("evidence") is None or item.get("evidence", {}).get("outcome") == "passed")
                for item in verification
            )
        ):
            raise SwarmStoreError("acceptance requires head, passing checks and approved review")

        async def persist(conn: asyncpg.Connection) -> None:
            changed = await conn.fetchval(
                "UPDATE swarm_task_attempts SET status='accepted',head_sha=$3,result=$4::jsonb,"
                "verification=$5::jsonb,review=$6::jsonb,cost_usd=$7,agent_pgid=NULL,finished_at=NOW() "
                "WHERE id=$1 AND task_id=$2 AND status='running' RETURNING id",
                attempt_id,
                task_id,
                head_sha,
                json.dumps(result),
                json.dumps(verification),
                json.dumps(review),
                Decimal(str(cost_usd)) if cost_usd is not None else None,
            )
            if changed is None:
                raise SwarmStoreError("acceptance requires matching running attempt")
            await conn.execute(
                "UPDATE swarm_task_context SET outcome='accepted',blocker=NULL WHERE task_id=$1", task_id
            )

        await repo.complete_task(task_id, version, cost_usd=cost_usd or 0, on_complete=persist)

    # ── attempts ────────────────────────────────────────────────────────
    @staticmethod
    async def _counted_attempts(conn: asyncpg.Connection, task_id: str) -> int:
        return int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM swarm_task_attempts WHERE task_id = $1 AND status <> 'cancelled'", task_id
            )
        )

    async def counted_attempts(self, task_id: str) -> int:
        async with self._pool.acquire() as conn:
            return await self._counted_attempts(conn, task_id)

    async def start_attempt(
        self,
        task_id: str,
        *,
        coordinator_id: str,
        host: str,
        worktree_path: str,
        branch: str,
        log_dir: str,
        base_sha: str,
    ) -> tuple[int, int]:
        """Insert a running attempt; returns ``(attempt_row_id, attempt_number)``."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task = await conn.fetchrow("SELECT status FROM tasks WHERE id=$1 FOR UPDATE", task_id)
                if task is None or task["status"] != "IN_PROGRESS":
                    raise SwarmStoreError("attempt requires an in-progress task")
                if await conn.fetchval(
                    "SELECT 1 FROM swarm_task_attempts WHERE task_id=$1 AND status='running'", task_id
                ):
                    raise SwarmStoreError("task already has a running attempt")
                number = await conn.fetchval(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 FROM swarm_task_attempts WHERE task_id = $1", task_id
                )
                row_id = await conn.fetchval(
                    """
                    INSERT INTO swarm_task_attempts (task_id, attempt, coordinator_id, host, worktree_path, branch,
                                                     log_dir, base_sha)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id
                    """,
                    task_id,
                    number,
                    coordinator_id,
                    host,
                    worktree_path,
                    branch,
                    log_dir,
                    base_sha,
                )
        return int(row_id), int(number)

    async def next_attempt_number(self, task_id: str) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 FROM swarm_task_attempts WHERE task_id = $1", task_id
                )
            )

    async def set_attempt_pgid(self, attempt_id: int, pgid: int | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE swarm_task_attempts SET agent_pgid = $2 WHERE id = $1", attempt_id, pgid)

    async def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        head_sha: str | None = None,
        result: dict[str, Any] | None = None,
        verification: list[dict[str, Any]] | None = None,
        review: dict[str, Any] | None = None,
        error: str | None = None,
        cost_usd: float | None = 0.0,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE swarm_task_attempts
                SET status = $2, head_sha = $3, result = $4::jsonb, verification = $5::jsonb, review = $6::jsonb,
                    error = $7, cost_usd = $8, agent_pgid = NULL, finished_at = NOW()
                WHERE id = $1
                """,
                attempt_id,
                status,
                head_sha,
                json.dumps(result) if result is not None else None,
                json.dumps(verification) if verification is not None else None,
                json.dumps(review) if review is not None else None,
                error,
                Decimal(str(round(cost_usd, 6))) if cost_usd is not None else None,
            )

    async def running_attempts(self, session_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.* FROM swarm_task_attempts a JOIN swarm_task_context c ON c.task_id = a.task_id
                WHERE c.session_id = $1 AND a.status = 'running' ORDER BY a.id
                """,
                session_id,
            )
        return [_record(row) for row in rows]  # type: ignore[misc]

    async def attempts_for_session(self, session_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.*, c.local_id, c.revision FROM swarm_task_attempts a
                JOIN swarm_task_context c ON c.task_id = a.task_id
                WHERE c.session_id = $1 ORDER BY c.revision, c.local_id, a.attempt
                """,
                session_id,
            )
        return [_record(row) for row in rows]  # type: ignore[misc]

    async def dependency_packets(self, task_id: str) -> list[dict[str, Any]]:
        """Accepted results of ``task_id``'s dependencies, for prompt hydration."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (dc.task_id) dc.local_id, dc.project_id, dc.role, a.branch, a.worktree_path,
                       a.base_sha, a.head_sha, a.result, a.verification
                FROM tasks t
                CROSS JOIN LATERAL jsonb_array_elements_text(t.dependencies) dep(id)
                JOIN swarm_task_context dc ON dc.task_id = dep.id
                JOIN swarm_task_attempts a ON a.task_id = dep.id AND a.status = 'accepted'
                WHERE t.id = $1
                ORDER BY dc.task_id, a.attempt DESC
                """,
                task_id,
            )
        packets = []
        for row in rows:
            record = _record(row) or {}
            result = record.get("result") or {}
            packets.append(
                {
                    "task": record["local_id"],
                    "project": record["project_id"],
                    "role": record["role"],
                    "branch": record["branch"],
                    "worktree": record["worktree_path"],
                    "base_sha": record["base_sha"],
                    "head_sha": record["head_sha"],
                    "summary": result.get("summary", ""),
                    "touched_files": result.get("touched_files", []),
                    "notes": result.get("notes", ""),
                    "verification": [
                        {"argv": v.get("argv"), "exit_code": v.get("exit_code")}
                        for v in (record.get("verification") or [])
                    ],
                }
            )
        return packets

    # ── addressed messages ──────────────────────────────────────────────
    async def participants(self, session_id: str) -> set[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.local_id FROM swarm_task_context c JOIN swarm_sessions s ON s.id = c.session_id
                WHERE c.session_id = $1 AND c.revision = s.applied_revision
                """,
                session_id,
            )
        return {row["local_id"] for row in rows} | set(RESERVED_PARTICIPANTS)

    async def send_message(
        self, session_id: str, *, sender: str, recipient: str, body: str, task_ref: str | None = None
    ) -> int:
        if not body.strip():
            raise SwarmStoreError("message body must not be empty")
        if len(body) > _MAX_MESSAGE:
            raise SwarmStoreError(f"message body exceeds {_MAX_MESSAGE} characters")
        if await self.get_session(session_id) is None:
            raise SwarmStoreError(f"unknown session {session_id!r}")
        known = await self.participants(session_id)
        for label, value in (("sender", sender), ("recipient", recipient)):
            if value not in known:
                raise SwarmStoreError(f"unknown {label} {value!r} in session {session_id}")
        if sender == recipient:
            raise SwarmStoreError("sender and recipient must differ")
        if task_ref is not None and task_ref not in known - RESERVED_PARTICIPANTS:
            raise SwarmStoreError(f"unknown task {task_ref!r} in session {session_id}")
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO swarm_messages (session_id, kind, sender, recipient, task_ref, body)
                VALUES ($1, 'peer', $2, $3, $4, $5) RETURNING id
                """,
                session_id,
                sender,
                recipient,
                task_ref,
                body,
            )

    async def inbox(self, session_id: str, recipient: str, *, include_acked: bool = False) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, sender, recipient, task_ref, body, created_at, delivered_at, acked_at
                FROM swarm_messages
                WHERE session_id = $1 AND kind = 'peer' AND recipient = $2 AND ($3 OR acked_at IS NULL)
                ORDER BY id
                """,
                session_id,
                recipient,
                include_acked,
            )
        return [_record(row) for row in rows]  # type: ignore[misc]

    async def mark_delivered(self, message_ids: list[int], *, session_id: str, recipient: str) -> None:
        if not message_ids:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE swarm_messages SET delivered_at = COALESCE(delivered_at, NOW()) "
                "WHERE id = ANY($1::bigint[]) AND session_id=$2 AND recipient=$3 AND kind='peer'",
                message_ids,
                session_id,
                recipient,
            )

    async def ack_messages(self, session_id: str, recipient: str, message_ids: list[int]) -> list[int]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                owned = await conn.fetch(
                    "SELECT id FROM swarm_messages WHERE session_id = $1 AND kind = 'peer' AND recipient = $2 "
                    "AND id = ANY($3::bigint[])",
                    session_id,
                    recipient,
                    message_ids,
                )
                owned_ids = {row["id"] for row in owned}
                foreign = sorted(set(message_ids) - owned_ids)
                if foreign:
                    raise SwarmStoreError(f"messages {foreign} are not addressed to {recipient!r} in {session_id}")
                rows = await conn.fetch(
                    "UPDATE swarm_messages SET acked_at = NOW(), delivered_at = COALESCE(delivered_at, NOW()) "
                    "WHERE id = ANY($1::bigint[]) AND acked_at IS NULL RETURNING id",
                    message_ids,
                )
        return sorted(row["id"] for row in rows)
