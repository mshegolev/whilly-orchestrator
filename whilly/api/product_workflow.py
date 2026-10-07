"""Admin workflow endpoints and bounded background jobs for the product cockpit."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME
from whilly.swarm.product_workflow import ProductWorkflow
from whilly.swarm.registry import RegistryError, load_registry

PLANNER_PROFILES = ("planner-strong", "planner-strong-claude", "planner-strong-codex", "planner-escalation")


def available_planners(registry_path: str) -> tuple[str, ...]:
    """Read trusted configuration without invoking providers or disclosing config errors."""
    try:
        registry = load_registry(registry_path)
    except (RegistryError, OSError, ValueError):
        return ()
    return tuple(name for name in PLANNER_PROFILES if name in registry.profiles)


class PlanRequest(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(default="", max_length=16000)
    planner_profile: Literal[
        "planner-strong", "planner-escalation", "planner-strong-claude", "planner-strong-codex"
    ] = "planner-strong"


class DiscussRequest(BaseModel):
    model_config = {"extra": "forbid"}
    body: str = Field(min_length=1, max_length=16000)
    planner_profile: Literal[
        "planner-strong", "planner-escalation", "planner-strong-claude", "planner-strong-codex"
    ] = "planner-strong"


class ExecuteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    workers: int = Field(default=1, ge=1, le=5, strict=True)


def build_product_workflow_router(pool, secret: bytes, registry_path: str) -> APIRouter:
    workflow = ProductWorkflow(pool, registry_path)
    admin = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=DEFAULT_SESSION_COOKIE_NAME))
    jobs: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        active = list(jobs.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

    router = APIRouter(lifespan=lifespan)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    @router.get("/swarm/product", response_class=HTMLResponse, name="product_swarm_ui")
    async def cockpit(request: Request, _principal: dict = admin):
        return templates.TemplateResponse(
            request=request,
            name="product_swarm.html.j2",
            context={"planner_profiles": PLANNER_PROFILES, "available_planners": available_planners(registry_path)},
        )

    def require_planner(name: str) -> None:
        if name not in available_planners(registry_path):
            raise HTTPException(409, "planner_setup_required")

    async def submit(key, operation, feature_id=None):
        if key in jobs or len(jobs) >= 5:
            raise HTTPException(409, "workflow_busy")

        async def run():
            # Session-level PG lock excludes other server instances, including planning races.
            async with pool.acquire() as conn:
                locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 77341))", key)
                if not locked:
                    await workflow.products.store.add_message("default", "workflow_busy: " + key, sender="system")
                    return
                try:
                    await operation()
                except BaseException as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                    if feature_id:
                        await workflow.products.invalidate(feature_id, error)
                    else:
                        await workflow.products.store.add_message("default", error, sender="system")
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 77341))", key)

        task = asyncio.create_task(run())
        jobs[key] = task
        task.add_done_callback(lambda _: jobs.pop(key, None))
        return {"status": "queued", "job": key}

    @router.post("/api/v1/swarm/products/default/discuss", status_code=202)
    async def discuss(req: DiscussRequest, _principal: dict = admin):
        require_planner(req.planner_profile)
        # Seed product before errors can be recorded.
        await workflow.products.store.ensure_product("Product swarm")
        return await submit("chief", lambda: workflow.discuss(req.body, req.planner_profile))

    @router.post("/api/v1/swarm/features/{feature_id}/plan", status_code=202)
    async def plan(feature_id: str, req: PlanRequest, _principal: dict = admin):
        require_planner(req.planner_profile)
        if await workflow.products.get_feature(feature_id) is None:
            raise HTTPException(404, "feature_not_found")
        return await submit(feature_id, lambda: workflow.plan(feature_id, req.text, req.planner_profile), feature_id)

    @router.post("/api/v1/swarm/features/{feature_id}/run", status_code=202)
    async def execute(feature_id: str, req: ExecuteRequest, _principal: dict = admin):
        feature = await workflow.products.get_feature(feature_id)
        if not feature:
            raise HTTPException(404, "feature_not_found")
        if feature["status"] != "approved" or not feature["approved_digest"]:
            raise HTTPException(409, "feature_approval_required")
        return await submit(feature_id, lambda: workflow.execute(feature_id, req.workers), feature_id)

    @router.post("/api/v1/swarm/features/{feature_id}/stop", status_code=202)
    async def stop(feature_id: str, _principal: dict = admin):
        from whilly.swarm.runtime import SwarmService

        feature = await workflow.products.get_feature(feature_id)
        if not feature:
            raise HTTPException(404, "feature_not_found")
        # Revoke first so a pending begin_run CAS cannot start after this point.
        await workflow.products.invalidate(feature_id, "stop_requested")
        await SwarmService(pool).stop(feature["session_id"])
        task = jobs.get(feature_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with pool.acquire() as conn:
            drained = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 77341))", feature_id)
            if drained:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 77341))", feature_id)
        # Across server instances cancellation is asynchronous. Never claim
        # that an in-flight remote push was undone or that all work is stopped.
        return {"status": "stop_requested", "drained": bool(drained), "feature": await workflow.require(feature_id)}

    @router.post("/api/v1/swarm/features/{feature_id}/publish", status_code=202)
    async def publish(feature_id: str, _principal: dict = admin):
        from whilly.swarm.product_publication import publish_feature

        feature = await workflow.products.get_feature(feature_id)
        if not feature or feature["status"] not in {"review", "mr_ready"}:
            raise HTTPException(409, "publication_requires_accepted_feature")
        return await submit(feature_id, lambda: publish_feature(workflow, feature_id), feature_id)

    @router.get("/api/v1/swarm/features/{feature_id}/publications")
    async def publications(feature_id: str, _principal: dict = admin):
        import json

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT receipt FROM swarm_publications WHERE feature_id=$1 ORDER BY project_id", feature_id
            )
        return {
            "publications": [
                json.loads(row["receipt"]) if isinstance(row["receipt"], str) else row["receipt"] for row in rows
            ]
        }

    return router
