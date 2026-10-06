"""PostgreSQL transactional outbox/inbox for durable learning messages."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.messages import DeliveryReceipt, MessageEnvelope, MessageStore


class PostgresMessageStore(MessageStore):
    """Persist one message and one inbox row atomically, with retry checks."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def persist(self, envelope: MessageEnvelope, max_fanout: int) -> DeliveryReceipt:
        if max_fanout <= 0:
            raise ValueError("max_fanout must be positive")
        if envelope.correlation_id is None:
            raise ValueError("correlation_id is required for bounded fan-out")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        f"{envelope.product_id}\x1f{envelope.sender_id}\x1f{envelope.correlation_id}",
                    )
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        f"{envelope.product_id}\x1f{envelope.sender_id}\x1fidem\x1f{envelope.idempotency_key}",
                    )
                    existing = await conn.fetchrow(
                        """SELECT id, sender_id, recipient_project, recipient_role,
                        idempotency_key, fingerprint, state, rejected_reason
                        FROM swarm_learning_messages
                        WHERE product_id=$1 AND sender_id=$2 AND idempotency_key=$3
                        FOR SHARE""",
                        envelope.product_id,
                        envelope.sender_id,
                        envelope.idempotency_key,
                    )
                    if existing is not None:
                        if existing["fingerprint"] != envelope.fingerprint():
                            raise ValueError("retry changed message payload under the same idempotency key")
                        return _receipt(existing)
                    if envelope.causation_id is not None:
                        parent = await conn.fetchrow(
                            "SELECT product_id, hop_count FROM swarm_learning_messages WHERE id=$1 FOR SHARE",
                            envelope.causation_id,
                        )
                        if (
                            parent is None
                            or parent["product_id"] != envelope.product_id
                            or envelope.hop_count != parent["hop_count"] + 1
                        ):
                            raise ValueError("message causation parent or hop is invalid")
                    group_count = await conn.fetchval(
                        """SELECT COUNT(*) FROM swarm_learning_messages
                        WHERE product_id=$1 AND sender_id=$2 AND correlation_id=$3""",
                        envelope.product_id,
                        envelope.sender_id,
                        envelope.correlation_id,
                    )
                    if group_count >= max_fanout:
                        raise ValueError("message fan-out exceeds policy limit")
                    await conn.execute(
                        """INSERT INTO swarm_learning_messages
                        (id, product_id, feature_id, task_id, sender_id, recipient_project,
                         recipient_role, kind, correlation_id, causation_id, idempotency_key,
                         expires_at, hop_count, payload, evidence_refs, fingerprint, state)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15::jsonb,$16,'persisted')""",
                        envelope.id,
                        envelope.product_id,
                        envelope.feature_id,
                        envelope.task_id,
                        envelope.sender_id,
                        envelope.recipient_project,
                        envelope.recipient_role,
                        envelope.kind,
                        envelope.correlation_id,
                        envelope.causation_id,
                        envelope.idempotency_key,
                        envelope.expires_at,
                        envelope.hop_count,
                        _json(envelope.payload),
                        _json(envelope.evidence_refs),
                        envelope.fingerprint(),
                    )
                    await conn.execute(
                        "INSERT INTO swarm_learning_message_outbox (message_id) VALUES ($1)", envelope.id
                    )
                    await conn.execute(
                        """INSERT INTO swarm_learning_message_inbox
                        (message_id, recipient_project, recipient_role, state)
                        VALUES ($1,$2,$3,'persisted')""",
                        envelope.id,
                        envelope.recipient_project,
                        envelope.recipient_role,
                    )
                    row = await conn.fetchrow(
                        """SELECT id, sender_id, recipient_project, recipient_role,
                        idempotency_key, fingerprint, state, rejected_reason
                        FROM swarm_learning_messages
                        WHERE product_id=$1 AND sender_id=$2 AND idempotency_key=$3""",
                        envelope.product_id,
                        envelope.sender_id,
                        envelope.idempotency_key,
                    )
                except asyncpg.PostgresError as exc:
                    raise ValueError("message conflicts with existing durable state") from exc
                if row is None:
                    raise ValueError("message persistence returned no receipt")
        return _receipt(row)

    async def get(self, message_id: str) -> MessageEnvelope | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM swarm_learning_messages WHERE id=$1", message_id)
        return _envelope(row) if row is not None else None

    async def list(
        self, principal: Principal, recipient_project: str, recipient_role: str, now: datetime
    ) -> list[MessageEnvelope]:
        async with self.pool.acquire() as conn:
            await _expire(conn, now)
            rows = await conn.fetch(
                """SELECT m.* FROM swarm_learning_messages m
                JOIN swarm_learning_message_inbox i ON i.message_id=m.id
                WHERE m.recipient_project=$1 AND m.recipient_role=$2
                  AND m.product_id = ANY($3::text[])
                  AND i.state IN ('persisted','delivered') AND m.expires_at > $4
                ORDER BY m.created_at, m.id""",
                recipient_project,
                recipient_role,
                list(principal.product_ids),
                now,
            )
        return [_envelope(row) for row in rows]

    async def deliver(
        self, principal: Principal, recipient_project: str, recipient_role: str, now: datetime
    ) -> list[MessageEnvelope]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await _expire(conn, now)
                rows = await conn.fetch(
                    """SELECT m.* FROM swarm_learning_messages m
                    JOIN swarm_learning_message_inbox i ON i.message_id=m.id
                    WHERE m.recipient_project=$1 AND m.recipient_role=$2
                      AND m.product_id = ANY($3::text[])
                      AND i.state IN ('persisted','delivered') AND m.expires_at > $4
                    ORDER BY m.created_at, m.id FOR UPDATE OF i""",
                    recipient_project,
                    recipient_role,
                    list(principal.product_ids),
                    now,
                )
                for row in rows:
                    await conn.execute(
                        "UPDATE swarm_learning_message_inbox SET state='delivered', delivered_at=COALESCE(delivered_at,$2) WHERE message_id=$1",
                        row["id"],
                        now,
                    )
                    await conn.execute(
                        "UPDATE swarm_learning_messages SET state='delivered', delivered_at=COALESCE(delivered_at,$2) WHERE id=$1",
                        row["id"],
                        now,
                    )
                    await conn.execute(
                        "UPDATE swarm_learning_message_outbox SET delivered_at=COALESCE(delivered_at,$2) WHERE message_id=$1",
                        row["id"],
                        now,
                    )
        return [_envelope(row) for row in rows]

    async def acknowledge(
        self, principal: Principal, message_id: str, recipient_project: str, recipient_role: str, now: datetime
    ) -> DeliveryReceipt:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT m.*, i.state AS inbox_state FROM swarm_learning_messages m
                    JOIN swarm_learning_message_inbox i ON i.message_id=m.id
                    WHERE m.id=$1 AND m.product_id = ANY($4::text[])
                      AND i.recipient_project=$2 AND i.recipient_role=$3 FOR UPDATE""",
                    message_id,
                    recipient_project,
                    recipient_role,
                    list(principal.product_ids),
                )
                if row is None:
                    raise PermissionError("message acknowledgment is not authorized")
                if row["expires_at"] <= now and row["inbox_state"] != "acknowledged":
                    await _expire_one(conn, message_id, now)
                    row = dict(row)
                    row["state"] = "expired"
                    row["rejected_reason"] = "message expired"
                    return _receipt(row)
                await conn.execute(
                    "UPDATE swarm_learning_message_inbox SET state='acknowledged', acknowledged_at=$2 WHERE message_id=$1",
                    message_id,
                    now,
                )
                await conn.execute(
                    "UPDATE swarm_learning_messages SET state='acknowledged', acknowledged_at=$2 WHERE id=$1",
                    message_id,
                    now,
                )
                row = dict(row)
                row["state"] = "acknowledged"
        return _receipt(row)

    async def expire(self, now: datetime) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await _expire(conn, now)


async def _expire(conn: Any, now: datetime) -> int:
    result = await conn.execute(
        """UPDATE swarm_learning_messages m SET state='expired'
        FROM swarm_learning_message_inbox i
        WHERE i.message_id=m.id AND m.expires_at <= $1
          AND i.state IN ('persisted','delivered')""",
        now,
    )
    await conn.execute(
        """UPDATE swarm_learning_message_inbox i SET state='expired'
        FROM swarm_learning_messages m
        WHERE i.message_id=m.id AND m.expires_at <= $1
          AND i.state IN ('persisted','delivered')""",
        now,
    )
    return int(result.split()[-1])


async def _expire_one(conn: Any, message_id: str, now: datetime) -> None:
    await conn.execute("UPDATE swarm_learning_messages SET state='expired' WHERE id=$1", message_id)
    await conn.execute("UPDATE swarm_learning_message_inbox SET state='expired' WHERE message_id=$1", message_id)


def _json(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"))


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "items"):
        return dict(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _decode_jsonb(value: Any) -> Any:
    """Accept asyncpg's decoded JSONB and test doubles returning JSON text."""
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _envelope(row: Any) -> MessageEnvelope:
    return MessageEnvelope(
        id=row["id"],
        product_id=row["product_id"],
        feature_id=row["feature_id"],
        task_id=row["task_id"],
        sender_id=row["sender_id"],
        recipient_project=row["recipient_project"],
        recipient_role=row["recipient_role"],
        kind=row["kind"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        idempotency_key=row["idempotency_key"],
        expires_at=row["expires_at"],
        hop_count=row["hop_count"],
        payload=_decode_jsonb(row["payload"]),
        evidence_refs=tuple(_decode_jsonb(row["evidence_refs"]) or ()),
    )


def _receipt(row: Any) -> DeliveryReceipt:
    return DeliveryReceipt(
        message_id=row["id"],
        state=row["state"],
        sender_id=row["sender_id"],
        recipient_project=row["recipient_project"],
        recipient_role=row["recipient_role"],
        idempotency_key=row["idempotency_key"],
        reason=row.get("rejected_reason") if isinstance(row, dict) else row["rejected_reason"],
    )
