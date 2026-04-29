"""Whilly v4 API surface (TASK-106).

This package owns the *control-plane* HTTP surface and its supporting
background machinery. The actual FastAPI app factory lives in
:mod:`whilly.adapters.transport.server`; this package adds the
lifespan-managed event flusher (TASK-106) that batches audit events
into bulk Postgres inserts.

What lives here
---------------
* :mod:`whilly.api.event_flusher` — :class:`EventFlusher` plus the
  :class:`EventRecord` value type. Used by :func:`create_app` to spawn
  a TaskGroup-managed coroutine that drains an :class:`asyncio.Queue`
  into the ``events`` table on a 100 ms / 500-row trigger.
* :mod:`whilly.api.main` — the public ``_log_event`` entry-point and a
  thin re-export of :func:`create_app`. Validators (and downstream
  callers) reach the flusher through this module.

Why a separate package and not a sub-package of ``adapters/transport``?
    The HTTP adapter is one of several call sites for the flusher
    (CLI background workers, future SSE streamers, etc.). Keeping the
    flusher module out of ``adapters/transport`` lets non-HTTP callers
    enqueue events without depending on FastAPI.

Why expose ``_log_event`` here and not on ``app.state``?
    Some callers do not have an ``app`` reference handy (e.g. CLI
    helpers running inside the same process). The function takes the
    ``FastAPI`` instance explicitly and reaches into
    ``app.state.event_queue`` — this keeps the contract testable while
    avoiding global module state.
"""

# NOTE: This package's submodules (``event_flusher``, ``main``) are
# imported on demand. We deliberately avoid eager re-exports here to
# keep ``whilly.api`` cycle-free: :mod:`whilly.adapters.transport.server`
# imports :class:`EventFlusher` from :mod:`whilly.api.event_flusher`,
# while :mod:`whilly.api.main` re-exports :func:`create_app` from the
# same transport module — eager re-exports here would close the loop
# during package initialisation. Callers should import from the
# submodule directly:
#
#     from whilly.api.event_flusher import EventFlusher, EventRecord
#     from whilly.api.main import _log_event, create_app
__all__: list[str] = []
