"""Configuration, composition and read-only history queries for L2 delivery."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg

from whilly.adapters.db.learning_messages import PostgresMessageStore
from whilly.swarm.learning.messages import DeliveryPolicy, MessageService


def delivery_policy_from_env() -> DeliveryPolicy | None:
    """Parse the exact bounded delivery policy from ``WHILLY_SWARM_DELIVERY_POLICY``."""
    raw = os.environ.get("WHILLY_SWARM_DELIVERY_POLICY")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("WHILLY_SWARM_DELIVERY_POLICY must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("WHILLY_SWARM_DELIVERY_POLICY must be a JSON object")
    required = {"max_payload_bytes", "max_hops", "max_fanout"}
    if set(value) != required:
        raise ValueError("WHILLY_SWARM_DELIVERY_POLICY requires exactly max_payload_bytes, max_hops, max_fanout")
    if any(not isinstance(value[name], int) or isinstance(value[name], bool) for name in required):
        raise ValueError("WHILLY_SWARM_DELIVERY_POLICY values must be integers")
    try:
        return DeliveryPolicy(**value)
    except ValueError as exc:
        raise ValueError(f"invalid delivery policy: {exc}") from exc


def build_delivery_service(
    pool: asyncpg.Pool,
    registry: Any,
    *,
    actor_id: str,
    sender_scopes: tuple[tuple[str, str, str], ...],
    recipient_scopes: tuple[tuple[str, str, str], ...],
    policy: DeliveryPolicy | None,
) -> MessageService:
    """Compose MessageService with explicit host grants; controller owns lifecycle."""
    registered_projects = {"default": tuple(registry.projects)}
    registered_roles: dict[str, list[str]] = {project_id: [] for project_id in registry.projects}
    for role_id, role in registry.roles.items():
        for project_id in role.projects:
            if project_id in registered_roles:
                registered_roles[project_id].append(role_id)
    return MessageService(
        PostgresMessageStore(pool),
        policy,
        lambda: datetime.now(timezone.utc),
        registered_projects,
        registered_roles,
        recipient_grants={actor_id: tuple(recipient_scopes)},
        sender_grants={actor_id: tuple(sender_scopes)},
    )


async def history_for_projects(
    pool: asyncpg.Pool,
    product_id: str,
    project_ids: tuple[str, ...],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read at most 100 product-scoped messages without changing delivery state."""
    bounded_limit = min(max(int(limit), 1), 100)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, product_id, feature_id, task_id, sender_id, recipient_project,
            recipient_role, kind, correlation_id, causation_id, idempotency_key,
            expires_at, hop_count, payload, evidence_refs, state
            FROM swarm_learning_messages
            WHERE product_id=$1 AND recipient_project = ANY($2::text[])
            ORDER BY created_at DESC, id DESC LIMIT $3""",
            product_id,
            list(project_ids),
            bounded_limit,
        )
    return [_decode_history(dict(row)) for row in rows]


def _decode_history(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("payload", "evidence_refs"):
        value = row.get(key)
        if isinstance(value, (str, bytes, bytearray)):
            row[key] = json.loads(value)
    for key in ("expires_at",):
        value = row.get(key)
        if isinstance(value, datetime):
            row[key] = value.isoformat()
    return row


__all__ = ["build_delivery_service", "delivery_policy_from_env", "history_for_projects"]
