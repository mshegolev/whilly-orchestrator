"""File IPC for sandboxed workers; the coordinator alone accesses PostgreSQL."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from whilly.adapters.filesystem.swarm_mailbox import MAX_BYTES, MailboxDirectory, MailboxIntegrityError
from whilly.swarm.store import SwarmStoreError

MAILBOX_ENV = "WHILLY_SWARM_MAILBOX"


def enqueue(root: Path, request: dict) -> str:
    if len(json.dumps(request).encode()) > MAX_BYTES:
        raise ValueError("mailbox request exceeds size limit")
    nonce = uuid.uuid4().hex
    with MailboxDirectory(root) as mailbox:
        if mailbox.entry_count(100) >= 100:
            raise ValueError("mailbox backlog limit reached")
        mailbox.write_request(f"{nonce}.json", request)
    return nonce


def read_inbox(root: Path, session_id: str, recipient: str, *, include_acked: bool = False) -> list[dict]:
    with MailboxDirectory(root) as mailbox:
        data = json.loads(mailbox.read_state("inbox.json"))
    if data["session_id"] != session_id or data["recipient"] != recipient:
        raise ValueError("mailbox scope mismatch")
    return [m for m in data["messages"] if include_acked or not m.get("acked_at")]


async def sync_mailbox(store: Any, root: Path, *, session_id: str, recipient: str) -> None:
    """Drain up to 100 local requests, then refresh the worker's scoped inbox."""
    with MailboxDirectory(root) as mailbox:
        for name in mailbox.request_names(100):
            if mailbox.has_receipt(name):
                if not mailbox.receipt_is_special_rejection(name):
                    mailbox.remove_request(name)
                continue
            try:
                request = json.loads(mailbox.read_request(name))
                if not isinstance(request, dict) or request.get("sender") != recipient:
                    raise ValueError("sender scope mismatch")
                if request.get("op") == "send":
                    mid = await store.send_message(
                        session_id,
                        sender=recipient,
                        recipient=request["recipient"],
                        body=request["body"],
                        task_ref=request.get("task_ref"),
                    )
                    result = {"status": "delivered", "message_id": mid}
                elif request.get("op") == "ack":
                    ids = request["ids"]
                    if not isinstance(ids, list) or not all(type(i) is int for i in ids):
                        raise ValueError("invalid acknowledgement IDs")
                    acked = await store.ack_messages(session_id, recipient, ids)
                    result = {"status": "acknowledged", "ids": acked}
                else:
                    raise ValueError("unknown mailbox operation")
            except MailboxIntegrityError as exc:
                mailbox.write_receipt(name, {"status": "rejected", "error": str(exc), "special_file": True})
                continue
            except (ValueError, KeyError, TypeError, AttributeError, SwarmStoreError) as exc:
                result = {"status": "rejected", "error": str(exc)}
            mailbox.write_receipt(name, result)
            mailbox.remove_request(name)
        messages = await store.inbox(session_id, recipient, include_acked=True)
        mailbox.write_state("inbox.json", {"session_id": session_id, "recipient": recipient, "messages": messages})
        await store.mark_delivered([m["id"] for m in messages], session_id=session_id, recipient=recipient)


async def sync_collaboration_mailbox(
    service: Any,
    root: Path,
    *,
    principal: Any,
    product_id: str,
    recipient_project: str,
    recipient_role: str,
    feature_id: str,
    task_id: str,
) -> None:
    """Sync a separate data-only inbox using exclusively host-provided identity."""
    from whilly.swarm.learning.messages import MessageEnvelope

    scope = {"product_id": product_id, "recipient_project": recipient_project, "recipient_role": recipient_role}
    with MailboxDirectory(root) as mailbox:
        if service is None or service.policy is None:
            mailbox.write_state("inbox.json", {**scope, "messages": [], "blocker": "message_delivery_policy_required"})
            return
        send_keys = {
            "op",
            "recipient_project",
            "recipient_role",
            "kind",
            "correlation_id",
            "causation_id",
            "idempotency_key",
            "expires_at",
            "payload",
            "evidence_refs",
        }
        for name in mailbox.request_names(100):
            if mailbox.has_receipt(name):
                if not mailbox.receipt_is_special_rejection(name):
                    mailbox.remove_request(name)
                continue
            try:
                request = json.loads(mailbox.read_request(name))
                if not isinstance(request, dict):
                    raise ValueError("invalid request")
                if request.get("op") == "send" and not (request.keys() - send_keys):
                    envelope = MessageEnvelope(
                        id=f"mail-{task_id}-{name[:-5]}",
                        product_id=product_id,
                        feature_id=feature_id,
                        task_id=task_id,
                        sender_id=principal.actor_id,
                        recipient_project=request["recipient_project"],
                        recipient_role=request["recipient_role"],
                        kind=request["kind"],
                        correlation_id=request["correlation_id"],
                        causation_id=request.get("causation_id"),
                        idempotency_key=request["idempotency_key"],
                        expires_at=datetime.fromisoformat(request["expires_at"]),
                        hop_count=0,
                        payload=request["payload"],
                        evidence_refs=tuple(request.get("evidence_refs", [])),
                    )
                    result = asdict(await service.send(principal, envelope))
                elif request.get("op") == "ack" and set(request) == {"op", "message_id"}:
                    if not isinstance(request["message_id"], str):
                        raise ValueError("invalid message id")
                    result = asdict(await service.ack(principal, request["message_id"], **scope))
                else:
                    raise ValueError("invalid operation or forged scope")
            except MailboxIntegrityError:
                mailbox.write_receipt(
                    name,
                    {"state": "rejected", "reason": "invalid_or_unauthorized_message", "special_file": True},
                )
                continue
            except (ValueError, PermissionError, KeyError, TypeError):
                result = {"state": "rejected", "reason": "invalid_or_unauthorized_message"}
            mailbox.write_receipt(name, result)
            mailbox.remove_request(name)
        messages = await service.deliver(principal, **scope)
        mailbox.write_state("inbox.json", {**scope, "messages": [item.as_dict() for item in messages], "blocker": None})


__all__ = [
    "MAILBOX_ENV",
    "MAX_BYTES",
    "MailboxIntegrityError",
    "enqueue",
    "read_inbox",
    "sync_mailbox",
    "sync_collaboration_mailbox",
]
