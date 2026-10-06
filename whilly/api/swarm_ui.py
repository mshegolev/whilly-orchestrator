"""Browser UI for swarm conversation and execution.

GET /swarm displays an HTML interface for single conversation management.
POST /api/v1/swarm/* endpoints handle chat, plan selection, and execution.

Session auth (ADMIN ONLY via existing require_admin_role pattern).
Registry path is trusted server config only; client cannot override.
Long-running operations (chat, run) execute asynchronously with bounded in-process
jobs; conversation and task state is durable in PostgreSQL, while job handles are
in-memory. Exceptions persist as named errors in conversation history and UI.
Router lifespan shuts down active coordinators.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME

logger = logging.getLogger(__name__)

TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"
SWARM_TEMPLATE: Final[str] = "swarm.html.j2"

MAX_CHAT_TEXT: Final[int] = 16000
MAX_TITLE: Final[int] = 200
MAX_ACTIVE_JOBS: Final[int] = 8
MIN_WORKERS: Final[int] = 1
MAX_WORKERS: Final[int] = 8
SESSION_ID_PATTERN: Final[re.Pattern] = re.compile(r"^s[a-f0-9]{10}$")


@dataclass
class Job:
    """In-process job tracking for async operations."""

    job_type: str
    active: bool
    error: str | None = None
    coordinator: Any = None
    task: asyncio.Task[Any] | None = None


def _runtime() -> tuple[type, type]:
    """Lazy import of runtime to allow test seams.

    Returns (SwarmService factory, Coordinator class).
    """
    from whilly.swarm.runtime import SwarmService, Coordinator

    return SwarmService, Coordinator


def _validate_session_id(value: str) -> bool:
    """Validate session ID format."""
    return bool(SESSION_ID_PATTERN.match(value))


class CreateSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(default="", max_length=MAX_TITLE)


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(..., min_length=1, max_length=MAX_CHAT_TEXT)
    mode: str = "discuss"


class SessionFlagsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    archived: bool | None = Field(default=None, strict=True)
    is_test: bool | None = Field(default=None, strict=True)


class RunRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: int = Field(..., ge=1, strict=True)
    workers: int = Field(default=1, ge=MIN_WORKERS, le=MAX_WORKERS)


class ResumeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    workers: int = Field(default=1, ge=MIN_WORKERS, le=MAX_WORKERS)


class StopRequest(BaseModel):
    model_config = {"extra": "forbid"}
    # Hard kills are an operator recovery action, not a browser capability.
    kill: Literal[False] = False


def build_swarm_router(
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    registry_path: str,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> APIRouter:
    """Build swarm UI router with session auth and bounded job management.

    Args:
        pool: Database connection pool
        secret: Session cookie secret
        registry_path: Trusted path to registry file (server-side only)
        cookie_name: Session cookie name (default from auth_routes)
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    ServiceFactory, CoordinatorClass = _runtime()
    service = ServiceFactory(pool)
    admin_dep = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))

    # Job tracking: session_id -> Job
    jobs: dict[str, Job] = {}

    def reserve_job(session_id: str, job: Job) -> None:
        if jobs.get(session_id, Job("none", False)).active:
            raise HTTPException(status_code=409, detail="job already active")
        if sum(item.active for item in jobs.values()) >= MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=429, detail="job limit reached")
        jobs[session_id] = job

    async def shutdown() -> None:
        active = [job for job in jobs.values() if job.active]
        for job in active:
            if job.coordinator is not None:
                job.coordinator.request_stop("router shutdown")
        tasks = [job.task for job in active if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any):
        try:
            yield
        finally:
            await shutdown()

    router = APIRouter(tags=["swarm"], lifespan=lifespan)

    # ── Display ────────────────────────────────────────────────────────
    @router.get("/swarm", response_class=HTMLResponse, include_in_schema=False)
    async def swarm_ui(request: Request, principal: dict = admin_dep) -> HTMLResponse:
        """Render the swarm conversation UI."""
        return templates.TemplateResponse(
            request,
            SWARM_TEMPLATE,
            {
                "title": "Swarm Conversation",
                "user": principal.get("username", "unknown"),
            },
        )

    # ── API: Sessions ──────────────────────────────────────────────────
    @router.get("/api/v1/swarm/sessions")
    async def list_sessions(
        include_test: bool = False, include_archived: bool = False, principal: dict = admin_dep
    ) -> dict[str, Any]:
        """List visible sessions; filters explicitly reveal test/archive history."""
        sessions_list = await service.store.list_sessions(
            limit=100, include_test=include_test, include_archived=include_archived
        )
        return {"sessions": sessions_list}

    @router.patch("/api/v1/swarm/sessions/{session_id}/flags")
    async def session_flags(session_id: str, req: SessionFlagsRequest, principal: dict = admin_dep) -> dict[str, Any]:
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session id")
        if req.archived is None and req.is_test is None:
            raise HTTPException(status_code=422, detail="provide archived or is_test")
        row = await service.store.set_session_flags(session_id, archived=req.archived, is_test=req.is_test)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {"id": row["id"], "archived": row["archived"], "is_test": row["is_test"]}

    @router.post("/api/v1/swarm/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        req: CreateSessionRequest,
        principal: dict = admin_dep,
    ) -> dict[str, str]:
        """Create a new swarm session.

        Only trusts server-configured registry_path; rejects client attempts to override.
        """
        session_id = await service.create_session(registry_path, title=req.title)
        return {"id": session_id}

    @router.get("/api/v1/swarm/sessions/{session_id}")
    async def get_session(
        session_id: str,
        principal: dict = admin_dep,
    ) -> dict[str, Any]:
        """Get session status and job info."""
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid session_id")

        try:
            status_data = await service.status(session_id)
        except Exception as e:
            logger.warning(f"Session lookup failed: {e}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

        job = jobs.get(session_id, Job(job_type="none", active=False))
        history = await service.store.chat_history(session_id)
        revisions = await service.store.list_revisions(session_id)

        return {
            "status": status_data,
            "history": [
                {
                    "sender": msg["sender"],
                    "body": msg["body"],
                    "created_at": msg.get("created_at"),
                }
                for msg in history
            ],
            "job": {
                "type": job.job_type,
                "active": job.active,
                "error": job.error,
            },
            "revisions": revisions,
        }

    # ── API: Chat ──────────────────────────────────────────────────────
    @router.post("/api/v1/swarm/sessions/{session_id}/chat", status_code=status.HTTP_202_ACCEPTED)
    async def chat(
        session_id: str,
        req: ChatRequest,
        principal: dict = admin_dep,
    ) -> dict[str, str]:
        """Submit user message and optionally request plan generation.

        Modes: 'discuss' (default), 'plan'.
        Runs asynchronously; only one chat/run job per session.
        """
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid session_id")

        # Validate existence
        try:
            await service.require_session(session_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        # Validate mode
        if req.mode not in ("discuss", "plan"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid mode")

        # Reject duplicate jobs
        # Mark job active and run async
        reserve_job(session_id, Job(job_type="chat", active=True))

        async def run_chat() -> None:
            try:
                reply = await service.chat(session_id, req.text, request_plan=(req.mode == "plan"))
                if reply.error:
                    error_msg = f"chat_failed: {reply.error}"
                    await service.store.add_chat_message(session_id, "system", error_msg)
                    jobs[session_id].error = error_msg
            except Exception as e:
                error_msg = f"chat_failed: {type(e).__name__}"
                await service.store.add_chat_message(session_id, "system", error_msg)
                jobs[session_id].error = error_msg
                logger.exception(f"Chat failed for {session_id}")
            finally:
                jobs[session_id].active = False

        jobs[session_id].task = asyncio.create_task(run_chat())
        return {"job": "started"}

    # ── API: Run (apply revision) ──────────────────────────────────────
    @router.post("/api/v1/swarm/sessions/{session_id}/run", status_code=status.HTTP_202_ACCEPTED)
    async def run(
        session_id: str,
        req: RunRequest,
        principal: dict = admin_dep,
    ) -> dict[str, str]:
        """Apply a proposed revision and start execution via Coordinator.

        Only explicit user selection (revision + workers) triggers execution.
        """
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid session_id")

        try:
            await service.require_session(session_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        # Check revision exists and is proposed
        get_revision = getattr(service.store, "get_revision", None)
        if get_revision is not None:
            rev = await get_revision(session_id, req.revision)
        else:
            revisions = await service.store.list_revisions(session_id)
            rev = next((item for item in revisions if item.get("revision") == req.revision), None)
        if rev is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="revision not found")
        if rev["status"] != "proposed":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="revision not proposed")

        # Reject duplicate jobs
        # Create coordinator and run async
        coordinator = CoordinatorClass(service, session_id, max_parallel=req.workers)
        reserve_job(session_id, Job(job_type="run", active=True, coordinator=coordinator))

        async def run_coordinator() -> None:
            try:
                await service.apply_revision(session_id, req.revision)
                summary = await coordinator.run()
                await service.store.add_chat_message(
                    session_id,
                    "system",
                    f"run finished: {summary.status} ({len(summary.accepted)} accepted, {len(summary.failed)} failed)",
                )
            except Exception as e:
                error_msg = f"run_failed: {type(e).__name__}: {str(e)}"
                await service.store.add_chat_message(session_id, "system", error_msg)
                jobs[session_id].error = error_msg
                logger.exception(f"Run failed for {session_id}")
            finally:
                jobs[session_id].active = False

        jobs[session_id].task = asyncio.create_task(run_coordinator())
        return {"job": "started"}

    # ── API: Stop / Resume ─────────────────────────────────────────────
    @router.post("/api/v1/swarm/sessions/{session_id}/stop", status_code=status.HTTP_200_OK)
    async def stop(
        session_id: str,
        req: StopRequest,
        principal: dict = admin_dep,
    ) -> dict[str, Any]:
        """Request graceful stop of active job (coordinator)."""
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid session_id")

        try:
            await service.require_session(session_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        job = jobs.get(session_id)
        if job and job.active:
            if job.coordinator:
                job.coordinator.request_stop("user stop request")
            if job.task is not None and not job.task.done():
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)

        result = await service.stop(session_id, kill=req.kill)
        return result or {"session": session_id, "stopped": True}

    @router.post("/api/v1/swarm/sessions/{session_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    async def resume(
        session_id: str,
        req: ResumeRequest,
        principal: dict = admin_dep,
    ) -> dict[str, str]:
        """Resume execution after a stop (if any tasks are pending)."""
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid session_id")

        try:
            session = await service.require_session(session_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        if session.get("applied_revision") is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no applied revision to resume")

        # Reject duplicate jobs
        job = jobs.get(session_id)
        if job and job.active:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job already active")

        # Create new coordinator for resume
        coordinator = CoordinatorClass(service, session_id, max_parallel=req.workers)
        reserve_job(session_id, Job(job_type="run", active=True, coordinator=coordinator))

        async def run_resume() -> None:
            try:
                summary = await coordinator.run()
                await service.store.add_chat_message(
                    session_id,
                    "system",
                    f"resume finished: {summary.status}",
                )
            except Exception as e:
                error_msg = f"resume_failed: {type(e).__name__}: {str(e)}"
                await service.store.add_chat_message(session_id, "system", error_msg)
                jobs[session_id].error = error_msg
                logger.exception(f"Resume failed for {session_id}")
            finally:
                jobs[session_id].active = False

        jobs[session_id].task = asyncio.create_task(run_resume())
        return {"job": "started"}

    # ── API: Report ────────────────────────────────────────────────────
    @router.get("/api/v1/swarm/sessions/{session_id}/report")
    async def get_report(
        session_id: str,
        principal: dict = admin_dep,
    ) -> dict[str, Any]:
        """Get execution report for applied revision."""
        if not _validate_session_id(session_id):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid session_id")

        try:
            report = await service.report(session_id)
        except Exception as e:
            logger.warning(f"Report lookup failed: {e}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

        return report

    return router
