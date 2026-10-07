"""Swarm conversation service and session coordinator.

:class:`SwarmService` owns the durable conversation: user messages, planner
replies and revisioned plans. Discussion never starts work; only
:meth:`SwarmService.apply_revision` followed by :class:`Coordinator.run`
(``whilly swarm run`` or ``/run`` in chat) does.

:class:`Coordinator` is the single executor for one session. It holds a
database lease (a second coordinator for the same session is refused),
claims tasks through :class:`TaskRepository` with a session-scoped worker
tag, runs each task in a fresh worktree with a bounded agent subprocess,
executes verification commands itself, asks an independent read-only
reviewer, and only then completes the task. There are no automatic retries
of failed tasks; recovery of tasks abandoned by a crashed coordinator is
bounded by ``limits.max_attempts``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import secrets
import shlex
import socket
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg

from whilly.adapters.db.repository import TaskRepository, VersionConflictError
from whilly.adapters.filesystem.swarm_workspace import materialize_candidate
from whilly.core.models import Task
from whilly.core.swarm_execution import ExecutionBlocked, ExecutionPolicy
from whilly.swarm import gitops
from whilly.swarm.admission import admitted_run_engine
from whilly.swarm.agent import (
    ensure_empty_mcp_config,
    process_group_alive,
)
from whilly.swarm.context import bounded_history
from whilly.swarm.execution import GuardedExecutor
from whilly.swarm.mailbox import MAILBOX_ENV, sync_mailbox
from whilly.swarm.plan import PlanError, SwarmPlan, extract_plan_json, validate_plan
from whilly.swarm.prompts import (
    ResultError,
    build_planner_prompt,
    build_reviewer_prompt,
    build_worker_prompt,
    parse_review,
    parse_worker_result,
)
from whilly.swarm.profiles import profile_engine, resolve_profile
from whilly.swarm.registry import Registry, RegistryError, load_registry, registry_from_dict
from whilly.swarm.verification import (
    VerificationError,
    host_project_execution_binding,
    protected_changes,
    require_project_verification,
)
from whilly.swarm.store import (
    WAITING_TAG,
    CoordinatorActiveError,
    PreparedTask,
    SwarmStore,
    SwarmStoreError,
    new_session_id,
    plan_id_for,
    session_tag,
)

__all__ = [
    "AGENT_OVERRIDE_ENV",
    "ChatReply",
    "Coordinator",
    "CoordinatorActiveError",
    "OrphanProcessError",
    "RunSummary",
    "SwarmService",
]

log = logging.getLogger(__name__)

AGENT_OVERRIDE_ENV = "WHILLY_SWARM_AGENT"
_VERIFY_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")
_TAIL = 4000
MAX_PROMPT_CHARS = 120_000


async def _await_blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run trusted blocking work without releasing its owner on cancellation.

    ``asyncio.to_thread`` cannot stop a running thread.  Shielding its task
    and waiting for it after coordinator cancellation prevents Git or
    materialization from mutating a candidate after the task has been
    released for restart.
    """
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(operation)
        except BaseException:
            pass
        raise


def _add_cost(total: float | None, value: float | None) -> float | None:
    """Preserve unknown cost; never turn an unknown backend price into zero."""
    if total is None or value is None:
        return None
    return total + value


def _require_prompt_budget(prompt: str, *, label: str) -> None:
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SwarmStoreError(f"{label} prompt exceeds {MAX_PROMPT_CHARS} characters; refusing to spawn")


class OrphanProcessError(SwarmStoreError):
    """A previous coordinator's agent process group is still alive."""


@dataclass(frozen=True)
class ChatReply:
    reply: str
    revision: int | None = None
    revision_status: str | None = None
    error: str | None = None


@dataclass
class RunSummary:
    session_id: str
    status: str
    accepted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "finished" and not self.failed and not self.blockers


def _tail(path: str | Path, limit: int = _TAIL) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _apply_agent_override(registry: Registry) -> Registry:
    override = os.environ.get(AGENT_OVERRIDE_ENV)
    if not override:
        return registry
    return dataclasses.replace(
        registry, agent=dataclasses.replace(registry.agent, executable=tuple(shlex.split(override)))
    )


def registry_from_snapshot(snapshot: dict[str, Any], *, project_ids: set[str] | None = None) -> Registry:
    bindings = snapshot.get("verification_bindings")
    if not isinstance(bindings, dict):
        raise RegistryError(["registry snapshot missing verification binding"])
    data = {key: value for key, value in snapshot.items() if not key.startswith("_") and key != "verification_bindings"}
    registry = registry_from_dict(data, check_git=False, source_path=snapshot.get("_source_path"))
    required_projects = set(registry.projects) if project_ids is None else set(project_ids)
    if set(bindings) != required_projects:
        raise RegistryError(["registry snapshot verification binding project set changed"])
    for project_id in required_projects:
        project = registry.projects.get(project_id)
        if project is None:
            raise RegistryError([f"registry snapshot binding references unknown project {project_id}"])
        binding = bindings.get(project_id)
        if not isinstance(binding, dict):
            raise RegistryError([f"registry snapshot missing verification binding for {project_id}"])
        try:
            expected = project.verification_policy.digest() if project.verification_policy else None
        except AttributeError:
            expected = None
        if (
            expected is None
            or binding.get("policy_digest") != expected
            or not binding.get("base_sha")
            or not binding.get("hook_digest")
        ):
            raise RegistryError([f"registry snapshot verification binding changed for {project_id}"])
    return _apply_agent_override(registry)


def default_messaging_cli() -> str:
    return f"{shlex.quote(sys.executable)} -m whilly swarm"


class SwarmService:
    def __init__(self, pool: asyncpg.Pool, executor: GuardedExecutor | None = None) -> None:
        self.pool = pool
        self.store = SwarmStore(pool)
        self.repo = TaskRepository(pool)
        self.executor = executor or GuardedExecutor()

    def _ensure_executor(self, registry: Registry) -> GuardedExecutor:
        if not self.executor.provisioning.toolchains:
            self.executor = GuardedExecutor.from_registry(registry)
        return self.executor

    def execution_policy(
        self,
        phase: str,
        cwd: Path,
        log_dir: Path,
        environment: dict[str, str],
        *,
        writable: bool,
        executor: GuardedExecutor | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecutionPolicy:
        toolchain_id = environment.get("WHILLY_SWARM_TOOLCHAIN_ID")
        executor = executor or self.executor
        if not toolchain_id:
            raise ExecutionBlocked("execution_toolchain_required")
        configured_roots, denied_roots = executor.roots(toolchain_id)
        home = Path(environment["HOME"]).resolve(strict=False)
        temporary = Path(environment["TMPDIR"]).resolve(strict=False)
        # macOS sandbox-exec needs read traversal through the two host-owned
        # parents of the per-launch temp directory.  They remain read-only;
        # child writes are still limited to ``temporary`` (and ``cwd`` only
        # for explicitly writable phases).
        temporary_parents = (temporary.parent, temporary.parent.parent)
        read_roots = tuple(
            dict.fromkeys((cwd.resolve(strict=False), home, temporary, *temporary_parents, *configured_roots))
        )
        write_roots: tuple[Path, ...] = (home, temporary)
        if writable:
            write_roots += (cwd.resolve(strict=False),)
            mailbox = environment.get(MAILBOX_ENV)
            if mailbox:
                write_roots += (Path(mailbox).resolve(strict=False),)
        protected = (cwd / ".git",) if writable and phase != "git" else ()
        return ExecutionPolicy(
            phase=phase,
            read_roots=tuple(str(path) for path in read_roots),
            write_roots=tuple(str(path) for path in write_roots),
            denied_roots=tuple(str(path.resolve(strict=False)) for path in denied_roots),
            protected_write_roots=tuple(str(path.resolve(strict=False)) for path in protected),
            network=phase in {"discussion", "planner", "escalation", "worker", "review"},
            timeout_seconds=timeout_seconds or 600,
            max_output_bytes=1024 * 1024,
        )

    # ── sessions / registry ────────────────────────────────────────────
    async def create_session(self, registry_path: str, *, title: str = "") -> str:
        registry = load_registry(registry_path)
        session_id = new_session_id()
        await self.store.create_session(session_id, registry_path=registry.source_path or registry_path, title=title)
        return session_id

    async def require_session(self, session_id: str) -> dict[str, Any]:
        session = await self.store.get_session(session_id)
        if session is None:
            raise SwarmStoreError(f"unknown session {session_id!r}")
        return session

    async def live_registry(self, session_id: str) -> Registry:
        session = await self.require_session(session_id)
        return _apply_agent_override(load_registry(session["registry_path"]))

    async def applied_registry(self, session_id: str) -> Registry | None:
        session = await self.require_session(session_id)
        if session["applied_revision"] is None:
            return None
        rev = await self.store.get_revision(session_id, session["applied_revision"])
        if rev is None or not rev.get("registry_snapshot"):
            return None
        project_ids = {task["project"] for task in rev.get("plan", {}).get("tasks", [])}
        return registry_from_snapshot(rev["registry_snapshot"], project_ids=project_ids)

    # ── conversation ───────────────────────────────────────────────────
    async def chat(
        self,
        session_id: str,
        text: str,
        *,
        request_plan: bool = False,
        registry_override: Registry | None = None,
        prompt_prefix: str = "",
        phase: str = "planner",
    ) -> ChatReply:
        """Record a user turn, ask the read-only planner, store its reply.

        A reply containing a plan block creates a new plan revision
        (``proposed`` or ``invalid`` with a named error). Nothing is queued.
        """
        from whilly.swarm.product_workflow import feature_for_session

        feature = await feature_for_session(self.pool, session_id)
        if registry_override is None:
            is_chief = False
            if feature is None:
                async with self.pool.acquire() as conn:
                    is_chief = bool(
                        await conn.fetchval("SELECT 1 FROM swarm_products WHERE chief_session_id=$1", session_id)
                    )
            if feature or is_chief:
                raise SwarmStoreError("product planning sessions require an explicit strong planner registry override")
        registry = registry_override or await self.live_registry(session_id)
        self._ensure_executor(registry)
        await self.store.add_chat_message(session_id, "user", text)
        history = bounded_history(await self.store.chat_history(session_id), max_chars=24000)
        prompt = prompt_prefix + build_planner_prompt(registry, history, request_plan=request_plan)
        try:
            _require_prompt_budget(prompt, label="planner")
        except SwarmStoreError as exc:
            await self.store.add_chat_message(session_id, "system", str(exc))
            return ChatReply(reply="", error=str(exc))
        state_dir = registry.resolved_state_dir()
        scratch = state_dir / "sessions" / session_id / "planner"
        scratch.mkdir(parents=True, exist_ok=True)
        turn = len(history)
        planner_engine = registry.agent.planner_engine
        call_id = await self.store.reserve_agent_call(
            session_id,
            engine=planner_engine,
            mode="planner",
            prompt_chars=len(prompt),
            max_calls=registry.limits.max_tasks * 3,
        )
        toolchain_id = self.executor.toolchain_for_engine(phase, planner_engine)
        env = self.executor.environment(toolchain_id=toolchain_id, phase=phase, attempt_root=scratch)
        policy = self.execution_policy(phase, scratch, scratch, env, writable=False)
        process, result = await admitted_run_engine(
            self.pool,
            planner_engine,
            registry.engines[planner_engine],
            mode="read_only",
            prompt=prompt,
            cwd=scratch,
            log_dir=scratch,
            name=f"turn-{turn:04d}",
            timeout_seconds=registry.agent.planner_timeout_seconds,
            max_turns=registry.agent.planner_max_turns,
            budget_usd=registry.agent.planner_budget_usd,
            empty_mcp_config=ensure_empty_mcp_config(state_dir),
            add_dirs=(),
            auth_mode=self.executor.provisioning.toolchains[toolchain_id].auth_mode,
            env=env,
            policy=policy,
            executor=self.executor,
        )
        await self.store.finish_agent_call(
            call_id,
            usage={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cached_input_tokens": result.usage.cache_read_tokens,
            },
            cost_usd=result.usage.cost_usd,
            error=result.error or (None if process.ok else process.describe()),
        )
        if not process.ok or result.error:
            error = f"planner_failed: {process.describe()}; {result.error or _tail(process.stderr_path, 500).strip()}"
            await self.store.add_chat_message(session_id, "system", error)
            return ChatReply(reply="", error=error)
        reply = result.output
        await self.store.add_chat_message(session_id, "planner", reply)
        try:
            data = extract_plan_json(reply)
        except PlanError as exc:
            revision = await self._store_invalid(session_id, registry, reply, str(exc), "planner")
            return ChatReply(reply=reply, revision=revision, revision_status="invalid", error=str(exc))
        if data is None:
            if request_plan:
                error = "planner_no_plan: reply did not contain a JSON plan block"
                await self.store.add_chat_message(session_id, "system", error)
                return ChatReply(reply=reply, error=error)
            return ChatReply(reply=reply)
        revision, status, error = await self.propose_plan(session_id, data, raw_text=reply, source="planner")
        return ChatReply(reply=reply, revision=revision, revision_status=status, error=error)

    async def _store_invalid(self, session_id: str, registry: Registry, raw: str, error: str, source: str) -> int:
        revision = await self.store.add_revision(
            session_id,
            status="invalid",
            source=source,
            plan=None,
            raw_text=raw,
            error=error,
            registry_snapshot=None,
        )
        await self.store.add_chat_message(session_id, "system", f"revision {revision} rejected: {error}")
        return revision

    async def propose_plan(
        self, session_id: str, data: Any, *, raw_text: str = "", source: str = "file"
    ) -> tuple[int, str, str | None]:
        registry = await self.live_registry(session_id)
        self._ensure_executor(registry)
        try:
            plan = validate_plan(data, registry)
        except PlanError as exc:
            revision = await self.store.add_revision(
                session_id,
                status="invalid",
                source=source,
                plan=data if isinstance(data, dict) else None,
                raw_text=raw_text or json.dumps(data)[:20000],
                error=str(exc),
                registry_snapshot=None,
            )
            await self.store.add_chat_message(session_id, "system", f"revision {revision} rejected: {exc}")
            return revision, "invalid", str(exc)
        revision = await self.store.add_revision(
            session_id,
            status="proposed",
            source=source,
            plan=plan.to_dict(),
            raw_text=raw_text,
            error=None,
            registry_snapshot=None,
        )
        await self.store.add_chat_message(
            session_id,
            "system",
            f"revision {revision} proposed with {len(plan.tasks)} task(s); run it explicitly to start work",
        )
        return revision, "proposed", None

    async def apply_revision(self, session_id: str, revision: int | None) -> tuple[int, list[str]]:
        """Re-validate against the current registry, resolve base SHAs, import atomically."""
        from whilly.swarm.product_workflow import require_feature_permission, require_plain_execution_binding
        from whilly.swarm.product_workflow import feature_for_session

        await require_feature_permission(self.pool, session_id, action="apply", revision=revision)
        feature = await feature_for_session(self.pool, session_id)
        if revision is None:
            revision = await self.store.latest_proposed_revision(session_id)
            if revision is None:
                raise SwarmStoreError(f"session {session_id} has no proposed revision to run")
        rev = await self.store.get_revision(session_id, revision)
        if rev is None:
            raise SwarmStoreError(f"revision {revision} does not exist in session {session_id}")
        if rev["status"] != "proposed":
            raise SwarmStoreError(f"revision {revision} is {rev['status']}; only proposed revisions can run")
        registry = await self.live_registry(session_id)
        plan: SwarmPlan = validate_plan(rev["plan"], registry)
        prepared: list[PreparedTask] = []
        project_ids = {task.project for task in plan.tasks}
        resolved_bases: dict[str, str] = {}
        verification_bindings: dict[str, dict[str, str]] = {}
        for project_id in sorted(project_ids):
            project = registry.projects[project_id]
            base_sha = await _await_blocking(gitops.resolve_commit, project.path, project.base_ref)
            try:
                verification_bindings[project_id] = await _await_blocking(
                    host_project_execution_binding, project, base_sha
                )
            except VerificationError as exc:
                raise SwarmStoreError(str(exc)) from exc
            resolved_bases[project_id] = base_sha
        by_id = {task.id: task for task in plan.tasks}
        for local_id in plan.topological_ids():
            task = by_id[local_id]
            project = registry.projects[task.project]
            base_sha = resolved_bases[project.id]
            prepared.append(
                PreparedTask(
                    local_id=task.id,
                    project_id=task.project,
                    role=task.role,
                    description=task.description,
                    depends_on=task.depends_on,
                    verification=task.verification,
                    acceptance=task.acceptance,
                    base_ref=project.base_ref,
                    base_sha=base_sha,
                )
            )
        snapshot = registry.to_snapshot()
        snapshot["_registry_hash"] = hashlib.sha256(Path(registry.source_path).read_bytes()).hexdigest()
        snapshot["verification_bindings"] = verification_bindings
        task_ids = await self.store.apply_revision(
            session_id,
            revision,
            prepared,
            max_attempts=min(2, registry.limits.max_attempts) if feature is not None else registry.limits.max_attempts,
            registry_snapshot=snapshot,
        )
        # Legacy sessions use apply as the preparation boundary.  Recheck the
        # persisted binding immediately after the atomic import so they cannot
        # reach a runnable revision without the same host-owned gate used by
        # the coordinator.
        if feature is None:
            await require_plain_execution_binding(self.pool, session_id, revision)
        await self.store.add_chat_message(session_id, "system", f"revision {revision} applied: {len(task_ids)} task(s)")
        return revision, task_ids

    # ── status / report ────────────────────────────────────────────────
    async def status(self, session_id: str) -> dict[str, Any]:
        session = await self.require_session(session_id)
        tasks = await self.store.session_tasks(session_id)
        attempts = await self.store.attempts_for_session(session_id)
        by_task: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            by_task.setdefault(attempt["task_id"], []).append(attempt)
        status_by_id = {task["task_id"]: task["status"] for task in tasks}
        rows = []
        for task in tasks:
            task_attempts = by_task.get(task["task_id"], [])
            latest = task_attempts[-1] if task_attempts else None
            rows.append(
                {
                    "task": task["local_id"],
                    "task_id": task["task_id"],
                    "revision": task["revision"],
                    "project": task["project_id"],
                    "role": task["role"],
                    "status": task["status"],
                    "outcome": task["outcome"],
                    "blocker": task["blocker"] or _pending_blocker(task, status_by_id),
                    "attempts": len([a for a in task_attempts if a["status"] != "cancelled"]),
                    "max_attempts": task["max_attempts"],
                    "latest_attempt": _attempt_view(latest) if latest else None,
                }
            )
        revisions = await self.store.list_revisions(session_id)
        usage = await self.store.agent_usage(session_id)
        return {
            "session": {
                "id": session["id"],
                "title": session["title"],
                "archived": session.get("archived", False),
                "is_test": session.get("is_test", False),
                "status": session["status"],
                "plan_id": session["plan_id"],
                "applied_revision": session["applied_revision"],
                "registry_path": session["registry_path"],
                "coordinator_live": session["coordinator_live"],
                "coordinator": session["coordinator_id"],
                "coordinator_host": session["coordinator_host"],
                "stop_requested": session["stop_requested"],
            },
            "revisions": [
                {
                    "revision": rev["revision"],
                    "status": rev["status"],
                    "source": rev["source"],
                    "error": rev["error"],
                    "tasks": len((rev.get("plan") or {}).get("tasks", [])) if isinstance(rev.get("plan"), dict) else 0,
                }
                for rev in revisions
            ],
            "tasks": rows,
            "usage": usage,
            "dashboard": f"whilly dashboard --plan {session['plan_id']}" if session["plan_id"] else None,
        }

    async def report(self, session_id: str) -> dict[str, Any]:
        status = await self.status(session_id)
        attempts = await self.store.attempts_for_session(session_id)
        usage = await self.store.agent_usage(session_id)
        applied = status["session"]["applied_revision"]
        current = [task for task in status["tasks"] if task["revision"] == applied]
        blockers = [
            f"{task['task']}: {task['blocker'] or task['outcome'] or task['status']}"
            for task in current
            if task["status"] != "DONE"
        ]
        report = {
            "session": status["session"],
            "summary": {
                "accepted": [t["task"] for t in current if t["status"] == "DONE"],
                "failed": [t["task"] for t in current if t["status"] == "FAILED"],
                "not_finished": [t["task"] for t in current if t["status"] not in {"DONE", "FAILED", "SKIPPED"}],
                "total_cost_usd": (
                    round(float(usage["known_cost_usd"]), 6) if not usage.get("unknown_cost_calls") else None
                ),
                "known_cost_usd": usage.get("known_cost_usd"),
                "unknown_cost_calls": usage.get("unknown_cost_calls", 0),
                "calls": usage.get("calls", 0),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens"),
            },
            "blockers": blockers,
            "tasks": status["tasks"],
            "attempts": [_attempt_view(attempt, full=True) for attempt in attempts],
            "notes": [
                "Nothing was merged or pushed. Worktrees and branches are retained for inspection.",
                "DONE means coordinator-run verification passed and an independent read-only review approved.",
            ],
        }
        try:
            registry = await self.applied_registry(session_id)
        except RegistryError as exc:
            # Historical reports remain readable even when their old snapshot
            # predates mandatory execution bindings; this is not an execution fallback.
            report["verification_readiness"] = {"outcome": "unavailable", "reason": str(exc)}
            registry = None
        if registry is not None:
            out_dir = registry.resolved_state_dir() / "sessions" / session_id
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "report.json"
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            report["report_path"] = str(path)
        return report

    async def stop(self, session_id: str, *, kill: bool = False) -> dict[str, Any]:
        """Request a graceful stop; ``kill`` also kills recorded local process groups
        of a session that has no live coordinator (crash cleanup)."""
        session = await self.store.request_stop(session_id)
        if session is None:
            raise SwarmStoreError(f"unknown session {session_id!r}")
        killed: list[int] = []
        if kill and not session["coordinator_live"]:
            host = socket.gethostname()
            for attempt in await self.store.running_attempts(session_id):
                pgid = attempt.get("agent_pgid")
                if pgid and attempt["host"] == host and process_group_alive(pgid):
                    raise OrphanProcessError(
                        f"refusing to kill task {attempt['task_id']} pgid {pgid}: "
                        "recorded PGID has no validated process identity marker"
                    )
        return {"session": session_id, "coordinator_live": session["coordinator_live"], "killed_process_groups": killed}


def _pending_blocker(task: dict[str, Any], status_by_id: dict[str, str]) -> str | None:
    if task["status"] != "PENDING" or WAITING_TAG not in (task.get("required_tags") or []):
        return None
    deps = task.get("dependencies") or []
    failed = [dep.rsplit(".", 1)[-1] for dep in deps if status_by_id.get(dep) in {"FAILED", "SKIPPED"}]
    if failed:
        return "dependency failed: " + ", ".join(failed)
    waiting = [dep.rsplit(".", 1)[-1] for dep in deps if status_by_id.get(dep) != "DONE"]
    return "waiting on: " + ", ".join(waiting) if waiting else None


def _attempt_view(attempt: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    review = attempt.get("review") or {}
    view = {
        "attempt": attempt["attempt"],
        "status": attempt["status"],
        "error": attempt.get("error"),
        "worktree": attempt.get("worktree_path"),
        "branch": attempt.get("branch"),
        "base_sha": attempt.get("base_sha"),
        "head_sha": attempt.get("head_sha"),
        "logs": attempt.get("log_dir"),
        "review_verdict": review.get("verdict") if isinstance(review, dict) else None,
        "cost_usd": float(attempt.get("cost_usd") or 0),
    }
    if full:
        view.update(
            {
                "task": attempt.get("local_id"),
                "task_id": attempt.get("task_id"),
                "started_at": attempt.get("started_at"),
                "finished_at": attempt.get("finished_at"),
                "revision": attempt.get("revision"),
                "result": attempt.get("result"),
                "verification": attempt.get("verification"),
                "review": attempt.get("review"),
            }
        )
    return view


class _TaskFailure(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class Coordinator:
    """Single executor for one swarm session (see module docstring)."""

    def __init__(
        self,
        service: SwarmService,
        session_id: str,
        *,
        max_parallel: int | None = None,
        kill_orphans: bool = False,
        messaging_cli: str | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.service = service
        self.executor = service.executor
        self.store = service.store
        self.repo = service.repo
        self.session_id = session_id
        self.host = socket.gethostname()
        self.coordinator_id = f"swarm-{session_id}-{self.host}-{uuid.uuid4().hex[:8]}"
        self.worker_id = self.coordinator_id
        self.kill_orphans = kill_orphans
        self.messaging_cli = messaging_cli or default_messaging_cli()
        self.poll_seconds = poll_seconds
        self._requested_parallel = max_parallel
        self._stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._lease_lost = False
        self._running: dict[str, asyncio.Task[None]] = {}
        self.summary = RunSummary(session_id=session_id, status="running")
        self.registry: Registry | None = None

    def request_stop(self, reason: str = "stop requested") -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stop.set()

    async def run(self) -> RunSummary:
        session = await self.service.require_session(self.session_id)
        if session["applied_revision"] is None:
            raise SwarmStoreError(f"session {self.session_id} has no applied revision")
        registry = await self.service.applied_registry(self.session_id)
        if registry is None:
            raise SwarmStoreError(f"applied revision of {self.session_id} has no registry snapshot")
        self.service._ensure_executor(registry)
        self.executor = self.service.executor
        from whilly.swarm.product_workflow import feature_for_session

        feature = await feature_for_session(self.service.pool, self.session_id)
        self._collaboration_feature = feature
        if feature:
            self._feature_worker_profile = resolve_profile(registry, "worker-cheap")
            self._feature_reviewer_profile = resolve_profile(registry, "reviewer-cheap")
            self._feature_worker_config = profile_engine(registry, "worker-cheap")
            self._feature_reviewer_config = profile_engine(registry, "reviewer-cheap")
            self._feature_worker_engine = self._feature_worker_profile.engine
            self._feature_reviewer_engine = self._feature_reviewer_profile.engine
        else:
            self._feature_worker_engine = None
            self._feature_reviewer_engine = None
            self._feature_worker_profile = None
            self._feature_reviewer_profile = None
            self._feature_worker_config = None
            self._feature_reviewer_config = None
        self.registry = registry
        limits = registry.limits
        requested = limits.max_parallel if self._requested_parallel is None else self._requested_parallel
        if isinstance(requested, bool) or not isinstance(requested, int) or not 1 <= requested <= 8:
            raise SwarmStoreError("max_parallel must be an integer in range [1, 8]")
        self.max_parallel = min(requested, limits.max_parallel)
        self.applied_revision = session["applied_revision"]
        from whilly.swarm.product_workflow import require_feature_permission

        await require_feature_permission(
            self.service.pool, self.session_id, action="run", revision=self.applied_revision
        )
        self.plan_id = plan_id_for(self.session_id)
        self.state_dir = registry.resolved_state_dir()
        acquired = await self.store.acquire_lease(
            self.session_id, self.coordinator_id, host=self.host, pid=os.getpid(), lease_seconds=limits.lease_seconds
        )
        if not acquired:
            raise CoordinatorActiveError(f"session {self.session_id} already has a live coordinator")
        final_status = "stopped"
        try:
            await self._register_worker()
            await self._recover()
            heartbeat = asyncio.create_task(self._heartbeat_loop())
            try:
                finished = await self._schedule_loop()
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._cancel_running()
            final_status = "finished" if finished and not self._stop.is_set() else "stopped"
        finally:
            if not self._lease_lost:
                await self.store.release_lease(self.session_id, self.coordinator_id, status=final_status)
        self.summary.status = final_status
        if self._stop_reason:
            self.summary.blockers.append(f"coordinator stopped: {self._stop_reason}")
        await self._collect_blockers()
        return self.summary

    async def _register_worker(self) -> None:
        token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        await self.repo.register_worker(self.worker_id, self.host, token_hash, tags=[session_tag(self.session_id)])

    async def _recover(self) -> None:
        """Handle attempts and claims left behind by a coordinator that died."""
        running = await self.store.running_attempts(self.session_id)
        unknown = [a for a in running if not a.get("agent_pgid")]
        if unknown:
            # NULL also covers a crash between OS spawn and PGID persistence,
            # or during coordinator-run checks. Absence is not proof of exit.
            raise OrphanProcessError(
                "unknown process identity for running attempts: "
                + ", ".join(a["task_id"] for a in unknown)
                + "; operator must verify all children stopped before recovery"
            )
        live = [
            a
            for a in running
            if a.get("agent_pgid") and a["host"] == self.host and process_group_alive(a["agent_pgid"])
        ]
        remote = [a for a in running if a.get("agent_pgid") and a["host"] != self.host]
        if remote:
            raise OrphanProcessError(
                "attempts from another host may still be running: "
                + ", ".join(f"{a['task_id']}@{a['host']} pgid {a['agent_pgid']}" for a in remote)
                + "; recover from that host"
            )
        # A persisted PGID is not an identity: it can be reused by an
        # unrelated process. Fail closed until the store supplies a validated
        # process-start identity marker.
        if live:
            raise OrphanProcessError(
                "previous agent process groups are still alive: "
                + ", ".join(f"{a['task_id']} pgid {a['agent_pgid']}" for a in live)
                + "; refusing to kill by PGID alone; stop them manually or provide an identity marker"
            )
        for attempt in running:
            await self.store.finish_attempt(
                attempt["id"], status="abandoned", error="coordinator exited while attempt was running"
            )
        for task in await self.store.session_tasks(self.session_id, revision=self.applied_revision):
            if task["status"] not in {"CLAIMED", "IN_PROGRESS"}:
                continue
            counted = await self.store.counted_attempts(task["task_id"])
            try:
                if counted >= task["max_attempts"]:
                    await self.repo.fail_task(
                        task["task_id"], task["version"], "attempts_exhausted", detail={"attempts": counted}
                    )
                    await self.store.set_outcome(task["task_id"], "attempts_exhausted", f"{counted} attempt(s) used")
                else:
                    await self.repo.release_task(task["task_id"], task["version"], "swarm_recovered")
            except VersionConflictError as exc:
                log.warning("recover: %s changed concurrently: %s", task["task_id"], exc)

    async def _heartbeat_loop(self) -> None:
        assert self.registry is not None
        limits = self.registry.limits
        while True:
            await asyncio.sleep(limits.heartbeat_seconds)
            try:
                owner, stop_requested = await self.store.renew_lease(
                    self.session_id, self.coordinator_id, limits.lease_seconds
                )
                if not owner:
                    self._lease_lost = True
                    self.request_stop("lease lost")
                    return
                if stop_requested:
                    self.request_stop("stop requested")
                await self.repo.update_heartbeat(self.worker_id)
                await self.store.refresh_claims(list(self._running), self.worker_id)
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("heartbeat failed: %s", exc)

    async def _schedule_loop(self) -> bool:
        """Return ``True`` when no runnable work remains, ``False`` on stop."""
        while not self._stop.is_set():
            await self.store.unblock_ready(self.session_id)
            while len(self._running) < self.max_parallel and not self._stop.is_set():
                task = await self.repo.claim_task(self.worker_id, self.plan_id)
                if task is None:
                    break
                if task.id in self._running:  # pragma: no cover — defensive, claims are exclusive
                    log.error("claim returned already-running task %s; stopping", task.id)
                    self.request_stop(f"duplicate claim for {task.id}")
                    break
                self._running[task.id] = asyncio.create_task(self._execute(task))
            if not self._running:
                return True
            done, _ = await asyncio.wait(
                list(self._running.values()), timeout=self.poll_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            for task_id, handle in list(self._running.items()):
                if handle in done:
                    del self._running[task_id]
                    if not handle.cancelled() and handle.exception() is not None:
                        log.error("task %s crashed: %r", task_id, handle.exception())
        return False

    async def _cancel_running(self) -> None:
        for handle in self._running.values():
            handle.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def _collect_blockers(self) -> None:
        status = await self.service.status(self.session_id)
        for task in status["tasks"]:
            if task["revision"] != self.applied_revision:
                continue
            if task["status"] == "DONE":
                self.summary.accepted.append(task["task"])
            elif task["status"] == "FAILED":
                self.summary.failed.append(f"{task['task']}: {task['outcome']} {task['blocker'] or ''}".strip())
            elif task["status"] != "SKIPPED":
                reason = task["blocker"] or f"status {task['status']}"
                if task["status"] == "PENDING" and not task["blocker"] and not self._stop.is_set():
                    reason = "not claimable (global worker pause or plan budget exhausted?)"
                self.summary.blockers.append(f"{task['task']}: {reason}")

    async def _execution_base_sha(self, task_id: str, ctx: dict[str, Any], project_id: str) -> str:
        """Use the sole accepted same-repo predecessor head, never merge implicitly."""
        deps = ctx.get("depends_on") or []
        if not deps:
            return ctx["base_sha"]
        tasks = await self.store.session_tasks(self.session_id, revision=self.applied_revision)
        attempts = await self.store.attempts_for_session(self.session_id)
        by_local = {row["local_id"]: row for row in tasks}
        heads: set[str] = set()
        for dep in deps:
            predecessor = by_local.get(dep)
            if not predecessor or predecessor.get("project_id") != project_id:
                continue
            accepted = [
                a
                for a in attempts
                if a["task_id"] == predecessor["task_id"] and a["status"] == "accepted" and a.get("head_sha")
            ]
            if accepted:
                heads.add(accepted[-1]["head_sha"])
        if len(heads) > 1:
            raise _TaskFailure(
                "integration_required",
                "multiple accepted same-repository predecessor heads require explicit integration",
            )
        return next(iter(heads), ctx["base_sha"])

    async def _execution_materialization(self, task_id: str, ctx: dict[str, Any], project_id: str) -> tuple[Path, str]:
        """Select the accepted predecessor candidate as the Git object source."""
        deps = ctx.get("depends_on") or []
        if not deps:
            return Path(self.registry.projects[project_id].path), ctx["base_sha"]  # type: ignore[union-attr]
        tasks = await self.store.session_tasks(self.session_id, revision=self.applied_revision)
        attempts = await self.store.attempts_for_session(self.session_id)
        by_local = {row["local_id"]: row for row in tasks}
        candidates = []
        for dep in deps:
            predecessor = by_local.get(dep)
            if not predecessor or predecessor.get("project_id") != project_id:
                continue
            accepted = [
                a
                for a in attempts
                if a["task_id"] == predecessor["task_id"] and a["status"] == "accepted" and a.get("head_sha")
            ]
            if accepted:
                candidates.append(accepted[-1])
        heads = {row["head_sha"] for row in candidates}
        if len(heads) > 1:
            raise _TaskFailure(
                "integration_required",
                "multiple accepted same-repository predecessor heads require explicit integration",
            )
        if not candidates:
            return Path(self.registry.projects[project_id].path), ctx["base_sha"]  # type: ignore[union-attr]
        source = Path(candidates[0]["worktree_path"])
        project = self.registry.projects[project_id]
        toolchain_id = project.verification_policy.toolchain_id if project.verification_policy else ""
        git_toolchain = self.executor.toolchain_for_phase("git", fallback=toolchain_id or None)
        inspection_log = self.state_dir / "sessions" / self.session_id / "materialization-inspection"
        git_env = self.executor.environment(
            toolchain_id=git_toolchain, phase="git", attempt_root=inspection_log / "launches" / "git"
        )
        inspection_policy = self.service.execution_policy("git", source, inspection_log, git_env, writable=False)
        actual = await _await_blocking(
            gitops.head_sha,
            source,
            policy=inspection_policy,
            environment=git_env,
            executor=self.executor,
            log_dir=inspection_log,
        )
        if actual != candidates[0]["head_sha"]:
            raise _TaskFailure("candidate_changed", "accepted predecessor candidate head changed")
        return source, actual

    async def _task_engine(self, ctx: dict[str, Any], *, review: bool = False) -> str:
        revision = await self.store.get_revision(self.session_id, ctx["revision"])
        planned = next(
            (t for t in (revision or {}).get("plan", {}).get("tasks", []) if t.get("id") == ctx["local_id"]), {}
        )
        role = self.registry.roles[ctx["role"]]  # type: ignore[union-attr]
        if review:
            if self._feature_reviewer_engine:
                return self._feature_reviewer_engine
            return self.registry.agent.review_engine  # type: ignore[union-attr]
        requested = planned.get("engine") or role.engine
        if self._feature_worker_config is not None:
            default_profile = resolve_profile(self.registry, "worker-cheap")  # type: ignore[arg-type]
            requested = requested or default_profile.engine
            if requested == default_profile.engine:
                return default_profile.engine
            variant = resolve_profile(self.registry, f"worker-cheap-{requested}")  # type: ignore[arg-type]
            if variant.engine != requested:
                raise SwarmStoreError("cheap_profile_engine_mismatch")
            return variant.engine
        return requested or self.registry.agent.planner_engine  # type: ignore[union-attr]

    def _task_engine_config(self, engine_id: str, *, review: bool = False) -> Any:
        if review and self._feature_reviewer_config is not None:
            return self._feature_reviewer_config
        if not review and self._feature_worker_config is not None:
            default_profile = resolve_profile(self.registry, "worker-cheap")  # type: ignore[arg-type]
            profile_name = "worker-cheap" if engine_id == default_profile.engine else f"worker-cheap-{engine_id}"
            return profile_engine(self.registry, profile_name)  # type: ignore[arg-type]
        return self.registry.engines[engine_id]  # type: ignore[union-attr]

    def _task_profile(self, engine_id: str, *, review: bool = False) -> Any:
        if review and self._feature_reviewer_profile is not None:
            return self._feature_reviewer_profile
        if not review and self._feature_worker_profile is not None:
            default = resolve_profile(self.registry, "worker-cheap")  # type: ignore[arg-type]
            name = "worker-cheap" if engine_id == default.engine else f"worker-cheap-{engine_id}"
            return resolve_profile(self.registry, name)  # type: ignore[arg-type]
        return None

    async def _launch_worker_engine(self, engine_id: str, engine_config: Any, **kwargs: Any) -> Any:
        """Revalidate the approved feature immediately before every child launch."""
        from whilly.swarm.product_workflow import require_feature_permission

        await require_feature_permission(
            self.service.pool, self.session_id, action="call", revision=self.applied_revision
        )
        return await admitted_run_engine(self.service.pool, engine_id, engine_config, **kwargs)

    # ── one task ───────────────────────────────────────────────────────

    async def _sync_task_mailbox(self, root: Path, ctx: dict[str, Any], task_id: str) -> None:
        await sync_mailbox(self.store, root, session_id=self.session_id, recipient=ctx["local_id"])
        feature = getattr(self, "_collaboration_feature", None)
        if not feature:
            return  # Legacy sessions retain their existing IPC contract.
        from whilly.adapters.db.learning_delivery import build_delivery_service, delivery_policy_from_env
        from whilly.swarm.learning.domain import Principal
        from whilly.swarm.mailbox import sync_collaboration_mailbox

        assert self.registry is not None
        registry = self.registry
        product_id = feature["product_id"]
        actor_id = f"task:{task_id}"
        projects = tuple(registry.roles[ctx["role"]].projects)
        principal = Principal(actor_id, (product_id,), projects, ("internal",))
        send_scopes = tuple(
            (product_id, project, role_id)
            for role_id, role in registry.roles.items()
            for project in role.projects
            if project in projects
        )
        receive_scopes = ((product_id, ctx["project_id"], ctx["role"]),)
        delivery = build_delivery_service(
            self.service.pool,
            registry,
            actor_id=actor_id,
            sender_scopes=send_scopes,
            recipient_scopes=receive_scopes,
            policy=delivery_policy_from_env(),
        )
        await sync_collaboration_mailbox(
            delivery,
            root / "collaboration",
            principal=principal,
            product_id=product_id,
            recipient_project=ctx["project_id"],
            recipient_role=ctx["role"],
            feature_id=feature["id"],
            task_id=task_id,
        )

        from whilly.adapters.db.learning_proposals import PostgresProposalStore
        from whilly.swarm.learning.proposals import ProposalService
        from whilly.swarm.proposal_mailbox import sync_proposal_mailbox

        # Worker IPC receives submit-only access, never owner decision grants.
        proposals = ProposalService(
            PostgresProposalStore(self.service.pool, actor_host=socket.gethostname()),
            registered_projects={product_id: tuple(registry.projects)},
        )
        await sync_proposal_mailbox(
            proposals, root / "proposals", principal=principal, feature_id=feature["id"], task_id=task_id
        )

    async def _execute(self, task: Task) -> None:
        assert self.registry is not None
        registry = self.registry
        ctx = await self.store.task_context(task.id)
        version = task.version
        attempt_id: int | None = None
        cost: float | None = 0.0
        head: str | None = None
        result: dict[str, Any] | None = None
        evidence: list[dict[str, Any]] | None = None
        review: dict[str, Any] | None = None
        try:
            if ctx is None or ctx["revision"] != self.applied_revision:
                raise _TaskFailure("not_in_applied_revision", "task has no context in the applied revision")
            counted = await self.store.counted_attempts(task.id)
            if counted >= ctx["max_attempts"]:
                raise _TaskFailure("attempts_exhausted", f"{counted} attempt(s) already used")
            started = await self.repo.start_task(task.id, version)
            version = started.version
            project = registry.projects[ctx["project_id"]]
            material_source, base_sha = await self._execution_materialization(task.id, ctx, project.id)
            number = await self.store.next_attempt_number(task.id)
            slug = f"r{ctx['revision']}-{ctx['local_id']}-a{number}"
            worktree = self.state_dir / "worktrees" / self.session_id / slug
            branch = f"swarm/{self.session_id}/{slug}"
            log_dir = self.state_dir / "sessions" / self.session_id / "tasks" / slug
            mailbox_root = log_dir / "mailbox"
            attempt_id, _ = await self.store.start_attempt(
                task.id,
                coordinator_id=self.coordinator_id,
                host=self.host,
                worktree_path=str(worktree),
                branch=branch,
                log_dir=str(log_dir),
                base_sha=base_sha,
            )
            git_toolchain_id = project.verification_policy.toolchain_id if project.verification_policy else ""
            git_toolchain = self.executor.toolchain_for_phase("git", fallback=git_toolchain_id or None)
            git_env = self.executor.environment(
                toolchain_id=git_toolchain, phase="git", attempt_root=log_dir / "launches" / "git"
            )
            # Create the empty destination before handing the capability to
            # the sandbox.  The child may write this one candidate, never its
            # parent (which also contains other attempts) or source metadata.
            worktree.mkdir(parents=True, exist_ok=False)
            configured_git_roots, git_denied_roots = self.executor.roots(git_toolchain)
            git_read = tuple(dict.fromkeys((material_source.resolve(strict=False), *configured_git_roots)))
            material_policy = ExecutionPolicy(
                phase="git",
                read_roots=tuple(str(item) for item in git_read),
                write_roots=(
                    str(worktree.resolve(strict=False)),
                    str(Path(git_env["TMPDIR"]).resolve(strict=False)),
                ),
                denied_roots=tuple(str(item) for item in git_denied_roots),
                network=False,
                timeout_seconds=120,
                max_output_bytes=1024 * 1024,
            )
            try:
                await _await_blocking(
                    materialize_candidate,
                    material_source,
                    base_sha,
                    worktree,
                    branch,
                    policy=material_policy,
                    environment=git_env,
                    hook_policy=project.hook_policy,
                    executor=self.executor,
                    log_dir=log_dir / "git-materialize",
                )
            except gitops.GitError as exc:
                raise _TaskFailure("worktree_failed", str(exc)) from exc

            await self._sync_task_mailbox(mailbox_root, ctx, task.id)

            inbox = await self.store.inbox(self.session_id, ctx["local_id"])
            await self.store.mark_delivered(
                [m["id"] for m in inbox], session_id=self.session_id, recipient=ctx["local_id"]
            )
            packets = await self.store.dependency_packets(task.id)
            commands = [tuple(cmd) for cmd in project.verification] + [tuple(cmd) for cmd in ctx["verification"]]
            engine_id = await self._task_engine(ctx)
            engine_config = self._task_engine_config(engine_id)
            worker_profile = self._task_profile(engine_id)
            prompt = build_worker_prompt(
                registry=registry,
                session_id=self.session_id,
                task_local_id=ctx["local_id"],
                project_id=project.id,
                role_id=ctx["role"],
                description=ctx["description"],
                acceptance=ctx.get("acceptance_criteria") or [],
                verification=commands,
                worktree=str(worktree),
                branch=branch,
                base_sha=base_sha,
                instructions=gitops.read_instruction_files(worktree, project.context_files),
                dependency_packets=packets,
                inbox=inbox,
                messaging_cli=self.messaging_cli,
                coordinator_commits=True,
            )
            from whilly.swarm.product_workflow import feature_for_session

            feature = await feature_for_session(self.service.pool, self.session_id)
            if feature and isinstance(feature.get("spec"), dict) and feature["spec"].get("memory_binding"):
                from whilly.swarm.learning_binding import bound_worker_prompt

                prompt += await bound_worker_prompt(
                    self.service.pool,
                    registry,
                    feature.get("product_id", "default"),
                    feature["spec"]["memory_binding"],
                    project.id,
                )
            _require_prompt_budget(prompt, label="worker")
            worker_fallback = project.verification_policy.toolchain_id if project.verification_policy else None
            worker_toolchain = self.executor.toolchain_for_engine("worker", engine_id, fallback=worker_fallback)
            env = self.executor.environment(
                toolchain_id=worker_toolchain,
                phase="worker",
                identity={"WHILLY_SWARM_SESSION": self.session_id, "WHILLY_SWARM_TASK": ctx["local_id"]},
                attempt_root=log_dir / "launches" / "worker",
            )
            env[MAILBOX_ENV] = str(mailbox_root)
            policy = self.service.execution_policy(
                "worker",
                worktree,
                log_dir,
                env,
                writable=True,
                timeout_seconds=worker_profile.timeout_seconds
                if worker_profile
                else registry.limits.agent_timeout_seconds,
            )

            async def record_pgid(pgid: int) -> None:
                assert attempt_id is not None
                await self.store.set_attempt_pgid(attempt_id, pgid)

            call_id = await self.store.reserve_agent_call(
                self.session_id,
                engine=engine_id,
                mode="worker",
                task_id=task.id,
                prompt_chars=len(prompt),
                max_calls=registry.limits.max_attempts * registry.limits.max_tasks * 3,
            )
            add_dirs = [str(mailbox_root)]
            mailbox_stop = asyncio.Event()
            mailbox_error: str | None = None

            async def mailbox_pump() -> None:
                nonlocal mailbox_error
                try:
                    while not mailbox_stop.is_set():
                        await asyncio.sleep(1)
                        await self._sync_task_mailbox(mailbox_root, ctx, task.id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    mailbox_error = f"mailbox_error: {type(exc).__name__}: {exc}"

            mailbox_task = asyncio.create_task(mailbox_pump())
            try:
                process, engine_result = await self._launch_worker_engine(
                    engine_id,
                    engine_config,
                    mode="worker",
                    prompt=prompt,
                    cwd=worktree,
                    log_dir=log_dir,
                    name="agent",
                    timeout_seconds=(
                        worker_profile.timeout_seconds
                        if worker_profile is not None
                        else registry.limits.agent_timeout_seconds
                    ),
                    max_turns=(worker_profile.max_turns if worker_profile is not None else registry.limits.max_turns),
                    budget_usd=(
                        worker_profile.budget_usd if worker_profile is not None else registry.limits.max_budget_usd
                    ),
                    empty_mcp_config=ensure_empty_mcp_config(self.state_dir),
                    add_dirs=add_dirs,
                    auth_mode=self.executor.provisioning.toolchains[worker_toolchain].auth_mode,
                    env=env,
                    on_start=record_pgid,
                    policy=policy,
                    executor=self.executor,
                )
            finally:
                mailbox_stop.set()
                mailbox_task.cancel()
                await asyncio.gather(mailbox_task, return_exceptions=True)
                try:
                    await self._sync_task_mailbox(mailbox_root, ctx, task.id)
                except Exception as exc:
                    mailbox_error = f"mailbox_error: {type(exc).__name__}: {exc}"
            usage = engine_result.usage
            call_cost = usage.cost_usd
            cost = _add_cost(cost, call_cost)
            await self.store.finish_agent_call(
                call_id,
                usage={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cached_input_tokens": usage.cache_read_tokens,
                },
                cost_usd=call_cost,
                error=engine_result.error or (None if process.ok else process.describe()),
            )
            await self.store.set_attempt_pgid(attempt_id, None)
            if mailbox_error:
                raise _TaskFailure("mailbox_error", mailbox_error)
            if not process.ok or engine_result.error:
                # A stop request may race with the child returning its
                # termination code.  In that case cancellation wins over a
                # worker failure: release the claim so a later coordinator
                # can restart the task, and keep the attempt audit explicit.
                session = await self.service.require_session(self.session_id)
                if self._stop.is_set() or session["stop_requested"]:
                    await self.store.finish_attempt(
                        attempt_id,
                        status="cancelled",
                        head_sha=head,
                        result=result,
                        error="cancelled",
                        cost_usd=cost,
                    )
                    try:
                        await self.repo.release_task(task.id, version, "swarm_stopped")
                    except VersionConflictError:
                        pass
                    return
                reason = "agent_timeout" if process.timed_out else "agent_failed"
                raise _TaskFailure(
                    reason, f"{process.describe()}; {engine_result.error or _tail(process.stderr_path, 800).strip()}"
                )
            try:
                result = parse_worker_result(engine_result.output)
            except ResultError as exc:
                raise _TaskFailure("invalid_result", str(exc)) from exc
            if result["status"] == "blocked":
                raise _TaskFailure("agent_blocked", result["summary"])
            commit_policy = self.service.execution_policy(
                "git", worktree, log_dir, git_env, writable=True, timeout_seconds=120
            )
            try:
                head = await _await_blocking(
                    gitops.commit_task_changes,
                    worktree,
                    expected_branch=branch,
                    task_id=ctx["local_id"],
                    policy=commit_policy,
                    environment=git_env,
                    executor=self.executor,
                    log_dir=log_dir / "git-commit",
                )
            except gitops.GitError as exc:
                raise _TaskFailure("coordinator_commit_failed", str(exc)) from exc
            dirty = await _await_blocking(
                gitops.dirty_files,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            if dirty:
                raise _TaskFailure("uncommitted_changes", ", ".join(dirty[:20]))
            head = await _await_blocking(
                gitops.head_sha,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            changed = await _await_blocking(
                gitops.changed_files,
                worktree,
                base_sha,
                head,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            result.update({"base_sha": base_sha, "head_sha": head, "changed_files": changed})
            if project.verification_policy is not None:
                protected = protected_changes(changed, project.verification_policy)
                if protected:
                    raise _TaskFailure("policy_changed", "protected files changed: " + ", ".join(protected[:20]))
            if not commands:
                raise _TaskFailure("unverified", "no verification commands configured for this task")
            tree_before_verification = await _await_blocking(
                gitops.tree_sha,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            evidence = await self._verify(project, head, worktree, log_dir, task_commands=commands)
            post_verify_head = await _await_blocking(
                gitops.head_sha,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            post_verify_tree = await _await_blocking(
                gitops.tree_sha,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            post_verify_dirty = await _await_blocking(
                gitops.dirty_files,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            if post_verify_head != head or post_verify_tree != tree_before_verification or post_verify_dirty:
                raise _TaskFailure("candidate_changed", "candidate head/tree changed during verification")
            failed = [e for e in evidence if not e["passed"]]
            if failed:
                raise _TaskFailure(
                    "verification_failed", "; ".join(f"{' '.join(e['argv'])}: {e['outcome']}" for e in failed)
                )
            diff = await _await_blocking(
                gitops.diff_text,
                worktree,
                base_sha,
                head,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            review_prompt = build_reviewer_prompt(
                task_local_id=ctx["local_id"],
                description=ctx["description"],
                acceptance=ctx.get("acceptance_criteria") or [],
                result=result,
                diff=diff,
                verification=[{k: e[k] for k in ("argv", "exit_code", "outcome", "stdout_tail")} for e in evidence],
            )
            _require_prompt_budget(review_prompt, label="review")
            review_engine_id = await self._task_engine(ctx, review=True)
            review_config = self._task_engine_config(review_engine_id, review=True)
            review_call_id = await self.store.reserve_agent_call(
                self.session_id,
                engine=review_engine_id,
                mode="review",
                task_id=task.id,
                prompt_chars=len(review_prompt),
                max_calls=registry.limits.max_attempts * registry.limits.max_tasks * 3,
            )
            from whilly.swarm.product_workflow import require_feature_permission

            await require_feature_permission(
                self.service.pool, self.session_id, action="call", revision=self.applied_revision
            )
            review_fallback = project.verification_policy.toolchain_id if project.verification_policy else None
            review_toolchain = self.executor.toolchain_for_engine("review", review_engine_id, fallback=review_fallback)
            review_env = self.executor.environment(
                toolchain_id=review_toolchain,
                phase="review",
                identity={"WHILLY_SWARM_SESSION": self.session_id, "WHILLY_SWARM_REVIEW": ctx["local_id"]},
                attempt_root=log_dir / "launches" / "review",
            )
            review_policy = self.service.execution_policy(
                "review",
                worktree,
                log_dir,
                review_env,
                writable=False,
                timeout_seconds=self._feature_reviewer_profile.timeout_seconds
                if self._feature_reviewer_profile
                else registry.agent.review_timeout_seconds,
            )
            review_process, review_result = await admitted_run_engine(
                self.service.pool,
                review_engine_id,
                review_config,
                mode="read_only",
                prompt=review_prompt,
                cwd=worktree,
                log_dir=log_dir,
                name="review",
                timeout_seconds=(
                    self._feature_reviewer_profile.timeout_seconds
                    if self._feature_reviewer_profile is not None
                    else registry.agent.review_timeout_seconds
                ),
                max_turns=(
                    self._feature_reviewer_profile.max_turns
                    if self._feature_reviewer_profile is not None
                    else registry.agent.review_max_turns
                ),
                budget_usd=(
                    self._feature_reviewer_profile.budget_usd
                    if self._feature_reviewer_profile is not None
                    else registry.agent.review_budget_usd
                ),
                empty_mcp_config=ensure_empty_mcp_config(self.state_dir),
                auth_mode=self.executor.provisioning.toolchains[review_toolchain].auth_mode,
                env=review_env,
                on_start=record_pgid,
                policy=review_policy,
                executor=self.executor,
            )
            cost = _add_cost(cost, review_result.usage.cost_usd)
            review_usage = review_result.usage
            await self.store.finish_agent_call(
                review_call_id,
                usage={
                    "input_tokens": review_usage.input_tokens,
                    "output_tokens": review_usage.output_tokens,
                    "cached_input_tokens": review_usage.cache_read_tokens,
                },
                cost_usd=review_usage.cost_usd,
                error=review_result.error or (None if review_process.ok else review_process.describe()),
            )
            await self.store.set_attempt_pgid(attempt_id, None)
            if not review_process.ok or review_result.error:
                raise _TaskFailure("review_failed", review_result.error or review_process.describe())
            try:
                review = parse_review(review_result.output)
            except ResultError as exc:
                raise _TaskFailure("review_invalid", str(exc)) from exc
            if review["verdict"] != "approve":
                raise _TaskFailure("review_rejected", review["summary"] or "; ".join(review["findings"]))
            final_head = await _await_blocking(
                gitops.head_sha,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            final_tree = await _await_blocking(
                gitops.tree_sha,
                worktree,
                policy=commit_policy,
                environment=git_env,
                executor=self.executor,
                log_dir=log_dir / "git-inspect",
            )
            if final_head != head or final_tree != tree_before_verification:
                raise _TaskFailure("candidate_changed", "candidate head/tree changed during verification or review")
            await require_feature_permission(
                self.service.pool, self.session_id, action="accept", revision=self.applied_revision
            )
            await self.store.accept_task(
                self.repo,
                task.id,
                version,
                attempt_id,
                head_sha=head,
                result=result,
                verification=evidence,
                review=review,
                cost_usd=cost,
            )
            await self.store.unblock_ready(self.session_id)
        except _TaskFailure as failure:
            await self._fail(task.id, version, attempt_id, failure, head, result, evidence, review, cost)
        except asyncio.CancelledError:
            if attempt_id is not None:
                await self.store.finish_attempt(
                    attempt_id, status="cancelled", head_sha=head, result=result, error="cancelled", cost_usd=cost
                )
            if not self._lease_lost:
                try:
                    await self.repo.release_task(task.id, version, "swarm_stopped")
                except VersionConflictError:
                    pass
            raise
        except VersionConflictError as exc:
            if attempt_id is not None:
                await self.store.finish_attempt(
                    attempt_id, status="abandoned", error=f"claim_lost: {exc}", cost_usd=cost
                )
            await self.store.set_outcome(task.id, "claim_lost", str(exc))
        except Exception as exc:  # noqa: BLE001 — record every coordinator-side error on the task
            log.exception("task %s crashed", task.id)
            failure = _TaskFailure("coordinator_error", f"{type(exc).__name__}: {exc}")
            await self._fail(task.id, version, attempt_id, failure, head, result, evidence, review, cost)

    async def _fail(
        self,
        task_id: str,
        version: int,
        attempt_id: int | None,
        failure: _TaskFailure,
        head: str | None,
        result: dict[str, Any] | None,
        evidence: list[dict[str, Any]] | None,
        review: dict[str, Any] | None,
        cost: float,
    ) -> None:
        try:
            await self.repo.fail_task(
                task_id, version, failure.reason, detail={"message": failure.message[:2000], "cost_usd": cost}
            )
        except VersionConflictError as exc:
            log.warning("fail_task %s lost claim: %s", task_id, exc)
        if attempt_id is not None:
            await self.store.finish_attempt(
                attempt_id,
                status="failed",
                head_sha=head,
                result=result,
                verification=evidence,
                review=review,
                error=str(failure)[:4000],
                cost_usd=cost,
            )
        await self.store.set_outcome(task_id, failure.reason, failure.message[:2000])

    async def _verify(
        self,
        project: Any,
        head_sha: str,
        worktree: Path,
        log_dir: Path,
        *,
        task_commands: list[tuple[str, ...]] = (),
    ) -> list[dict[str, Any]]:
        assert self.registry is not None
        from whilly.swarm.verification_runner import VerificationRunner

        verification = require_project_verification(project)
        verify_fallback = verification.toolchain_id
        verify_toolchain = self.executor.toolchain_for_phase("verify", fallback=verify_fallback)
        env = self.executor.environment(
            toolchain_id=verify_toolchain, phase="verify", attempt_root=log_dir / "launches" / "verify"
        )
        policy = self.service.execution_policy(
            "verify",
            worktree,
            log_dir,
            env,
            # Verification runs inside a disposable candidate worktree.  Test
            # suites are allowed to create fixtures/caches and to exercise
            # generated artifacts there; the candidate's .git directory is
            # still protected by execution_policy().
            writable=True,
            timeout_seconds=self.registry.limits.verification_timeout_seconds,
        )
        runner = VerificationRunner()
        commands = [
            *(("test", "pytest-junit", argv) for argv in verification.test),
            *(("lint", "ruff-json", argv) for argv in verification.lint),
            *(("architecture", "architecture-json", argv) for argv in verification.architecture),
        ]
        evidence = await runner.run(
            commands,
            executor=self.executor,
            policy=policy,
            environment=env,
            cwd=worktree,
            log_dir=log_dir / "verification",
            stage="candidate",
            head_sha=head_sha,
            policy_digest=verification.digest(),
        )
        # Task-local checks remain compatibility constraints; mandatory
        # project gates above remain the structured acceptance authority.
        for index, argv in enumerate(task_commands):
            process = await self.executor.run(
                argv,
                phase="verify",
                policy=policy,
                environment=env,
                cwd=worktree,
                log_dir=log_dir / "task-checks" / str(index),
            )
            evidence.append(
                runner.wrap_evidence(
                    stage="candidate",
                    category="task",
                    argv=tuple(argv),
                    head_sha=head_sha,
                    policy_digest=verification.digest(),
                    outcome="passed" if process.ok else "failed",
                    collected=None,
                    passed=1 if process.ok else 0,
                    skipped=None,
                    exit_code=process.exit_code,
                )
            )
        return evidence


def describe_registry_error(exc: RegistryError) -> str:
    return "\n".join(f"  - {problem}" for problem in exc.problems)
