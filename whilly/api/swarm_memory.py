"""Admin API for submitting and reading bounded swarm learning memory."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME
from whilly.adapters.db.learning_memory import PostgresMemoryStore
from whilly.swarm.learning.domain import KnowledgeRevision, Principal
from whilly.adapters.memory.config import build_memory_coordinator
from whilly.swarm.learning.memory import render_context
from whilly.swarm.product import ProductService
from whilly.swarm.registry import load_registry

_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SOURCE_RE = re.compile(r"^(?:git:[^/][^\x00]*|https://[^\s]+)$")
_KINDS = {"fact", "hypothesis", "observation", "decision"}


class KnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    kind: str
    body: str = Field(min_length=1, max_length=16000)
    source_uri: str
    source_sha: str | None = None
    evidence_hash: str = Field(min_length=1, max_length=256)
    expires_at: datetime | None = None
    ingestion_kind: str = "manual"


def _principal(raw: dict[str, object], project_ids: tuple[str, ...], product_id: str) -> Principal:
    actor = str(raw.get("username") or raw.get("email") or "")
    return Principal(actor, (product_id,), project_ids, ("internal",))


def _json_revision(revision: KnowledgeRevision) -> dict[str, Any]:
    result = dict(revision.__dict__)
    for key in ("observed_at", "verified_at", "expires_at"):
        if result[key] is not None:
            result[key] = result[key].isoformat()
    result["conflicts"] = list(result["conflicts"])
    return result


def build_memory_router(
    pool: asyncpg.Pool,
    secret: bytes,
    registry_path: str,
    *,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> APIRouter:
    """Build the admin-only memory API; server mounting remains controller-owned."""
    try:
        registry = load_registry(registry_path, check_git=False)
    except Exception as exc:
        raise RuntimeError("memory registry load failed") from exc
    projects = {project_id: (project.path, project.base_ref) for project_id, project in registry.projects.items()}
    store = PostgresMemoryStore(pool)
    service = build_memory_coordinator(pool, registry)
    products = ProductService(pool, registry_path)
    admin = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))

    @asynccontextmanager
    async def lifespan(app):
        await service.startup()
        yield

    router = APIRouter(prefix="/api/v1/swarm/knowledge", tags=["swarm-memory"], lifespan=lifespan)

    @router.get("")
    async def context(
        project_id: str = Query(...),
        query: str = Query("", max_length=4096),
        max_chars: int = Query(4096, ge=256, le=16000),
        principal: dict[str, object] = admin,
    ) -> dict[str, Any]:
        if project_id not in projects:
            raise HTTPException(status_code=400, detail="invalid project")
        product = await products.store.ensure_product(registry.raw.get("name", "default"))
        grants = tuple(projects)
        package = await service.context(
            _principal(principal, grants, str(product["id"])),
            str(product["id"]),
            (project_id,),
            query,
            max_chars=max_chars,
        )
        return json.loads(render_context(package))

    @router.get("/status")
    async def backend_status(principal: dict[str, object] = admin) -> dict[str, Any]:
        del principal
        return service.status().to_dict()

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def submit(request: KnowledgeRequest, principal: dict[str, object] = admin) -> dict[str, Any]:
        if request.project_id not in projects:
            raise HTTPException(status_code=400, detail="invalid project")
        if request.kind not in _KINDS or not _SOURCE_RE.fullmatch(request.source_uri):
            raise HTTPException(status_code=400, detail="invalid knowledge request")
        if request.source_sha is not None and not _SHA_RE.fullmatch(request.source_sha):
            raise HTTPException(status_code=400, detail="invalid source sha")
        if request.ingestion_kind == "session":
            raise HTTPException(status_code=409, detail="retention_policy_required")
        if request.ingestion_kind != "manual":
            raise HTTPException(status_code=400, detail="invalid ingestion kind")
        if request.expires_at is not None and request.expires_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="expires_at must be timezone-aware")
        if request.expires_at is not None and request.expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="expiry must be future")
        product = await products.store.ensure_product(registry.raw.get("name", "default"))
        revision = KnowledgeRevision(
            id=str(uuid4()),
            product_id=str(product["id"]),
            project_id=request.project_id,
            kind=request.kind,
            body=request.body,
            source_uri=request.source_uri,
            source_sha=request.source_sha,
            evidence_hash=request.evidence_hash,
            observed_at=datetime.now(timezone.utc),
            verified_at=None,
            expires_at=request.expires_at,
            classification="internal",
            status="candidate",
            author_id=str(principal.get("username") or principal.get("email") or ""),
            verifier_id=None,
            policy_version="l1.3",
        )
        try:
            return _json_revision(await store.append(revision))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid knowledge request") from exc

    @router.post("/{revision_id}/redact")
    async def redact(revision_id: str, principal: dict[str, object] = admin) -> dict[str, bool]:
        product = await products.store.ensure_product(registry.raw.get("name", "default"))
        result = await service.redact(_principal(principal, tuple(projects), str(product["id"])), revision_id)
        if not result.redacted:
            raise HTTPException(status_code=404, detail="not found")
        return {"redacted": True, "purge_pending": result.purge_pending}

    return router
