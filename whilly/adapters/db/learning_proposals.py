"""PostgreSQL persistence for immutable learning task proposals."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from whilly.swarm.learning.proposals import (
    ProposalEvent,
    ProposalResult,
    ProposalStore,
    PROPOSAL_STATES,
    TaskProposal,
    can_transition,
)


class PostgresProposalStore(ProposalStore):
    """Persist proposal content once and append lifecycle events atomically."""

    def __init__(self, pool: asyncpg.Pool, *, actor_host: str) -> None:
        if not actor_host.strip():
            raise ValueError("actor_host must be non-empty")
        self.pool = pool
        self.actor_host = actor_host

    async def validate_origin(self, product_id: str, feature_id: str, task_id: str | None) -> None:
        async with self.pool.acquire() as conn:
            feature = await conn.fetchrow(
                "SELECT product_id, session_id FROM swarm_product_features WHERE id=$1",
                feature_id,
            )
            if feature is None or feature["product_id"] != product_id:
                raise PermissionError("origin feature is not in the proposal product")
            if task_id is not None:
                task = await conn.fetchrow(
                    """SELECT c.task_id FROM swarm_task_context c
                    WHERE c.session_id=$1 AND (c.local_id=$2 OR c.task_id=$2)""",
                    feature["session_id"],
                    task_id,
                )
                if task is None:
                    raise PermissionError("origin task is not in the feature session")

    async def persist(self, product_id: str, actor_id: str, proposal: TaskProposal) -> ProposalResult:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._validate_origin_locked(
                    conn, product_id, proposal.origin_feature_id, proposal.origin_task_id
                )
                lock_key = "\x1f".join(
                    (product_id, proposal.target_project, proposal.target_module, proposal.fingerprint)
                )
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key)
                existing = await conn.fetchrow(
                    """SELECT id, fingerprint, status FROM swarm_learning_proposals
                    WHERE product_id=$1 AND target_project=$2 AND target_module=$3 AND fingerprint=$4
                    FOR SHARE""",
                    product_id,
                    proposal.target_project,
                    proposal.target_module,
                    proposal.fingerprint,
                )
                if existing is not None:
                    return ProposalResult(existing["id"], existing["status"], existing["id"], "duplicate proposal")
                by_id = await conn.fetchrow(
                    "SELECT fingerprint, status FROM swarm_learning_proposals WHERE id=$1 AND product_id=$2 FOR SHARE",
                    proposal.id,
                    product_id,
                )
                if by_id is not None:
                    if by_id["fingerprint"] != proposal.fingerprint:
                        raise ValueError("proposal id already exists with different content")
                    return ProposalResult(proposal.id, by_id["status"], proposal.id, "retry")
                id_conflict = await conn.fetchval("SELECT 1 FROM swarm_learning_proposals WHERE id=$1", proposal.id)
                if id_conflict is not None:
                    raise ValueError("proposal id conflicts with durable state")
                await conn.execute(
                    """INSERT INTO swarm_learning_proposals
                    (id, product_id, origin_feature_id, origin_task_id, target_project, target_module,
                     evidence_refs, outcome, contract_impact, acceptance, dependencies, resource_class,
                     fingerprint, status, actor_id, actor_host)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10::jsonb,$11::jsonb,$12,$13,'proposed',$14,$15)""",
                    proposal.id,
                    product_id,
                    proposal.origin_feature_id,
                    proposal.origin_task_id,
                    proposal.target_project,
                    proposal.target_module,
                    json.dumps(proposal.evidence_refs),
                    proposal.outcome,
                    proposal.contract_impact,
                    json.dumps(proposal.acceptance),
                    json.dumps(proposal.dependencies),
                    proposal.resource_class,
                    proposal.fingerprint,
                    actor_id,
                    self.actor_host,
                )
                await conn.execute(
                    """INSERT INTO swarm_learning_proposal_events
                    (proposal_id, from_state, to_state, actor_id, actor_host, reason)
                    VALUES ($1,NULL,'proposed',$2,$3,NULL)""",
                    proposal.id,
                    actor_id,
                    self.actor_host,
                )
        return ProposalResult(proposal.id, "proposed")

    async def _validate_origin_locked(self, conn: Any, product_id: str, feature_id: str, task_id: str | None) -> None:
        feature = await conn.fetchrow(
            "SELECT product_id, session_id FROM swarm_product_features WHERE id=$1 FOR UPDATE",
            feature_id,
        )
        if feature is None or feature["product_id"] != product_id:
            raise PermissionError("origin feature is not in the proposal product")
        if task_id is not None:
            task = await conn.fetchrow(
                """SELECT c.task_id FROM swarm_task_context c
                WHERE c.session_id=$1 AND (c.local_id=$2 OR c.task_id=$2) FOR UPDATE""",
                feature["session_id"],
                task_id,
            )
            if task is None:
                raise PermissionError("origin task is not in the feature session")

    async def get(self, product_id: str, proposal_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM swarm_learning_proposals WHERE product_id=$1 AND id=$2", product_id, proposal_id
            )
        return _proposal_row(row) if row else None

    async def list(self, product_id: str, *, target_project: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM swarm_learning_proposals WHERE product_id=$1"
        args: list[Any] = [product_id]
        if target_project is not None:
            query += " AND target_project=$2"
            args.append(target_project)
        query += " ORDER BY created_at, id"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [_proposal_row(row) for row in rows]

    async def events(self, product_id: str, proposal_id: str) -> tuple[ProposalEvent, ...]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT e.proposal_id, e.from_state, e.to_state, e.actor_id, e.actor_host, e.reason "
                "FROM swarm_learning_proposal_events e JOIN swarm_learning_proposals p ON p.id=e.proposal_id "
                "WHERE p.product_id=$1 AND e.proposal_id=$2 ORDER BY e.event_id",
                product_id,
                proposal_id,
            )
        return tuple(ProposalEvent(**dict(row)) for row in rows)

    async def transition(
        self,
        product_id: str,
        proposal_id: str,
        expected_state: str,
        to_state: str,
        *,
        actor_id: str,
        reason: str | None = None,
    ) -> ProposalResult:
        if expected_state not in PROPOSAL_STATES or to_state not in PROPOSAL_STATES:
            raise ValueError("invalid proposal state")
        if not can_transition(expected_state, to_state):
            raise ValueError("illegal proposal transition")
        if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 256:
            raise ValueError("actor_id must be a non-empty string of at most 256 characters")
        result = ProposalResult(proposal_id, to_state, reason=reason)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status FROM swarm_learning_proposals WHERE product_id=$1 AND id=$2 FOR UPDATE",
                    product_id,
                    proposal_id,
                )
                if row is None:
                    raise KeyError(proposal_id)
                if row["status"] != expected_state:
                    raise ValueError("proposal state changed")
                await conn.execute("UPDATE swarm_learning_proposals SET status=$2 WHERE id=$1", proposal_id, to_state)
                await conn.execute(
                    """INSERT INTO swarm_learning_proposal_events
                    (proposal_id, from_state, to_state, actor_id, actor_host, reason)
                    VALUES ($1,$2,$3,$4,$5,$6)""",
                    proposal_id,
                    expected_state,
                    to_state,
                    actor_id,
                    self.actor_host,
                    reason,
                )
        return result


def _proposal_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("evidence_refs", "acceptance", "dependencies"):
        if isinstance(result.get(key), (str, bytes, bytearray)):
            result[key] = json.loads(result[key])
        result[key] = tuple(result[key])
    return result
