"""Admin-only, read-bounded API for evaluating and admitting swarm proposals."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from whilly.adapters.db.learning_proposals import PostgresProposalStore
from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME
from whilly.swarm.learning.domain import Principal
from whilly.swarm.registry import load_registry

PRODUCT_ID = "default"
_SAFE_REASON_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:")
_SAFE_DECISION_CODES = frozenset(
    {
        "contract_consumer_unregistered",
        "contract_producer_unverified",
        "contract_verification_proof_type_missing",
        "dependency_cycle",
        "dependency_foreign",
        "dependency_unknown",
        "feature_approval_required",
        "feature_budget_exhausted",
        "feature_not_found",
        "feature_revision_mismatch",
        "feature_running",
        "proposal_already_decided",
        "proposal_not_found",
        "proposal_state_changed",
        "work_exists",
        "protected_module_path",
        "origin_feature_missing",
        "feature_budget_unknown",
        "feature_call_budget_exhausted",
        "feature_time_budget_exhausted",
        "stale_feature_revision",
        "existing_active_project_work",
        "proposal_state_not_admissible",
        "proposal_not_rejectable",
        "proposal_already_in_plan",
    }
)


class AcceptProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=0, strict=True)
    reason: str = Field(..., min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-blank")
        return value


class RejectProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be non-blank")
        return value


def build_proposal_service(pool: asyncpg.Pool, registry_path: str, *, actor_id: str, owner: bool):
    """Load the admission backend lazily so the API remains importable in unit tests."""
    from whilly.adapters.db.learning_proposal_admission import build_proposal_service as factory

    return factory(pool, registry_path, actor_id=actor_id, owner=owner)


def build_proposal_router(
    pool: asyncpg.Pool,
    secret: bytes,
    registry_path: str,
    *,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> APIRouter:
    """Construct the sidecar; controller/server mounting remains controller-owned."""
    admin = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))
    router = APIRouter(prefix="/api/v1/swarm/proposals", tags=["swarm-proposals"])
    store = PostgresProposalStore(pool, actor_host=socket.gethostname())

    async def context(raw_principal: dict[str, object]) -> tuple[Principal, tuple[str, ...]]:
        try:
            registry = load_registry(registry_path, check_git=False)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal registry unavailable") from exc
        project_ids = tuple(registry.projects)
        actor_id = str(raw_principal.get("username") or raw_principal.get("email") or "").strip()
        if not actor_id:
            raise HTTPException(status_code=403, detail="admin identity unavailable")
        return Principal(actor_id, (PRODUCT_ID,), project_ids, ("internal",)), project_ids

    async def feature_revision(feature_id: str) -> int | None:
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT revision FROM swarm_product_features WHERE product_id=$1 AND id=$2",
                PRODUCT_ID,
                feature_id,
            )
        return int(value) if value is not None else None

    def proposal_json(proposal: Mapping[str, Any]) -> dict[str, Any]:
        result = jsonable_encoder(dict(proposal))
        for field in ("evidence_refs", "acceptance", "dependencies"):
            value = result.get(field)
            if isinstance(value, str):
                try:
                    result[field] = json.loads(value)
                except (TypeError, ValueError):
                    result[field] = []
        return result

    def evaluation_json(result: Any) -> dict[str, Any]:
        return {"status": str(result.status), "reason": result.reason}

    def make_service(actor_id: str) -> Any:
        try:
            return build_proposal_service(pool, registry_path, actor_id=actor_id, owner=True)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal backend unavailable") from exc

    async def evaluate(service: Any, principal: Principal, proposal_id: str) -> dict[str, Any]:
        try:
            result = await service.evaluate(principal, proposal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="proposal evaluation forbidden") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal evaluation unavailable") from exc
        return evaluation_json(result)

    @router.get("")
    async def list_proposals(principal: dict[str, object] = admin) -> dict[str, Any]:
        auth_principal, project_ids = await context(principal)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT p.*, f.revision AS feature_revision
                       FROM swarm_learning_proposals p
                       JOIN swarm_product_features f ON f.id=p.origin_feature_id AND f.product_id=p.product_id
                      WHERE p.product_id=$1 AND p.target_project = ANY($2::text[])
                      ORDER BY p.created_at DESC, p.id DESC
                      LIMIT 100""",
                    PRODUCT_ID,
                    list(project_ids),
                )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal list unavailable") from exc
        actor_id = auth_principal.actor_id
        service = make_service(actor_id)
        result = []
        for row in rows:
            item = proposal_json(dict(row))
            item["evaluation"] = await evaluate(service, auth_principal, str(item["id"]))
            result.append(item)
        return {"proposals": result}

    @router.get("/{proposal_id}")
    async def get_proposal(proposal_id: str, principal: dict[str, object] = admin) -> dict[str, Any]:
        auth_principal, project_ids = await context(principal)
        try:
            proposal = await store.get(PRODUCT_ID, proposal_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal details unavailable") from exc
        if proposal is None or proposal.get("target_project") not in project_ids:
            raise HTTPException(status_code=404, detail="proposal not found")
        try:
            revision = await feature_revision(str(proposal["origin_feature_id"]))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal details unavailable") from exc
        if revision is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        service = make_service(auth_principal.actor_id)
        try:
            events = await store.events(PRODUCT_ID, proposal_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal details unavailable") from exc
        return {
            "proposal": proposal_json(proposal),
            "events": jsonable_encoder(events),
            "evaluation": await evaluate(service, auth_principal, proposal_id),
            "feature_revision": revision,
        }

    async def pread(proposal_id: str, raw_principal: dict[str, object]) -> tuple[Principal, Any]:
        auth_principal, project_ids = await context(raw_principal)
        try:
            proposal = await store.get(PRODUCT_ID, proposal_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal decision unavailable") from exc
        if proposal is None or proposal.get("target_project") not in project_ids:
            raise HTTPException(status_code=404, detail="proposal not found")
        return auth_principal, make_service(auth_principal.actor_id)

    @router.post("/{proposal_id}/accept")
    async def accept(proposal_id: str, request: AcceptProposalRequest, principal: dict[str, object] = admin) -> Any:
        auth_principal, service = await pread(proposal_id, principal)
        try:
            result = await service.accept_for_planning(
                auth_principal,
                proposal_id,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="proposal decision forbidden") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_safe_reason(str(exc), "proposal decision rejected")) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal decision unavailable") from exc
        return _decision_response(result)

    @router.post("/{proposal_id}/reject")
    async def reject(proposal_id: str, request: RejectProposalRequest, principal: dict[str, object] = admin) -> Any:
        auth_principal, service = await pread(proposal_id, principal)
        try:
            result = await service.reject(auth_principal, proposal_id, reason=request.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="proposal decision forbidden") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_safe_reason(str(exc), "proposal decision rejected")) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="proposal decision unavailable") from exc
        return _decision_response(result)

    return router


def _safe_reason(value: str, fallback: str) -> str:
    if value in _SAFE_DECISION_CODES and len(value) <= 128 and all(char in _SAFE_REASON_CHARS for char in value):
        return value
    return fallback


def _safe_blockers(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item in _SAFE_DECISION_CODES))[:32]


def _decision_response(result: Any) -> Any:
    payload = jsonable_encoder(result.__dict__)
    if payload.get("status") != "blocked":
        return payload
    return JSONResponse(
        status_code=409,
        content={
            "detail": _safe_reason(str(payload.get("reason") or ""), "proposal_blocked"),
            "blockers": _safe_blockers(payload.get("blockers")),
        },
    )


__all__ = [
    "AcceptProposalRequest",
    "RejectProposalRequest",
    "build_proposal_router",
    "build_proposal_service",
]
