"""Async DB repository for the ``auth_audit`` table (migration 025).

The auth route layer (:mod:`whilly.api.auth_routes`) will call
:func:`insert_attempt` on every login outcome — successful or not — once
the call-site instrumentation lands in D10b. This module is migration-025-
only scope: it exposes the insert path and the outcome enum so the route
layer can be wired up in a follow-up PR without further schema changes.

Location: this file lives in ``whilly/api/`` to match the established
per-table-repo convention (see :mod:`whilly.api.users_repo` and
:mod:`whilly.api.sessions`). The PRD prose nominates
``whilly/adapters/db/auth_audit_repo.py``; the actual ``whilly/adapters/db/``
package is reserved for the big core repository module
(:mod:`whilly.adapters.db.repository`) and the connection pool, not for
table-specific async helpers. Keeping ``users_repo``, ``sessions`` and
``auth_audit_repo`` co-located in ``whilly/api/`` keeps the auth surface
discoverable in one directory.

Design contract:

* **Best-effort.** :func:`insert_attempt` MUST NOT raise — an audit insert
  failure must not block the login response or surface to the user. Any
  error is logged at WARNING and swallowed. This mirrors the
  ``update_last_login`` discipline in :mod:`whilly.api.users_repo`.
* **No FastAPI / Jinja imports.** Pure asyncpg, so the function is
  testable directly via a testcontainers Postgres without spinning up the
  FastAPI app.
* **No FK to ``users(username)``.** Bad-actor probes for non-existent
  accounts (``outcome='missing_user'``) must also be auditable; a FK would
  silently reject those rows.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final, Literal

import asyncpg

logger = logging.getLogger(__name__)

#: Allowed values for the ``outcome`` column — must stay in lock-step with
#: the ``ck_auth_audit_outcome_valid`` CHECK constraint in migration 025.
AUTH_AUDIT_OUTCOMES: Final[tuple[str, ...]] = (
    "ok",
    "bad_password",
    "locked",
    "rate_limited",
    "missing_user",
)

#: Typed alias so call sites get autocompletion + mypy enforcement on the
#: enum string. The runtime tuple above is what the DB CHECK constraint
#: actually validates against.
AuthAuditOutcome = Literal["ok", "bad_password", "locked", "rate_limited", "missing_user"]


async def insert_attempt(
    pool: asyncpg.Pool,
    *,
    username: str | None,
    ip: str | None,
    user_agent: str | None,
    outcome: AuthAuditOutcome,
    session_id: uuid.UUID | None = None,
) -> None:
    """Record a single login attempt in ``auth_audit``.

    Best-effort: any DB error is logged and swallowed so the login path
    can never fail because the audit write failed. The route layer treats
    this call as fire-and-forget once the validation step is past.

    ``outcome`` is validated against :data:`AUTH_AUDIT_OUTCOMES` before
    the round-trip to fail fast in dev (otherwise the only signal would
    be a Postgres CHECK violation, which the swallow-and-log policy would
    hide). An invalid outcome is logged at ERROR and the row is not
    inserted.

    ``session_id`` is only meaningful when ``outcome='ok'`` — it lets the
    admin browse join an audit row to the session row that resulted from
    the successful login. For every failure outcome it must be ``None``.
    """
    if outcome not in AUTH_AUDIT_OUTCOMES:
        logger.error(
            "auth_audit_repo: refusing to insert row with invalid outcome=%r (allowed=%s)",
            outcome,
            AUTH_AUDIT_OUTCOMES,
        )
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_audit (username, ip, user_agent, outcome, session_id)
                VALUES ($1, $2, $3, $4, $5)
                """,
                username,
                ip,
                user_agent,
                outcome,
                session_id,
            )
    except Exception:  # noqa: BLE001 — best-effort, must never fail the login path
        logger.warning(
            "auth_audit_repo: insert_attempt failed (outcome=%r username=%r) — swallowed",
            outcome,
            username,
            exc_info=True,
        )
        return


__all__ = [
    "AUTH_AUDIT_OUTCOMES",
    "AuthAuditOutcome",
    "insert_attempt",
]
