"""Admin user-management UI (PRD-post-auth-hardening §Epic D, Item 10).

Two surfaces under ``/admin/``:

1. ``/admin/users`` — CRUD on the ``users`` table:
   * ``GET /admin/users``                          — list + create form
   * ``POST /admin/users/create``                  — create with must_change_password=TRUE
   * ``POST /admin/users/{username}/role``         — set role
   * ``POST /admin/users/{username}/reset-password`` — generate random pw + must_change=TRUE
   * ``POST /admin/users/{username}/delete``       — drop the row (idempotent → 404 on second)

2. ``/admin/auth-audit`` — paginated browse of the ``auth_audit`` ledger
   (50 rows/page; supports ``?username=X`` filter).

All routes are gated by :func:`require_admin_role` — a tiny FastAPI
dependency that calls :func:`whilly.api.auth_routes._authenticate_session`,
looks up the principal's user row, and returns 403 when the role is
anything other than ``"admin"``. PRD nominated putting this in
``sessions.py`` but that module is pure-asyncpg (no FastAPI imports);
keeping the guard here co-locates it with its only call sites.

Anti-privilege-escalation guard: the create and role-change paths refuse
to set ``role="admin"`` from anywhere except an existing admin (enforced
implicitly because every route is gated by :func:`require_admin_role`).
The delete path refuses to drop the currently-signed-in user — otherwise
an admin could brick the only admin account.
"""

from __future__ import annotations

import logging
from typing import Final

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from whilly.api import auth_audit_repo, users_repo
from whilly.api.auth_routes import (
    DEFAULT_SESSION_COOKIE_NAME,
    TEMPLATES_DIR,
    _authenticate_session,
)

logger = logging.getLogger(__name__)

ADMIN_USERS_TEMPLATE: Final[str] = "admin_users.html.j2"
ADMIN_AUTH_AUDIT_TEMPLATE: Final[str] = "admin_auth_audit.html.j2"

_VALID_ROLES: Final[tuple[str, ...]] = ("operator", "admin", "readonly")
_PAGE_SIZE: Final[int] = 50


def require_admin_role(*, pool: asyncpg.Pool, secret: bytes, cookie_name: str) -> object:
    """Build a FastAPI dependency that 403s any non-admin.

    Factory pattern matches :func:`whilly.api.auth_routes.build_auth_router`
    so the closure carries ``pool``/``secret``/``cookie_name`` once and the
    routes don't need to re-Depend on them. Returns the dependency callable,
    intended to be wrapped in :class:`fastapi.Depends` at the route level.
    """

    async def _dep(request: Request) -> dict[str, object]:
        try:
            principal = await _authenticate_session(request, pool=pool, secret=secret, cookie_name=cookie_name)
        except HTTPException as exc:
            # Re-raise — 401 from the auth helper is correct for missing session.
            raise exc
        session_email = str(principal.get("email", ""))
        username = session_email.removesuffix("@local") if session_email.endswith("@local") else session_email
        user = await users_repo.get_user_by_username(pool, username=username) if username else None
        if user is None or user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin role required",
            )
        principal["username"] = user.username
        principal["role"] = user.role
        return principal

    return _dep


def build_admin_users_router(
    *,
    pool: asyncpg.Pool,
    secret: bytes,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
) -> APIRouter:
    """Construct the admin router. See module docstring for the surface."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    router = APIRouter(tags=["admin"])
    admin_dep = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))

    @router.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
    async def list_users_view(request: Request, principal: dict = admin_dep) -> HTMLResponse:
        users = await users_repo.list_users(pool)
        return templates.TemplateResponse(
            request,
            ADMIN_USERS_TEMPLATE,
            {"users": users, "current_user": principal.get("username"), "form_error": None, "flash": None},
        )

    @router.post("/admin/users/create", response_class=HTMLResponse)
    async def create_user_view(
        request: Request,
        username: str = Form(..., min_length=1, max_length=64),
        email: str = Form(default="", max_length=320),
        role: str = Form(..., min_length=1, max_length=16),
        initial_password: str = Form(..., min_length=1, max_length=512),
        principal: dict = admin_dep,
    ) -> HTMLResponse:
        if role not in _VALID_ROLES:
            return await _render_with_error(request, templates, principal, f"invalid role {role!r}", status_code=400)
        try:
            await users_repo.create_user(
                pool,
                username=username,
                initial_password=initial_password,
                email=email or None,
                role=role,
            )
        except ValueError as exc:
            return await _render_with_error(request, templates, principal, str(exc), status_code=400)
        logger.info("admin.users.create: %r by %r", username, principal.get("username"))
        return await _render_users(request, templates, principal, flash=f"created user {username!r}")

    @router.post("/admin/users/{username}/role", response_class=HTMLResponse)
    async def set_role_view(
        request: Request,
        username: str,
        role: str = Form(..., min_length=1, max_length=16),
        principal: dict = admin_dep,
    ) -> HTMLResponse:
        if role not in _VALID_ROLES:
            return await _render_with_error(request, templates, principal, f"invalid role {role!r}", status_code=400)
        try:
            await users_repo.set_role(pool, username=username, role=role)
        except (LookupError, ValueError) as exc:
            return await _render_with_error(request, templates, principal, str(exc), status_code=400)
        logger.info("admin.users.set_role: %r→%r by %r", username, role, principal.get("username"))
        return await _render_users(request, templates, principal, flash=f"role of {username!r} → {role}")

    @router.post("/admin/users/{username}/reset-password", response_class=HTMLResponse)
    async def reset_password_view(request: Request, username: str, principal: dict = admin_dep) -> HTMLResponse:
        try:
            new_password = await users_repo.reset_password_to_random(pool, username=username)
        except LookupError as exc:
            return await _render_with_error(request, templates, principal, str(exc), status_code=404)
        logger.info(
            "admin.users.reset_password: %r by %r (must_change_password=TRUE)",
            username,
            principal.get("username"),
        )
        flash = (
            f"reset password for {username!r}. Communicate this value to the user — "
            f"it will not be shown again: {new_password}"
        )
        return await _render_users(request, templates, principal, flash=flash)

    @router.post("/admin/users/{username}/delete", response_class=HTMLResponse)
    async def delete_user_view(request: Request, username: str, principal: dict = admin_dep) -> HTMLResponse:
        # Self-deletion guard — never let an admin brick the only admin row.
        if username.strip().lower() == str(principal.get("username", "")).strip().lower():
            return await _render_with_error(
                request, templates, principal, "cannot delete the currently signed-in user", status_code=400
            )
        deleted = await users_repo.delete_user(pool, username=username)
        if not deleted:
            # Idempotent contract per AC: second delete returns 404 (rendered as
            # an HTMLResponse with a 404 status, not raised — the admin UI is a
            # browser surface, raising would dump a stack-trace page).
            return await _render_with_error(
                request, templates, principal, f"user {username!r} not found", status_code=404
            )
        logger.info("admin.users.delete: %r by %r", username, principal.get("username"))
        return await _render_users(request, templates, principal, flash=f"deleted user {username!r}")

    @router.get("/admin/auth-audit", response_class=HTMLResponse, include_in_schema=False)
    async def auth_audit_view(
        request: Request,
        page: int = Query(default=1, ge=1, le=10_000),
        username: str | None = Query(default=None, max_length=64),
        principal: dict = admin_dep,
    ) -> HTMLResponse:
        offset = (page - 1) * _PAGE_SIZE
        rows = await auth_audit_repo.list_attempts(pool, limit=_PAGE_SIZE, offset=offset, username_filter=username)
        return templates.TemplateResponse(
            request,
            ADMIN_AUTH_AUDIT_TEMPLATE,
            {
                "rows": rows,
                "page": page,
                "page_size": _PAGE_SIZE,
                "username_filter": username,
                "has_next": len(rows) == _PAGE_SIZE,
                "current_user": principal.get("username"),
            },
        )

    return router


async def _render_users(
    request: Request, templates: Jinja2Templates, principal: dict, *, flash: str | None
) -> HTMLResponse:
    users = await users_repo.list_users(_get_pool_from_principal_or_request(principal, request))
    return templates.TemplateResponse(
        request,
        ADMIN_USERS_TEMPLATE,
        {"users": users, "current_user": principal.get("username"), "form_error": None, "flash": flash},
    )


async def _render_with_error(
    request: Request,
    templates: Jinja2Templates,
    principal: dict,
    msg: str,
    *,
    status_code: int,
) -> HTMLResponse:
    users = await users_repo.list_users(_get_pool_from_principal_or_request(principal, request))
    return templates.TemplateResponse(
        request,
        ADMIN_USERS_TEMPLATE,
        {"users": users, "current_user": principal.get("username"), "form_error": msg, "flash": None},
        status_code=status_code,
    )


def _get_pool_from_principal_or_request(principal: dict, request: Request) -> asyncpg.Pool:
    """Recover the asyncpg pool from request.app.state or principal closure.

    The principal dict doesn't carry the pool (it shouldn't — that would leak
    a DB handle into route bodies), but the FastAPI app's state has it.
    Falling back to None if neither is wired up would be a bug — fail fast.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        # The principal closure carries the pool reference indirectly via the
        # require_admin_role factory — in production the app sets app.state.pool
        # in create_app. If a test omits this, we surface a clear error.
        raise RuntimeError(
            "admin_users_routes: request.app.state.pool is not set; "
            "create_app must call `app.state.pool = pool` before include_router."
        )
    return pool


# Re-exports — tests / wiring code grab these by name.
__all__ = [
    "ADMIN_AUTH_AUDIT_TEMPLATE",
    "ADMIN_USERS_TEMPLATE",
    "build_admin_users_router",
    "require_admin_role",
]
