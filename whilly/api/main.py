"""Public API surface for the lifespan event flusher (TASK-106).

This module is the thin entry-point validators reach for both the
:func:`create_app` factory and the :func:`_log_event` enqueue helper.
The actual FastAPI app factory lives in
:mod:`whilly.adapters.transport.server` (it is the *composition root*
of the HTTP API and predates this package); this module re-exports
``create_app`` so callers can write::

    from whilly.api.main import create_app, _log_event

without dragging in the transport adapter's import path. The
``_log_event`` helper:

* takes a :class:`fastapi.FastAPI` instance (so the same process can
  host multiple apps without sharing state);
* accepts a permissive set of kwargs that mirror the ``events`` table
  columns (``task_id``, ``plan_id``, ``payload``, ``detail``);
* is **synchronous** — it ``put_nowait``-s onto the lifespan-owned
  queue and returns immediately (VAL-OBS-002).

Why a free function and not a method on ``EventFlusher``?
    Some callers receive a ``FastAPI`` app from a context manager (e.g.
    pytest fixtures spinning the lifespan via ``app.router.lifespan_context``)
    and don't carry an :class:`EventFlusher` reference. A free function
    that reads ``app.state.event_flusher`` keeps the call site clean
    while preserving the strongly-typed enqueue path.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from whilly.adapters.transport.server import create_app
from whilly.api.event_flusher import EventFlusher, EventRecord

__all__ = [
    "_log_event",
    "create_app",
    "log_event",
]


def log_event(
    app: FastAPI,
    event_type: str,
    *,
    task_id: str | None = None,
    plan_id: str | None = None,
    payload: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Enqueue a single audit event onto the lifespan-owned flusher queue.

    Synchronous and non-blocking — performs no DB I/O on the caller's
    thread. The actual ``INSERT INTO events`` round-trip happens later
    on the flusher coroutine, batched with up to 499 sibling events
    into a single bulk statement.

    Args
    ----
    app:
        The FastAPI app whose lifespan owns the flusher. Must have
        been entered (``app.router.lifespan_context`` already invoked
        or ``TestClient`` already constructed); raises
        :class:`RuntimeError` if the flusher has not been wired up.
    event_type:
        The ``events.event_type`` column value — a stable
        machine-readable identifier ("task.skipped", "audit.note", etc.).
    task_id, plan_id:
        Optional FK columns. Validators rely on at least one being set
        for cross-flow tests; both ``None`` is allowed for purely
        process-level audit events that have no DB anchor.
    payload:
        Free-form JSONB blob serialised as ``events.payload``.
        Defaults to ``{}``.
    detail:
        Optional JSONB blob serialised as ``events.detail``. ``None``
        round-trips to SQL ``NULL`` (not the literal JSON string
        ``"null"``).

    Raises
    ------
    RuntimeError
        If the lifespan has not yet wired up the flusher (i.e.
        ``app.state.event_flusher`` is missing or ``None``). This is a
        programmer error: callers must ensure the lifespan is active
        before logging events.
    """
    flusher: EventFlusher | None = getattr(app.state, "event_flusher", None)
    if flusher is None:
        raise RuntimeError(
            "log_event called before lifespan started: app.state.event_flusher is None. "
            "Wrap your test in `async with app.router.lifespan_context(app):` or use "
            "FastAPI's TestClient to enter the lifespan first."
        )
    flusher.enqueue(
        EventRecord(
            event_type=event_type,
            task_id=task_id,
            plan_id=plan_id,
            payload=payload or {},
            detail=detail,
        )
    )


# Module-private alias — historical name used by the validation contract
# (``_log_event``) and by some callers. Keeps both spellings working
# without forcing every caller through the underscore convention.
_log_event = log_event
