"""Database admission adapter for learning proposals.

This adapter is deliberately a persistence boundary: it performs readonly
admission checks and the feature-first decision transaction, but never calls a
model, creates a runtime task, or queues execution.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Mapping
from typing import Any

import asyncpg

from whilly.swarm.learning.proposals import (
    ProposalResult,
    ProposalService,
    _decision_reason,
)
from whilly.swarm.product_workflow import WorkflowBlocked, validate_budget
from whilly.swarm.registry import load_registry


class PostgresProposalAdmission:
    def __init__(self, pool: asyncpg.Pool, *, actor_host: str) -> None:
        self.pool = pool
        self.actor_host = actor_host

    async def inspect_admission(
        self, product_id: str, proposal_id: str, *, registry: Any, owner: bool
    ) -> Mapping[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT p.*, f.revision AS feature_revision, f.status AS feature_status,
                          f.spec, f.budget, f.session_id
                   FROM swarm_learning_proposals p
                   LEFT JOIN swarm_product_features f ON f.id=p.origin_feature_id
                   WHERE p.product_id=$1 AND p.id=$2""",
                product_id,
                proposal_id,
            )
            if row is None:
                raise KeyError(proposal_id)
            data = dict(row)
            budget = _json(data, "budget", {})
            blockers: list[str] = []
            if data["feature_status"] is None:
                blockers.append("origin_feature_missing")
            elif data["feature_status"] == "running":
                blockers.append("feature_running")
            blockers.extend(self._path_blockers(data["target_module"], registry))
            blockers.extend(await self._dependency_blockers(conn, data, product_id))
            if data["contract_impact"] != "none":
                blockers.append("contract_verification_proof_type_missing")
            if data["session_id"] is not None:
                blockers.extend(await self._budget_status(conn, data["session_id"], budget))
            if data["session_id"] is not None and await self._existing_active_work(conn, data):
                blockers.append("existing_active_project_work")
            return {
                "feature_revision": data["feature_revision"],
                "feature_status": data["feature_status"],
                "contract_impact": data["contract_impact"],
                "protected": any(item == "protected_module_path" for item in blockers),
                "blockers": tuple(dict.fromkeys(blockers)),
            }

    async def accept_for_planning(
        self,
        product_id: str,
        proposal_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        reason: str,
        registry: Any,
    ) -> ProposalResult:
        _decision_reason(reason)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Lock order is part of the contract: origin feature first, proposal second.
                origin = await conn.fetchrow(
                    "SELECT origin_feature_id FROM swarm_learning_proposals WHERE product_id=$1 AND id=$2",
                    product_id,
                    proposal_id,
                )
                if origin is None:
                    raise KeyError(proposal_id)
                feature = await conn.fetchrow(
                    "SELECT * FROM swarm_product_features WHERE id=$1 FOR UPDATE", origin["origin_feature_id"]
                )
                if feature is None or feature["product_id"] != product_id:
                    raise KeyError(proposal_id)
                proposal = await conn.fetchrow(
                    "SELECT * FROM swarm_learning_proposals WHERE product_id=$1 AND id=$2 FOR UPDATE",
                    product_id,
                    proposal_id,
                )
                if proposal is None:
                    raise KeyError(proposal_id)
                if proposal["status"] == "awaiting_approval":
                    return ProposalResult(
                        proposal_id, "awaiting_approval", feature_revision=feature["revision"], reason="retry"
                    )
                if proposal["status"] not in {"proposed", "triaged"}:
                    return ProposalResult(
                        proposal_id,
                        "blocked",
                        feature_revision=feature["revision"],
                        reason="proposal_state_not_admissible",
                        blockers=("proposal_state_not_admissible",),
                    )
                if feature["status"] == "running":
                    return ProposalResult(
                        proposal_id,
                        "blocked",
                        feature_revision=feature["revision"],
                        reason="feature_running",
                        blockers=("feature_running",),
                    )
                if int(feature["revision"]) != expected_revision:
                    return ProposalResult(
                        proposal_id,
                        "blocked",
                        feature_revision=feature["revision"],
                        reason="stale_feature_revision",
                        blockers=("stale_feature_revision",),
                    )
                snapshot = await self._inspect_locked(conn, product_id, proposal, feature, registry)
                blockers = tuple(snapshot["blockers"])
                if blockers:
                    return ProposalResult(
                        proposal_id,
                        "blocked",
                        feature_revision=feature["revision"],
                        reason=blockers[0],
                        blockers=blockers,
                    )
                current_spec = _json(dict(feature), "spec", None)
                accepted = {"previous_spec": current_spec, "accepted_proposals": []}
                records = list(accepted["accepted_proposals"])
                records.append(
                    {
                        "id": proposal_id,
                        "fingerprint": proposal["fingerprint"],
                        "target_project": proposal["target_project"],
                        "target_module": proposal["target_module"],
                        "contract_impact": proposal["contract_impact"],
                        "resource_class": proposal["resource_class"],
                        "evidence": list(_json(dict(proposal), "evidence_refs", ())),
                        "outcome": proposal["outcome"],
                        "acceptance": list(_json(dict(proposal), "acceptance", ())),
                        "dependencies": list(_json(dict(proposal), "dependencies", ())),
                        "reason": reason,
                        "actor_id": actor_id,
                    }
                )
                accepted["accepted_proposals"] = records
                revision = int(feature["revision"]) + 1
                await conn.execute(
                    "INSERT INTO swarm_product_specs (feature_id, revision, body) VALUES ($1,$2,$3::jsonb)",
                    feature["id"],
                    revision,
                    json.dumps(accepted),
                )
                await conn.execute(
                    """UPDATE swarm_product_features SET revision=$2, spec=$3::jsonb, status='draft',
                       approval_digest=NULL, approved_digest=NULL, blocker=NULL WHERE id=$1""",
                    feature["id"],
                    revision,
                    json.dumps(accepted),
                )
                if proposal["status"] == "proposed":
                    await _transition(conn, proposal, "triaged", actor_id, self.actor_host, reason)
                    proposal = dict(proposal)
                    proposal["status"] = "triaged"
                await _transition(conn, proposal, "awaiting_approval", actor_id, self.actor_host, reason)
        return ProposalResult(proposal_id, "awaiting_approval", feature_revision=revision, reason=reason)

    async def reject(self, product_id: str, proposal_id: str, *, actor_id: str, reason: str) -> ProposalResult:
        _decision_reason(reason)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM swarm_learning_proposals WHERE product_id=$1 AND id=$2 FOR UPDATE",
                    product_id,
                    proposal_id,
                )
                if row is None:
                    raise KeyError(proposal_id)
                if row["status"] == "rejected":
                    return ProposalResult(proposal_id, "rejected", reason="retry")
                if row["status"] in {"awaiting_approval", "eligible"}:
                    return ProposalResult(
                        proposal_id,
                        "blocked",
                        reason="proposal_already_in_plan",
                        blockers=("proposal_already_in_plan",),
                    )
                if row["status"] not in {"proposed", "triaged"}:
                    return ProposalResult(
                        proposal_id, "blocked", reason="proposal_not_rejectable", blockers=("proposal_not_rejectable",)
                    )
                await conn.execute(
                    "UPDATE swarm_learning_proposals SET status='rejected' WHERE product_id=$1 AND id=$2",
                    product_id,
                    proposal_id,
                )
                await conn.execute(
                    """INSERT INTO swarm_learning_proposal_events
                       (proposal_id, from_state, to_state, actor_id, actor_host, reason)
                       VALUES ($1,$2,'rejected',$3,$4,$5)""",
                    proposal_id,
                    row["status"],
                    actor_id,
                    self.actor_host,
                    reason,
                )
        return ProposalResult(proposal_id, "rejected", reason=reason)

    async def _inspect_locked(
        self, conn: Any, product_id: str, proposal: Any, feature: Any, registry: Any
    ) -> dict[str, Any]:
        data = dict(proposal)
        blockers = self._path_blockers(data["target_module"], registry)
        blockers.extend(
            await self._dependency_blockers(conn, {**data, "session_id": feature["session_id"]}, product_id)
        )
        if data["contract_impact"] != "none":
            blockers.append("contract_verification_proof_type_missing")
        blockers.extend(await self._budget_status(conn, feature["session_id"], _json(dict(feature), "budget", {})))
        if await self._existing_active_work(conn, {**data, "session_id": feature["session_id"]}):
            blockers.append("existing_active_project_work")
        return {"blockers": tuple(dict.fromkeys(blockers))}

    @staticmethod
    def _path_blockers(module: str, registry: Any) -> list[str]:
        del registry
        normalized = module.replace("\\", "/").strip("/")
        parts = tuple(part.lower() for part in normalized.split("/") if part)
        basename = parts[-1] if parts else ""
        protected_dirs = {
            ".git",
            ".agents",
            ".codex",
            ".claude",
            ".github",
            ".gitlab",
            "ci",
            "infra",
            "deploy",
            "helm",
            "terraform",
        }
        protected_names = {"agents.md", "claude.md", ".env", ".envrc", "dockerfile", "docker-compose.yml"}
        protected_tokens = re.compile(r"(?:secret|credential|password|token|policy|config)", re.IGNORECASE)
        protected = (
            any(part in protected_dirs for part in parts)
            or basename in protected_names
            or bool(protected_tokens.search(basename))
        )
        return ["protected_module_path"] if protected else []

    async def _dependency_blockers(self, conn: Any, proposal: Mapping[str, Any], product_id: str) -> list[str]:
        dependencies = _json(proposal, "dependencies", ())
        blockers: list[str] = []
        for dependency in dependencies:
            if dependency.startswith("proposal:"):
                row = await conn.fetchrow("SELECT product_id FROM swarm_learning_proposals WHERE id=$1", dependency[9:])
                if row is None:
                    blockers.append("dependency_unknown")
                elif row["product_id"] != product_id:
                    blockers.append("dependency_foreign")
                elif dependency[9:] == proposal["id"]:
                    blockers.append("dependency_cycle")
            elif dependency.startswith("task:"):
                task_id = dependency[5:]
                row = await conn.fetchrow(
                    """SELECT c.task_id FROM swarm_task_context c JOIN swarm_sessions s ON s.id=c.session_id
                       JOIN swarm_product_features f ON f.session_id=s.id
                       WHERE f.product_id=$1 AND (c.task_id=$2 OR c.local_id=$2) LIMIT 1""",
                    product_id,
                    task_id,
                )
                if row is None:
                    blockers.append("dependency_unknown")
        rows = await conn.fetch("SELECT id, dependencies FROM swarm_learning_proposals WHERE product_id=$1", product_id)
        graph = {row["id"]: tuple(_json(dict(row), "dependencies", ())) for row in rows}
        graph[proposal["id"]] = tuple(dependencies)
        if _has_proposal_cycle(graph, proposal["id"]):
            blockers.append("dependency_cycle")
        return blockers

    async def _budget_status(self, conn: Any, session_id: str, budget: Any) -> list[str]:
        if not isinstance(budget, dict):
            return ["feature_budget_unknown"]
        try:
            validate_budget(budget)
        except WorkflowBlocked:
            return ["feature_budget_unknown"]
        calls = await conn.fetchval("SELECT count(*) FROM swarm_agent_calls WHERE session_id=$1", session_id)
        elapsed = await conn.fetchval(
            "SELECT SUM(EXTRACT(EPOCH FROM (COALESCE(finished_at,NOW())-created_at))) FROM swarm_agent_calls WHERE session_id=$1",
            session_id,
        )
        blockers = []
        if int(calls or 0) >= budget["max_calls"]:
            blockers.append("feature_call_budget_exhausted")
        if elapsed is not None and float(elapsed) >= budget["max_elapsed_seconds"]:
            blockers.append("feature_time_budget_exhausted")
        return blockers

    @staticmethod
    async def _existing_active_work(conn: Any, proposal: Mapping[str, Any]) -> bool:
        session_id = proposal.get("session_id")
        project_id = proposal.get("target_project")
        product_id = proposal.get("product_id")
        if not product_id or not session_id or not project_id:
            return False
        row = await conn.fetchrow(
            """SELECT t.status FROM tasks t
               JOIN swarm_task_context c ON c.task_id=t.id
               JOIN swarm_sessions s ON s.id=c.session_id
               JOIN swarm_product_features f ON f.session_id=s.id
               WHERE f.product_id=$1 AND c.project_id=$2
                 AND t.status IN ('CLAIMED','IN_PROGRESS') LIMIT 1""",
            product_id,
            project_id,
        )
        return row is not None


async def _transition(
    conn: Any, proposal: Mapping[str, Any], to_state: str, actor_id: str, actor_host: str, reason: str
) -> None:
    await conn.execute(
        "UPDATE swarm_learning_proposals SET status=$2 WHERE id=$1",
        proposal["id"],
        to_state,
    )
    await conn.execute(
        """INSERT INTO swarm_learning_proposal_events
           (proposal_id, from_state, to_state, actor_id, actor_host, reason)
           VALUES ($1,$2,$3,$4,$5,$6)""",
        proposal["id"],
        proposal["status"],
        to_state,
        actor_id,
        actor_host,
        reason,
    )


def _json(row: Mapping[str, Any], key: str, default: Any) -> Any:
    value = row.get(key, default)
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _has_proposal_cycle(graph: Mapping[str, tuple[str, ...]], start: str) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, ()):
            if dependency.startswith("proposal:") and visit(dependency[9:]):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return visit(start)


def build_proposal_service(pool: Any, registry_path: str, *, actor_id: str, owner: bool = False) -> ProposalService:
    """Create a fresh host-pinned service with the registry's current projects."""

    registry = load_registry(registry_path)
    # ``owner`` is a host-authenticated grant supplied by the controller.  It
    # is intentionally not derived from request role/product/actor claims.
    owner_verified = bool(owner)
    from whilly.adapters.db.learning_proposals import PostgresProposalStore

    store = PostgresProposalStore(pool, actor_host=socket.gethostname())
    admission = PostgresProposalAdmission(pool, actor_host=store.actor_host)
    return ProposalService(
        store,
        registered_projects={"default": tuple(registry.projects)},
        admission_port=admission,
        owner_verified=owner_verified,
        actor_id=actor_id,
        registry=registry,
    )
