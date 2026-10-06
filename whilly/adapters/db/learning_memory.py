"""PostgreSQL implementation of the shared memory store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from whilly.swarm.learning.domain import KnowledgeRevision, Principal


class PostgresMemoryStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def append(self, revision: KnowledgeRevision) -> KnowledgeRevision:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    if revision.supersedes or revision.conflicts:
                        prior_ids = tuple(dict.fromkeys(tuple(x for x in (revision.supersedes,) if x) + revision.conflicts))
                        scope_rows = await conn.fetch(
                            """SELECT r.id, r.product_id, r.project_id, r.classification, p.redacted_at
                            FROM swarm_learning_revisions r
                            JOIN swarm_learning_payloads p ON p.revision_id = r.id
                            WHERE r.id = ANY($1::text[]) FOR SHARE""",
                            list(prior_ids),
                        )
                        by_id = {row["id"]: row for row in scope_rows}
                        if any(
                            item_id not in by_id
                            or by_id[item_id]["redacted_at"] is not None
                            or by_id[item_id]["product_id"] != revision.product_id
                            or by_id[item_id]["project_id"] != revision.project_id
                            or by_id[item_id]["classification"] != revision.classification
                            for item_id in prior_ids
                        ):
                            raise ValueError("memory revision reference is outside its scope")
                    await conn.execute(
                        """INSERT INTO swarm_learning_revisions
                        (id, product_id, project_id, kind, evidence_hash, observed_at, verified_at, expires_at,
                         classification, status, author_id, verifier_id, policy_version, supersedes, conflicts)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                        revision.id, revision.product_id, revision.project_id, revision.kind, revision.evidence_hash,
                        revision.observed_at, revision.verified_at, revision.expires_at, revision.classification,
                        revision.status, revision.author_id, revision.verifier_id, revision.policy_version,
                        revision.supersedes, list(revision.conflicts),
                    )
                    await conn.execute(
                        "INSERT INTO swarm_learning_payloads (revision_id, body, source_uri, source_sha) VALUES ($1,$2,$3,$4)",
                        revision.id, revision.body, revision.source_uri, revision.source_sha,
                    )
                except asyncpg.PostgresError as exc:
                    raise ValueError("memory revision conflicts with existing state") from exc
        return revision

    async def visible(self, principal: Principal, product_id: str, project_ids: tuple[str, ...]) -> list[KnowledgeRevision]:
        if product_id not in principal.product_ids or not set(project_ids).issubset(principal.project_ids):
            raise PermissionError("memory visibility denied")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT r.*, p.body, p.source_uri, p.source_sha FROM swarm_learning_revisions r
                JOIN swarm_learning_payloads p ON p.revision_id = r.id AND p.redacted_at IS NULL
                WHERE r.product_id=$1 AND r.classification = ANY($2::text[])
                  AND (r.project_id IS NULL OR r.project_id = ANY($3::text[]))
                  AND r.status NOT IN ('retracted', 'superseded')
                  AND NOT EXISTS (
                      SELECT 1 FROM swarm_learning_revisions successor
                      WHERE successor.supersedes = r.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM swarm_learning_revisions prior
                      JOIN swarm_learning_payloads prior_payload ON prior_payload.revision_id = prior.id
                      WHERE (r.supersedes = prior.id OR prior.id = ANY(r.conflicts))
                        AND prior_payload.redacted_at IS NOT NULL
                  ) ORDER BY r.observed_at, r.id""",
                product_id, list(principal.classifications), list(project_ids),
            )
        return [_revision(row) for row in rows]

    async def redact(self, principal: Principal, revision_id: str) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT r.product_id, r.project_id, r.classification FROM swarm_learning_revisions r
                    JOIN swarm_learning_payloads p ON p.revision_id=r.id
                    WHERE r.id=$1 AND p.redacted_at IS NULL FOR UPDATE""", revision_id
                )
                if (
                    row is None
                    or row["product_id"] not in principal.product_ids
                    or row["classification"] not in principal.classifications
                    or (row["project_id"] is not None and row["project_id"] not in principal.project_ids)
                ):
                    return False
                await conn.execute(
                    "UPDATE swarm_learning_payloads SET body='', source_uri='', source_sha=NULL, redacted_at=$2 WHERE revision_id=$1",
                    revision_id, datetime.now(timezone.utc),
                )
                return True


def _revision(row: Any) -> KnowledgeRevision:
    return KnowledgeRevision(
        id=row["id"], product_id=row["product_id"], project_id=row["project_id"], kind=row["kind"], body=row["body"],
        source_uri=row["source_uri"], source_sha=row["source_sha"], evidence_hash=row["evidence_hash"],
        observed_at=row["observed_at"], verified_at=row["verified_at"], expires_at=row["expires_at"],
        classification=row["classification"], status=row["status"], author_id=row["author_id"], verifier_id=row["verifier_id"],
        policy_version=row["policy_version"], supersedes=row["supersedes"], conflicts=tuple(row["conflicts"] or ()),
    )
