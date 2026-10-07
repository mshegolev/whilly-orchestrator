"""Durable PostgreSQL storage for product chat and feature specifications."""

from __future__ import annotations

import json
import hashlib
import secrets
from typing import Any

import asyncpg


class ProductStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def ensure_product(self, name: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO swarm_products (id, name) VALUES ('default', $1)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name RETURNING *""",
                name,
            )
        return dict(row)

    async def list_products(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            return [dict(row) for row in await conn.fetch("SELECT * FROM swarm_products ORDER BY id")]

    async def add_message(self, product_id: str, body: str, sender: str = "user") -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO swarm_product_messages (product_id, sender, body) VALUES ($1, $2, $3) RETURNING *",
                product_id,
                sender,
                body,
            )
        return dict(row)

    async def list_messages(self, product_id: str) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            return [
                dict(row)
                for row in await conn.fetch(
                    "SELECT * FROM swarm_product_messages WHERE product_id = $1 ORDER BY id", product_id
                )
            ]

    async def create_feature(self, **values: Any) -> dict[str, Any]:
        feature_id = "f_" + secrets.token_hex(8)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO swarm_product_features
                (id, product_id, title, intent, session_id, budget) VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING *""",
                feature_id,
                values["product_id"],
                values["title"],
                values["intent"],
                values["session_id"],
                json.dumps(values.get("budget", {"max_calls": 60, "max_elapsed_seconds": 7200})),
            )
        return _decode(dict(row))

    async def get_feature(self, feature_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM swarm_product_features WHERE id = $1", feature_id)
        return _decode(dict(row)) if row else None

    async def list_features(self, product_id: str = "default") -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT f.*
                FROM swarm_product_features AS f
                JOIN swarm_sessions AS s ON s.id = f.session_id
                WHERE f.product_id=$1 AND s.archived=FALSE
                ORDER BY f.created_at""",
                product_id,
            )
        return [_decode(dict(row)) for row in rows]

    async def prepare(self, feature_id: str, **values: Any) -> dict[str, Any]:
        execution_binding = values.get("execution_binding")
        if not isinstance(execution_binding, dict) or not execution_binding:
            raise ValueError("execution_binding_required")
        spec = values.get("spec")
        if not isinstance(spec, dict) or spec.get("execution_binding") != execution_binding:
            raise ValueError("execution_binding_host_owned")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT revision, approval_digest, status FROM swarm_product_features WHERE id=$1 FOR UPDATE",
                    feature_id,
                )
                if row is None:
                    raise KeyError(feature_id)
                if row["status"] == "running":
                    raise ValueError("running feature must be stopped before mutation")
                expected_revision = values.get("expected_revision")
                if expected_revision is not None and int(row["revision"]) != expected_revision:
                    raise ValueError("stale feature revision")
                revision = int(row["revision"]) + 1
                binding = {
                    "spec": values["spec"],
                    "execution_binding": execution_binding,
                    "revision": revision,
                    "plan_revision": values["plan_revision"],
                    "registry_hash": values["registry_hash"],
                    "base_shas": values["base_shas"],
                    "profiles": values["profiles"],
                    "budget": values["budget"],
                }
                digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                await conn.execute(
                    """INSERT INTO swarm_product_specs (feature_id, revision, body, binding) VALUES ($1,$2,$3::jsonb,$4::jsonb)""",
                    feature_id,
                    revision,
                    json.dumps(values["spec"]),
                    json.dumps(binding),
                )
                updated = await conn.fetchrow(
                    """UPDATE swarm_product_features SET revision=$2, spec=$3::jsonb,
                    status='planned', approval_digest=$9, approved_digest=NULL, plan_revision=$4,
                    registry_hash=$5, base_shas=$6::jsonb, profiles=$7::jsonb, budget=$8::jsonb, blocker=NULL
                    WHERE id=$1 AND status <> 'running' RETURNING *""",
                    feature_id,
                    revision,
                    json.dumps(values["spec"]),
                    values["plan_revision"],
                    values["registry_hash"],
                    json.dumps(values["base_shas"]),
                    json.dumps(values["profiles"]),
                    json.dumps(values["budget"]),
                    digest,
                )
        return _decode(dict(updated))

    async def approve(self, feature_id: str, revision: int, digest: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE swarm_product_features SET status='approved', approved_digest=$3
                WHERE id=$1 AND revision=$2 AND status='planned' AND approval_digest=$3 RETURNING *""",
                feature_id,
                revision,
                digest,
            )
        return _decode(dict(row)) if row else None

    async def invalidate(self, feature_id: str, reason: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE swarm_product_features SET status='blocked', blocker=$2, approval_digest=NULL, approved_digest=NULL WHERE id=$1 RETURNING *",
                feature_id,
                reason,
            )
        return _decode(dict(row)) if row else None

    async def set_spec(self, feature_id: str, body: Any) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT revision, status FROM swarm_product_features WHERE id=$1 FOR UPDATE", feature_id
                )
                if current is None:
                    return None
                if current["status"] == "running":
                    raise ValueError("running feature must be stopped before mutation")
                revision = int(current["revision"]) + 1
                encoded = json.dumps(body)
                await conn.execute(
                    "INSERT INTO swarm_product_specs (feature_id, revision, body) VALUES ($1,$2,$3::jsonb)",
                    feature_id,
                    revision,
                    encoded,
                )
                row = await conn.fetchrow(
                    """UPDATE swarm_product_features SET revision=$2, spec=$3::jsonb, status='draft',
                    approval_digest=NULL, approved_digest=NULL, blocker=NULL WHERE id=$1 RETURNING *""",
                    feature_id,
                    revision,
                    encoded,
                )
        return _decode(dict(row)) if row else None

    async def set_budget(self, feature_id: str, budget: dict[str, int]) -> dict[str, Any] | None:
        max_calls = budget.get("max_calls")
        max_elapsed_seconds = budget.get("max_elapsed_seconds")
        if not isinstance(max_calls, int) or not isinstance(max_elapsed_seconds, int):
            raise ValueError("budget requires max_calls and max_elapsed_seconds")
        if not 1 <= max_calls <= 1000 or not 1 <= max_elapsed_seconds <= 86400:
            raise ValueError("max_calls must be 1..1000 and max_elapsed_seconds must be 1..86400")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE swarm_product_features SET revision=revision+1, budget=$2::jsonb, status='draft',
                approval_digest=NULL, approved_digest=NULL WHERE id=$1 AND status <> 'running' RETURNING *""",
                feature_id,
                json.dumps({"max_calls": max_calls, "max_elapsed_seconds": max_elapsed_seconds}),
            )
        return _decode(dict(row)) if row else None

    async def begin_run(self, feature_id: str, digest: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE swarm_product_features SET status='running' WHERE id=$1
                AND status='approved' AND approved_digest=$2 RETURNING *""",
                feature_id,
                digest,
            )
        return _decode(dict(row)) if row else None

    async def finish(
        self, feature_id: str, status: str, *, expected_digest: str | None = None
    ) -> dict[str, Any] | None:
        if status not in {"review", "mr_ready", "blocked"}:
            raise ValueError("invalid terminal feature status")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE swarm_product_features SET status=$2, blocker=NULL WHERE id=$1 "
                "AND approved_digest IS NOT NULL AND status IN ('running','review','mr_ready') "
                "AND ($3::text IS NULL OR approved_digest=$3) RETURNING *",
                feature_id,
                status,
                expected_digest,
            )
        return _decode(dict(row)) if row else None


def _decode(value: dict[str, Any]) -> dict[str, Any]:
    for key in ("spec", "base_shas", "profiles", "budget"):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return value
