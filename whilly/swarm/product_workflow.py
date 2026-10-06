"""Trusted product workflow: explicit profiles, immutable approval and bounded execution.

Browser payloads are intent, never trusted execution metadata. Planning cannot
write project code; returned specifications are persisted by this service.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
from typing import Any

from whilly.swarm import gitops
from whilly.swarm.product import ProductService
from whilly.swarm.product_store import _decode
from whilly.swarm.profiles import profile_engine, resolve_profile
from whilly.swarm.registry import Registry, load_registry
from whilly.swarm.store import SwarmStoreError
from whilly.swarm.verification import VerificationError, host_project_execution_binding
from whilly.swarm.execution import GuardedExecutor


class WorkflowBlocked(SwarmStoreError):
    """An explicit setup, budget or approval blocker."""


async def apply_or_resume_revision(service: Any, session_id: str, revision: int) -> None:
    """Apply a proposed revision or continue the same already-applied revision."""
    stored = await service.store.get_revision(session_id, revision)
    if stored is None:
        raise WorkflowBlocked("feature_plan_revision_missing")
    if stored["status"] == "proposed":
        await service.apply_revision(session_id, revision)
        return
    if stored["status"] == "applied":
        session = await service.require_session(session_id)
        if session.get("applied_revision") == revision:
            return
    raise WorkflowBlocked("feature_plan_revision_not_resumable")


async def _await_blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Keep host-side Git/BMAD work alive until it finishes on cancellation."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(operation)
        except BaseException:
            pass
        raise


async def require_plain_execution_binding(pool: Any, session_id: str, revision: int | None) -> None:
    """Recheck a non-product binding immediately before model or acceptance work."""
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT registry_path, applied_revision FROM swarm_sessions WHERE id=$1", session_id
        )
        if session is None or session["applied_revision"] is None:
            raise WorkflowBlocked("verification_binding_required")
        applied_revision = int(session["applied_revision"])
        if revision is not None and revision != applied_revision:
            raise WorkflowBlocked("verification_revision_mismatch")
        row = await conn.fetchrow(
            "SELECT plan, registry_snapshot FROM swarm_plan_revisions WHERE session_id=$1 AND revision=$2",
            session_id,
            applied_revision,
        )
    snapshot = row["registry_snapshot"] if row else None
    plan = row["plan"] if row else None
    if isinstance(plan, str):
        plan = json.loads(plan)
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    project_ids = {task["project"] for task in (plan or {}).get("tasks", [])}
    if not isinstance(snapshot, dict) or not project_ids:
        raise WorkflowBlocked("verification_binding_required")
    from pathlib import Path

    current_hash = hashlib.sha256(Path(session["registry_path"]).read_bytes()).hexdigest()
    if snapshot.get("_registry_hash") != current_hash:
        raise WorkflowBlocked("feature_registry_changed: replan and approve again")
    from whilly.swarm.runtime import registry_from_snapshot

    try:
        stored_registry = registry_from_snapshot(snapshot, project_ids=project_ids)
    except Exception as exc:
        raise WorkflowBlocked(f"verification_binding_changed: {exc}") from exc
    current_registry = load_registry(session["registry_path"])
    for project_id in sorted(project_ids):
        project = current_registry.projects[project_id]
        base_sha = await _await_blocking(gitops.resolve_commit, project.path, project.base_ref)
        try:
            current = await _await_blocking(host_project_execution_binding, project, base_sha)
        except VerificationError as exc:
            raise WorkflowBlocked(str(exc)) from exc
        if current != snapshot["verification_bindings"][project_id]:
            raise WorkflowBlocked(f"verification_binding_changed: {project_id}")
        if stored_registry.projects[project_id].verification_policy is None:
            raise WorkflowBlocked(f"verification_policy_required: {project_id}")


def validate_budget(budget: dict) -> dict[str, int]:
    bounds = {"max_calls": 1000, "max_elapsed_seconds": 86400}
    for name, maximum in bounds.items():
        value = budget.get(name)
        if type(value) is not int or not 1 <= value <= maximum:
            raise WorkflowBlocked(f"budget_invalid: {name} must be 1..{maximum}")
    return {key: budget[key] for key in bounds}


def planner_registry(registry: Registry, name: str) -> Registry:
    if name not in {"planner-strong", "planner-escalation", "planner-strong-claude", "planner-strong-codex"}:
        raise WorkflowBlocked("planner_profile_required")
    profile = resolve_profile(registry, name)
    return dataclasses.replace(
        registry,
        engines={**registry.engines, profile.engine: profile_engine(registry, name)},
        agent=dataclasses.replace(
            registry.agent,
            planner_engine=profile.engine,
            planner_max_turns=profile.max_turns,
            planner_timeout_seconds=profile.timeout_seconds,
            planner_budget_usd=profile.budget_usd,
        ),
    )


async def feature_for_session(pool: Any, session_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM swarm_product_features WHERE session_id=$1", session_id)
    return _decode(dict(row)) if row else None


async def require_feature_permission(
    pool: Any, session_id: str, *, action: str, revision: int | None = None, expected_digest: str | None = None
) -> None:
    feature = await feature_for_session(pool, session_id)
    if not feature:
        async with pool.acquire() as conn:
            chief = await conn.fetchval("SELECT 1 FROM swarm_products WHERE chief_session_id=$1", session_id)
        if chief:
            raise WorkflowBlocked("chief_session_is_planning_only")
        if action != "apply":
            await require_plain_execution_binding(pool, session_id, revision)
        return  # Legacy sessions retain discussion/planning, not unbound execution.
    allowed_states = {"approved", "running", "review", "mr_ready"} if action == "publish" else {"approved", "running"}
    if feature["status"] not in allowed_states or not feature["approved_digest"]:
        raise WorkflowBlocked("feature_approval_required")
    if feature["approved_digest"] != feature["approval_digest"]:
        raise WorkflowBlocked("feature_approval_stale")
    if expected_digest is not None and feature["approved_digest"] != expected_digest:
        raise WorkflowBlocked("feature_approval_binding_changed")
    if revision is not None and revision != feature["plan_revision"]:
        raise WorkflowBlocked("feature_revision_mismatch")
    async with pool.acquire() as conn:
        path = await conn.fetchval("SELECT registry_path FROM swarm_sessions WHERE id=$1", session_id)
    from pathlib import Path

    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != feature["registry_hash"]:
        raise WorkflowBlocked("feature_registry_changed: replan and approve again")
    registry = load_registry(path)
    binding = feature.get("spec", {}).get("memory_binding") if isinstance(feature.get("spec"), dict) else None
    if binding is not None:
        from whilly.swarm.learning_binding import validate_binding

        await validate_binding(pool, registry, feature.get("product_id", "default"), binding)
    if action in {"run", "call", "publish"}:
        async with pool.acquire() as conn:
            bases = await conn.fetch(
                "SELECT project_id,base_sha FROM swarm_task_context WHERE session_id=$1 AND revision=$2",
                session_id,
                feature["plan_revision"],
            )
        if any(row["base_sha"] != feature["base_shas"].get(row["project_id"]) for row in bases):
            raise WorkflowBlocked("feature_applied_base_mismatch")
    spec = feature.get("spec")
    execution_binding = spec.get("execution_binding") if isinstance(spec, dict) else None
    if not isinstance(execution_binding, dict):
        raise WorkflowBlocked("execution_binding_required")
    if execution_binding.get("_status") != "ready":
        reason = execution_binding.get("_reason", "verification_binding_unavailable")
        raise WorkflowBlocked(f"verification_not_ready: {reason}")
    for project_id, approved_sha in feature["base_shas"].items():
        try:
            current_binding = await _await_blocking(
                host_project_execution_binding, registry.projects[project_id], approved_sha
            )
        except VerificationError as exc:
            raise WorkflowBlocked(str(exc)) from exc
        if execution_binding.get(project_id) != current_binding:
            raise WorkflowBlocked(f"feature_execution_binding_changed: {project_id}")
    for project_id, approved_sha in feature["base_shas"].items():
        project = registry.projects[project_id]
        current = await _await_blocking(gitops.resolve_commit, project.path, project.base_ref)
        if current != approved_sha:
            raise WorkflowBlocked(f"feature_base_changed: {project_id}")
    validate_budget(feature["budget"])


async def check_feature_budget(conn: Any, session_id: str, mode: str) -> int | None:
    """Called while canonical session row is locked; all model reservations count."""
    row = await conn.fetchrow("SELECT * FROM swarm_product_features WHERE session_id=$1", session_id)
    if not row:
        return None
    feature = _decode(dict(row))
    budget = validate_budget(feature["budget"])
    if mode != "planner" and (feature["status"] != "running" or not feature["approved_digest"]):
        raise WorkflowBlocked("feature_approval_required")
    elapsed = await conn.fetchval(
        "SELECT SUM(EXTRACT(EPOCH FROM (COALESCE(finished_at,NOW())-created_at))) FROM swarm_agent_calls WHERE session_id=$1",
        session_id,
    )
    if elapsed is not None and float(elapsed) >= budget["max_elapsed_seconds"]:
        raise WorkflowBlocked("feature_time_budget_exhausted")
    return budget["max_calls"]


class ProductWorkflow:
    def __init__(self, pool: Any, registry_path: str, executor: GuardedExecutor | None = None, *, publication_backend=None):
        self.publication_backend = publication_backend
        self.pool = pool
        self.registry_path = registry_path
        self.executor = executor or GuardedExecutor()
        self.products = ProductService(pool, registry_path)

    def _executor_for_registry(self, registry: Registry) -> GuardedExecutor:
        if self.executor.provisioning.toolchains:
            return self.executor
        return GuardedExecutor.from_registry(registry)

    async def require(self, feature_id: str) -> dict:
        feature = await self.products.get_feature(feature_id)
        if feature is None:
            raise WorkflowBlocked("feature_not_found")
        return feature

    async def plan(self, feature_id: str, text: str, profile: str = "planner-strong") -> dict:
        from whilly.swarm.bmad import build_bmad_context
        from whilly.swarm.runtime import SwarmService

        feature = await self.require(feature_id)
        if feature["status"] == "running":
            raise WorkflowBlocked("feature_running: stop before replanning")
        registry = load_registry(self.registry_path)
        effective = planner_registry(registry, profile)
        workflow_executor = self._executor_for_registry(effective)
        memory_suffix = ""
        memory_binding = None
        if os.environ.get("WHILLY_SWARM_MEMORY") == "1":
            from whilly.swarm.learning_binding import bind_spec, make_binding, memory_prompt, planning_context

            memory_query = f"{feature['intent']}\n{text}"
            if len(memory_query) > 4096:
                raise WorkflowBlocked("memory_query_limit")
            package = await planning_context(
                self.pool, registry, feature.get("product_id", "default"), query=memory_query
            )
            memory_suffix = memory_prompt(package)
            memory_binding = make_binding(package)
        profiles = {
            name: dataclasses.asdict(resolve_profile(registry, name))
            for name in (profile, "worker-cheap", "reviewer-cheap")
        }
        for name in ("worker-cheap-claude", "worker-cheap-codex"):
            if name in registry.profiles:
                profiles[name] = dataclasses.asdict(resolve_profile(registry, name))
        spec_workspace = None
        if registry.raw.get("bmad", {}).get("mode") == "host-spec":
            from whilly.swarm.bmad_host import prepare_spec_workspace

            spec_workspace = await _await_blocking(
                prepare_spec_workspace,
                registry,
                feature_id,
                intent=feature["intent"] + "\n" + text,
                previous_spec=str(feature["spec"] or ""),
                executor=workflow_executor,
            )
            context = spec_workspace.context + (
                "\nHost adapter protocol: do NOT write files or execute scripts yourself. "
                "The host has initialized the BMAD memlog and resolved config. "
                "Return one fenced bmad-artifacts JSON block with keys SPEC.md (Markdown string), "
                "companions (map of simple .md filenames to strings), decisions (list of strings), "
                "coherence and preservation (nonempty validation verdicts). "
                "Follow it with the canonical executable plan in a separate json fence. "
                "Preserve existing capability IDs. The host persists all artifacts.\n"
            )
        else:
            context = build_bmad_context(registry)
        validate_budget(feature["budget"])
        await self.products.invalidate(feature_id, "planning_in_progress")
        service = SwarmService(self.pool, executor=workflow_executor)
        reply = await service.chat(
            feature["session_id"],
            f"Feature: {feature['title']}\nIntent: {feature['intent']}\nCurrent specification: {feature['spec']}\n{text}",
            request_plan=True,
            registry_override=effective,
            phase="escalation" if profile == "planner-escalation" else "planner",
            prompt_prefix=context
            + memory_suffix
            + "\nProduce a human-readable specification with acceptance criteria followed by the canonical JSON plan. Do not edit any repository.\n",
        )
        if reply.error or reply.revision_status != "proposed":
            raise WorkflowBlocked(reply.error or "planner_no_valid_plan")
        specification = reply.reply
        if spec_workspace:
            from pathlib import Path
            from whilly.swarm.bmad_host import persist_spec_result

            artifacts = await _await_blocking(persist_spec_result, spec_workspace, reply.reply)
            specification = {
                "document": Path(artifacts["spec_path"]).read_text(encoding="utf-8"),
                "artifacts": {
                    path.name: path.read_text(encoding="utf-8") for path in spec_workspace.workspace.glob("*.md")
                },
                "artifact_directory": str(spec_workspace.workspace),
            }
        revision = await service.store.get_revision(feature["session_id"], reply.revision)
        bases = {}
        for task in revision["plan"]["tasks"]:
            project = registry.projects[task["project"]]
            bases[project.id] = await _await_blocking(gitops.resolve_commit, project.path, project.base_ref)
        if memory_binding is not None:
            specification = bind_spec(specification, memory_binding)
        return await self.products.prepare(
            feature_id,
            spec=specification,
            plan_revision=reply.revision,
            base_shas=bases,
            profiles=profiles,
            budget=feature["budget"],
            expected_revision=feature["revision"],
        )

    async def execute(self, feature_id: str, workers: int) -> dict:
        from whilly.swarm.runtime import SwarmService, Coordinator

        feature = await self.require(feature_id)
        await require_feature_permission(
            self.pool, feature["session_id"], action="apply", revision=feature["plan_revision"]
        )
        acquired = await self.products.store.begin_run(feature_id, feature["approved_digest"])
        if not acquired:
            raise WorkflowBlocked("feature_already_running_or_stale")
        registry = load_registry(self.registry_path)
        service = SwarmService(self.pool, executor=self._executor_for_registry(registry))
        try:
            await apply_or_resume_revision(service, feature["session_id"], feature["plan_revision"])
            async with asyncio.timeout(validate_budget(feature["budget"])["max_elapsed_seconds"]):
                for round_number in range(2):
                    coordinator = Coordinator(service, feature["session_id"], max_parallel=min(workers, 5))
                    await coordinator.run()
                    status = await service.status(feature["session_id"])
                    retryable = [
                        task
                        for task in status["tasks"]
                        if task["revision"] == feature["plan_revision"]
                        and task["status"] == "FAILED"
                        and task["attempts"] < min(2, task["max_attempts"])
                        and task["outcome"]
                        in {
                            "agent_failed",
                            "agent_timeout",
                            "invalid_result",
                            "verification_failed",
                            "review_rejected",
                            "review_failed",
                            "review_invalid",
                            "uncommitted_changes",
                        }
                    ]
                    if round_number or not retryable:
                        break
                    for task in retryable:
                        await service.store.rerun_task(feature["session_id"], task["task"])
            status = await service.status(feature["session_id"])
            tasks = [t for t in status["tasks"] if t["revision"] == feature["plan_revision"]]
            if not tasks or any(t["status"] != "DONE" for t in tasks):
                exhausted = [t for t in tasks if t["status"] == "FAILED" and t["attempts"] >= 2]
                if exhausted:
                    await self.products.invalidate(feature_id, "planner_escalation_required")
                    return await self.plan(
                        feature_id,
                        "Two cheap attempts failed. Re-decompose the work; do not implement code. Evidence: "
                        + json.dumps(exhausted, default=str)[:16000],
                        "planner-escalation",
                    )
                raise WorkflowBlocked("execution_blocked: inspect task evidence; replan required")
            from whilly.swarm.product_publication import publish_feature

            await self.products.finish(feature_id, "review", expected_digest=feature["approved_digest"])
            try:
                await publish_feature(self, feature_id)
            except WorkflowBlocked as exc:
                # Preserve approved evidence for explicit publication retry; no new model work.
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE swarm_product_features SET blocker=$2 WHERE id=$1", feature_id, str(exc))
        except BaseException as exc:
            await self.products.invalidate(feature_id, f"{type(exc).__name__}: {str(exc)[:1000]}")
            raise
        return await self.require(feature_id)

    async def discuss(self, body: str, profile: str) -> dict:
        from whilly.swarm.runtime import SwarmService

        await self.products.add_message(body)
        registry = planner_registry(load_registry(self.registry_path), profile)
        # Dedicated persisted chief session; no product repositories are writable.
        service = SwarmService(self.pool, executor=self._executor_for_registry(registry))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(739241)")
                session_id = await conn.fetchval("SELECT chief_session_id FROM swarm_products WHERE id='default'")
                if not session_id:
                    session_id = await service.create_session(self.registry_path, title="Product chief")
                    await conn.execute("UPDATE swarm_products SET chief_session_id=$1 WHERE id='default'", session_id)
        features = await self.products.list_features()
        context = json.dumps([{"id": f["id"], "title": f["title"], "status": f["status"]} for f in features])[:12000]
        reply = await service.chat(
            session_id,
            body,
            registry_override=registry,
            prompt_prefix="You are the product chief. Discuss scope and cross-project dependencies. Never implement code. Feature work requires explicit feature planning and approval. Current features: "
            + context
            + "\n",
            phase="discussion",
        )
        if reply.error:
            raise WorkflowBlocked(reply.error)
        return await self.products.store.add_message("default", reply.reply, sender="planner")
