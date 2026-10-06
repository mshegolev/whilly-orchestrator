"""Transactional PostgreSQL adapter for product change-set state and audit."""

from __future__ import annotations

import json

import asyncpg

from whilly.swarm.change_set import (
    ChangeSetStatus, RepoChangeStatus, ProductChangeSet, RepoChange, Evidence, EvidenceOutcome,
    ExternalEffectReceipt, VersionConflict, EffectKeyConflict, TransitionError, canonical_digest, thaw,
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _evidence(value):
    return Evidence.from_dict(_json(value)) if value is not None else None


def _repo(row):
    return RepoChange(row["repo_id"], row["base_sha"], RepoChangeStatus(row["status"]), row["version"],
                      row["mandatory"], _evidence(row["last_evidence"]),
                      RepoChangeStatus(row["resume_status"]) if row["resume_status"] else None)


def _receipt(row):
    return ExternalEffectReceipt(row["effect_key"], row["change_id"], row["repo_id"], row["operation"],
                                 row["request_digest"], _evidence(row["evidence"]))


class ProductChangeSetStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def _get(self, conn, change_id, *, lock=False):
        row = await conn.fetchrow("SELECT * FROM product_change_sets WHERE id=$1" + (" FOR UPDATE" if lock else ""), change_id)
        if row is None:
            return None
        repos = await conn.fetch("SELECT * FROM repo_changes WHERE change_id=$1 ORDER BY repo_id", change_id)
        return ProductChangeSet(
            change_id=row["id"], product_id=row["product_id"], goal=row["goal"],
            acceptance_criteria=tuple(_json(row["acceptance_criteria"])), registry_snapshot=_json(row["registry_snapshot"]),
            registry_digest=row["registry_digest"], approval_digest=row["approval_digest"],
            base_shas=_json(row["base_shas"]), dependencies=_json(row["dependencies"]),
            repo_changes=tuple(_repo(repo) for repo in repos), status=ChangeSetStatus(row["status"]),
            version=row["version"], last_evidence=_evidence(row["last_evidence"]),
            resume_status=ChangeSetStatus(row["resume_status"]) if row["resume_status"] else None,
        )

    async def get(self, change_id: str) -> ProductChangeSet | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                return await self._get(conn, change_id)

    async def _event(self, conn, change_id, repo_id, version, source, target, evidence):
        key = "change-event:" + canonical_digest((change_id, repo_id, version))
        await conn.execute(
            "INSERT INTO change_set_events (change_id,repo_id,entity_version,from_status,to_status,evidence,idempotency_key) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)", change_id, repo_id, version, source, target,
            json.dumps(evidence.to_dict()), key,
        )

    async def create(self, value: ProductChangeSet) -> ProductChangeSet:
        if value.status != ChangeSetStatus.DRAFT or value.version != 1:
            raise ValueError("new_change_set_must_be_draft")
        if any(repo.status != RepoChangeStatus.PLANNED or repo.version != 1 or not repo.mandatory
               for repo in value.repo_changes):
            raise ValueError("new_repo_changes_must_be_mandatory_and_planned")
        creation = Evidence("creation", EvidenceOutcome.UNAVAILABLE, absence="verification_not_run")
        encoded = json.dumps(creation.to_dict())
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO product_change_sets
                    (id,product_id,goal,acceptance_criteria,registry_snapshot,registry_digest,approval_digest,base_shas,dependencies,last_evidence)
                    VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb)""",
                    value.change_id, value.product_id, value.goal, json.dumps(list(value.acceptance_criteria)),
                    json.dumps(thaw(value.registry_snapshot)), value.registry_digest, value.approval_digest,
                    json.dumps(thaw(value.base_shas)), json.dumps(thaw(value.dependencies)), encoded,
                )
                for repo in value.repo_changes:
                    await conn.execute(
                        "INSERT INTO repo_changes (change_id,repo_id,base_sha,last_evidence) VALUES ($1,$2,$3,$4::jsonb)",
                        value.change_id, repo.repo_id, repo.base_sha, encoded,
                    )
                    await self._event(conn, value.change_id, repo.repo_id, 1, None, RepoChangeStatus.PLANNED, creation)
                await self._event(conn, value.change_id, None, 1, None, ChangeSetStatus.DRAFT, creation)
                return await self._get(conn, value.change_id)

    async def transition(self, change_id: str, expected_version: int, target: ChangeSetStatus,
                         evidence: Evidence) -> ProductChangeSet:
        if type(expected_version) is not int:
            raise ValueError("expected_version_invalid")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await self._get(conn, change_id, lock=True)
                if current is None:
                    raise KeyError(change_id)
                if current.version != expected_version:
                    raise VersionConflict("change_set_version_conflict")
                updated = current.transition(target, evidence)
                row = await conn.fetchrow(
                    """UPDATE product_change_sets SET status=$3,version=$4,last_evidence=$5::jsonb,
                    resume_status=$6,approval_digest=$7,updated_at=NOW() WHERE id=$1 AND version=$2 RETURNING id""",
                    change_id, expected_version, updated.status, updated.version, json.dumps(evidence.to_dict()),
                    updated.resume_status, updated.approval_digest,
                )
                if row is None:
                    raise VersionConflict("change_set_version_conflict")
                await self._event(conn, change_id, None, updated.version, current.status, updated.status, evidence)
                return await self._get(conn, change_id)

    async def transition_repo(self, change_id: str, repo_id: str, expected_version: int,
                              target: RepoChangeStatus, evidence: Evidence) -> RepoChange:
        if type(expected_version) is not int:
            raise ValueError("expected_version_invalid")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await self._get(conn, change_id, lock=True)
                if current is None:
                    raise KeyError(change_id)
                repo = next((repo for repo in current.repo_changes if repo.repo_id == repo_id), None)
                if repo is None:
                    raise KeyError(repo_id)
                if repo.version != expected_version:
                    raise VersionConflict("repo_change_version_conflict")
                target = RepoChangeStatus(target)
                if target == RepoChangeStatus.NOT_IMPACTED:
                    phases = {ChangeSetStatus.DRAFT, ChangeSetStatus.PLANNED}
                elif target in {RepoChangeStatus.REVERT_OPEN, RepoChangeStatus.REVERTED, RepoChangeStatus.ROLLBACK_FAILED}:
                    phases = {ChangeSetStatus.ROLLING_BACK, ChangeSetStatus.ROLLBACK_FAILED}
                elif target in {RepoChangeStatus.MERGED, RepoChangeStatus.ARTIFACT_READY}:
                    phases = {ChangeSetStatus.MERGING, ChangeSetStatus.MERGED, ChangeSetStatus.DEPLOYING_STAGE,
                              ChangeSetStatus.ACCEPTING_STAGE}
                else:
                    phases = {ChangeSetStatus.EXECUTING, ChangeSetStatus.VERIFYING_REPOS,
                              ChangeSetStatus.VERIFYING_INTEGRATION, ChangeSetStatus.READY_TO_MERGE,
                              ChangeSetStatus.MERGING}
                if current.status not in phases:
                    raise TransitionError("repo_transition_outside_product_phase")
                updated = repo.transition(target, evidence)
                row = await conn.fetchrow(
                    """UPDATE repo_changes SET status=$4,version=$5,mandatory=$6,last_evidence=$7::jsonb,
                    resume_status=$8,updated_at=NOW() WHERE change_id=$1 AND repo_id=$2 AND version=$3 RETURNING *""",
                    change_id, repo_id, expected_version, updated.status, updated.version, updated.mandatory,
                    json.dumps(evidence.to_dict()), updated.resume_status,
                )
                if row is None:
                    raise VersionConflict("repo_change_version_conflict")
                await self._event(conn, change_id, repo_id, updated.version, repo.status, updated.status, evidence)
                return _repo(row)

    async def events(self, change_id: str) -> tuple[dict, ...]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM change_set_events WHERE change_id=$1 ORDER BY id", change_id)
        return tuple({**dict(row), "evidence": _json(row["evidence"])} for row in rows)

    async def record_effect(self, receipt: ExternalEffectReceipt) -> ExternalEffectReceipt:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """INSERT INTO external_effect_receipts (effect_key,change_id,repo_id,operation,request_digest,evidence)
                    VALUES ($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT (effect_key) DO NOTHING RETURNING *""",
                    receipt.effect_key, receipt.change_id, receipt.repo_id, receipt.operation,
                    receipt.request_digest, json.dumps(receipt.evidence.to_dict()),
                )
                if row is None:
                    row = await conn.fetchrow("SELECT * FROM external_effect_receipts WHERE effect_key=$1", receipt.effect_key)
                    if row is None:
                        raise EffectKeyConflict("external_effect_receipt_unavailable")
                    stored = _receipt(row)
                    if (stored.change_id, stored.repo_id, stored.operation, stored.request_digest) != (
                        receipt.change_id, receipt.repo_id, receipt.operation, receipt.request_digest
                    ):
                        raise EffectKeyConflict("external_effect_key_scope_or_request_conflict")
                return _receipt(row)

    async def get_effect(self, effect_key: str) -> ExternalEffectReceipt | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM external_effect_receipts WHERE effect_key=$1", effect_key)
        return _receipt(row) if row is not None else None

    async def effects(self, change_id: str) -> tuple[ExternalEffectReceipt, ...]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM external_effect_receipts WHERE change_id=$1 ORDER BY created_at,effect_key", change_id)
        return tuple(_receipt(row) for row in rows)
