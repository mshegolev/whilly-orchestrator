"""Admin-only bounded API for inspecting and manually sending swarm messages."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from whilly.adapters.db.learning_delivery import (
    build_delivery_service,
    delivery_policy_from_env,
    history_for_projects,
)
from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME
from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.messages import MessageEnvelope
from whilly.swarm.product_store import ProductStore
from whilly.swarm.registry import load_registry


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_project: str = Field(min_length=1, max_length=128)
    recipient_role: str = Field(min_length=1, max_length=128)
    kind: Literal["question", "answer", "finding", "contract_change", "task_proposal", "receipt"]
    payload: dict[str, Any]
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=32
    )
    feature_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expires_at: datetime


def build_message_router(
    pool: asyncpg.Pool,
    secret: bytes,
    registry_path: str,
    *,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> APIRouter:
    """Build the API; controller mounts it and owns application lifecycle."""
    try:
        registry = load_registry(registry_path, check_git=False)
        delivery_policy_from_env()  # Validate configuration during composition; requests re-read it below.
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError("message registry load failed") from exc

    project_ids = tuple(registry.projects)
    sender_scopes = tuple(
        ("default", project_id, role_id)
        for role_id, role in registry.roles.items()
        for project_id in role.projects
        if project_id in registry.projects
    )
    admin = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))
    products = ProductStore(pool)
    router = APIRouter(prefix="/api/v1/swarm/collaboration", tags=["swarm-messages"])

    @router.get("/status")
    async def backend_status(principal: dict[str, object] = admin) -> dict[str, Any]:
        del principal
        try:
            configured_policy = delivery_policy_from_env()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="message delivery policy is invalid") from exc
        return {
            "configured": configured_policy is not None,
            "blocker": None if configured_policy is not None else "message_delivery_policy_required",
        }

    @router.get("")
    async def history(principal: dict[str, object] = admin) -> dict[str, Any]:
        del principal
        try:
            rows = await history_for_projects(pool, "default", project_ids, limit=100)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="message history unavailable") from exc
        now = datetime.now(timezone.utc)
        for row in rows:
            expiry = _parse_expiry(row.get("expires_at"))
            if expiry is not None and expiry <= now and row.get("state") in {"persisted", "delivered"}:
                row["state"] = "expired"
        return {"messages": rows}

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def send(request: MessageRequest, principal: dict[str, object] = admin) -> dict[str, Any]:
        try:
            current_policy = delivery_policy_from_env()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="message delivery policy is invalid") from exc
        if current_policy is None:
            raise HTTPException(status_code=409, detail="message_delivery_policy_required")
        if request.recipient_project not in registry.projects:
            raise HTTPException(status_code=400, detail="invalid message recipient")
        role = registry.roles.get(request.recipient_role)
        if role is None or request.recipient_project not in role.projects:
            raise HTTPException(status_code=400, detail="invalid message recipient")
        if request.expires_at.tzinfo is None or request.expires_at.utcoffset() is None:
            raise HTTPException(status_code=422, detail="expires_at must be timezone-aware")
        if request.expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="expiry must be future")
        actor_id = str(principal.get("username") or principal.get("email") or "")
        if not actor_id:
            raise HTTPException(status_code=403, detail="admin identity unavailable")
        if ("default", request.recipient_project, request.recipient_role) not in sender_scopes:
            raise HTTPException(status_code=403, detail="message delivery is not authorized")
        try:
            payload_bytes = len(json.dumps(request.payload, separators=(",", ":")).encode())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid message payload") from exc
        if payload_bytes > current_policy.max_payload_bytes:
            raise HTTPException(status_code=400, detail="invalid message request")
        # Host grants are bound to the actual authenticated username, never client data.
        admin_principal = Principal(actor_id, ("default",), project_ids, ("internal",))
        service = build_delivery_service(
            pool,
            registry,
            actor_id=actor_id,
            sender_scopes=sender_scopes,
            recipient_scopes=(),
            policy=current_policy,
        )
        try:
            envelope = MessageEnvelope(
                id=str(uuid4()),
                product_id="default",
                feature_id=request.feature_id,
                task_id=request.task_id,
                sender_id=actor_id,
                recipient_project=request.recipient_project,
                recipient_role=request.recipient_role,
                kind=request.kind,
                correlation_id=request.correlation_id,
                causation_id=None,
                idempotency_key=request.idempotency_key,
                expires_at=request.expires_at,
                hop_count=0,
                payload=request.payload,
                evidence_refs=tuple(request.evidence_refs),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid message request") from exc
        # Product creation is deliberately after policy, auth and envelope validation.
        try:
            await products.ensure_product(registry.raw.get("name", "default"))
            receipt = await service.send(admin_principal, envelope)
        except PermissionError as exc:
            detail = "message delivery is not authorized"
            if "policy" in str(exc):
                detail = "message_delivery_policy_required"
            raise HTTPException(status_code=403, detail=detail) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid message request") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail="message persistence failed") from exc
        return receipt.__dict__

    return router


def _parse_expiry(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    return None


__all__ = ["MessageRequest", "build_message_router"]
