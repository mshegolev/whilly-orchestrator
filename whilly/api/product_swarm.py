"""Admin-only durable product swarm API."""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME
from whilly.swarm.product import ProductService


class MessageRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=16000)


class FeatureRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    intent: str = Field(..., min_length=1, max_length=16000)


class SpecRequest(BaseModel):
    model_config = {"extra": "forbid"}
    body: Any


class ApprovalRequest(BaseModel):
    revision: int = Field(..., ge=1)
    digest: str = Field(..., min_length=1, max_length=256)


class InvalidateRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=4000)


class BudgetRequest(BaseModel):
    model_config = {"extra": "forbid"}
    max_calls: int = Field(..., ge=1, le=1000, strict=True)
    max_elapsed_seconds: int = Field(..., ge=1, le=86400, strict=True)


def build_product_router(
    pool: asyncpg.Pool,
    secret: bytes,
    registry_path: str,
    *,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> APIRouter:
    service = ProductService(pool, registry_path)
    admin = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))
    router = APIRouter(prefix="/api/v1/swarm", tags=["swarm-products"])

    @router.get("/products")
    async def products(_principal: dict = admin) -> dict[str, Any]:
        return {"products": await service.store.list_products()}

    @router.get("/products/default/messages")
    async def messages(_principal: dict = admin) -> dict[str, Any]:
        return {"messages": await service.list_messages()}

    @router.post("/products/default/messages", status_code=status.HTTP_201_CREATED)
    async def add_message(req: MessageRequest, _principal: dict = admin) -> dict[str, Any]:
        return await service.add_message(req.body)

    @router.post("/products/default/features", status_code=status.HTTP_201_CREATED)
    async def create_feature(req: FeatureRequest, _principal: dict = admin) -> dict[str, Any]:
        return await service.create_feature(req.title, req.intent)

    @router.get("/features")
    async def features(_principal: dict = admin) -> dict[str, Any]:
        return {"features": await service.list_features()}

    @router.get("/features/{feature_id}")
    async def feature(feature_id: str, _principal: dict = admin) -> dict[str, Any]:
        result = await service.get_feature(feature_id)
        if result is None:
            raise HTTPException(status_code=404, detail="feature not found")
        return result

    @router.post("/features/{feature_id}/spec")
    async def prepare(feature_id: str, req: SpecRequest, _principal: dict = admin) -> dict[str, Any]:
        try:
            return await service.set_spec(feature_id, req.body)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="feature not found") from exc

    @router.post("/features/{feature_id}/approve")
    async def approve(feature_id: str, req: ApprovalRequest, _principal: dict = admin) -> dict[str, Any]:
        try:
            return await service.approve(feature_id, revision=req.revision, digest=req.digest)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/features/{feature_id}/budget")
    async def budget(feature_id: str, req: BudgetRequest, _principal: dict = admin) -> dict[str, Any]:
        try:
            return await service.set_budget(
                feature_id,
                {"max_calls": req.max_calls, "max_elapsed_seconds": req.max_elapsed_seconds},
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/features/{feature_id}/invalidate")
    async def invalidate(feature_id: str, req: InvalidateRequest, _principal: dict = admin) -> dict[str, Any]:
        try:
            return await service.invalidate(feature_id, req.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="feature not found") from exc

    return router
