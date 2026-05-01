"""JSONL audit sink — restores ``whilly_logs/whilly_events.jsonl``.

The v3 / v4.3.1 orchestrator appended one structured JSON line per
significant event to ``whilly_logs/whilly_events.jsonl`` so operators
could ``tail -f`` the loop's audit trail without booting a dashboard.
v4 moved the canonical audit log into Postgres (``events`` table); the
file-based mirror was unintentionally dropped.

This module restores it as a *parallel* sink: the database INSERT
remains the durable source of truth, and the JSONL append is a
best-effort, non-blocking mirror keyed off the same ``event_type`` /
``task_id`` / ``plan_id`` / ``payload`` shape. Failures to write to
disk are logged but never raised — the contract is "audit log to
Postgres always succeeds; JSONL mirror is opportunistic".

Line shape
----------
Every line is a single JSON object with the following keys::

    {
      "ts": "<ISO-8601 UTC timestamp>",
      "event": "<event_type>",          # legacy v3 / v4.3.1 alias
      "event_type": "<event_type>",     # canonical v4.4+ key
      "task_id": "<TaskId|null>",
      "plan_id": "<PlanId|null>",
      "payload": { ...event_type-specific shape... }
    }

The ``payload`` sub-object mirrors ``events.payload`` in Postgres
(same dict the repository writes via ``_INSERT_EVENT_SQL``), so a
JSON-Schema validator pointed at
``tests/fixtures/baselines/events_payload_v4.4.0.json`` succeeds on
``payload`` alone — the wrapper keys (``ts`` / ``event`` /
``event_type`` / ``task_id`` / ``plan_id``) are line-level metadata
that v4.3.1 readers also tolerate (their schemas keyed on ``ts`` +
``event``).

Why both ``event`` and ``event_type``?
    The v3 / v4.3.1 ``_log_event`` writer used ``event`` as the
    canonical key. v4 internally calls the same string ``event_type``
    (matching the Postgres column). Emitting both lets old tooling
    (``jq '.event'``) and new validators (``jq '.event_type'``) read
    the same line without translation.

Concurrency / atomicity
-----------------------
Each ``record`` call opens, appends one line, then closes the file —
the open/append/close cycle is short-lived so a SIGKILL between
events cannot leave a half-written line. POSIX guarantees ``write``
under ``O_APPEND`` is atomic for chunks ≤ ``PIPE_BUF`` (4096 bytes
on Linux/macOS), which covers every realistic event payload.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default log directory (matches the project default ``log_dir``
#: ``whilly_logs/`` documented in ``CLAUDE.md`` and used by the v3
#: ``_log_event`` writer).
DEFAULT_LOG_DIR: str = "whilly_logs"

#: Canonical filename inside :data:`DEFAULT_LOG_DIR`.
DEFAULT_JSONL_FILENAME: str = "whilly_events.jsonl"

#: Env var that overrides the log directory. Mirrors the v3 convention
#: documented in ``docs/workshop/PLAN-OPENCODE-DOCKER.md`` (per-worker
#: ``WHILLY_LOG_DIR=/logs/<worker_id>``).
LOG_DIR_ENV: str = "WHILLY_LOG_DIR"


class JsonlEventSink:
    """Append-only JSONL writer for orchestrator events (v4.3.1 backcompat).

    Construct once per orchestrator process and attach to a
    :class:`~whilly.adapters.db.repository.TaskRepository` via
    :meth:`~whilly.adapters.db.repository.TaskRepository.attach_jsonl_sink`.
    The repository then mirrors every successful ``INSERT INTO events``
    onto this sink immediately after the parent transaction commits.

    Side-effects on construction
    ----------------------------
    None — the file and parent directory are created lazily on the
    first :meth:`record` call. This keeps ``__init__`` cheap and
    side-effect-free for tests that build a sink but never write to
    it.
    """

    def __init__(self, log_dir: Path | str | None = None) -> None:
        """Bind the sink to a directory; the JSONL file lives at ``<dir>/whilly_events.jsonl``.

        Args:
            log_dir: Directory under which the JSONL file is appended.
                ``None`` resolves to the ``WHILLY_LOG_DIR`` env var or,
                failing that, :data:`DEFAULT_LOG_DIR` (``whilly_logs``).
                Relative paths resolve against the process CWD at
                ``record`` time, matching the v3 convention.
        """
        if log_dir is None:
            log_dir = os.environ.get(LOG_DIR_ENV, DEFAULT_LOG_DIR)
        self._log_dir = Path(log_dir)
        self._path = self._log_dir / DEFAULT_JSONL_FILENAME

    @property
    def log_dir(self) -> Path:
        """Resolved log directory (may not exist on disk yet)."""
        return self._log_dir

    @property
    def path(self) -> Path:
        """Absolute / relative path to the JSONL file."""
        return self._path

    def record(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        plan_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one JSON line to the sink's JSONL file.

        Idempotency / failure handling: each call is independent — a
        write failure (full disk, read-only filesystem, permissions)
        is logged at WARNING and swallowed so the caller's main flow
        (the database commit) is never affected.

        Args:
            event_type: Canonical event type literal — matches the
                ``events.event_type`` column written by the parent
                repository (e.g. ``"CLAIM"``, ``"COMPLETE"``,
                ``"task.created"``).
            task_id: Task primary key when the event is per-task;
                ``None`` for plan-level events.
            plan_id: Plan primary key.
            payload: ``events.payload`` jsonb shape — must already be
                JSON-serialisable (the repository constructs it
                explicitly with primitives).
        """
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "jsonl sink: mkdir(%s) failed: %s — skipping line for %s",
                self._log_dir,
                exc,
                event_type,
            )
            return
        line: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "event_type": event_type,
            "task_id": task_id,
            "plan_id": plan_id,
            "payload": payload if payload is not None else {},
        }
        try:
            # ``a`` mode + short-lived handle keeps the line append
            # atomic on POSIX for payloads ≤ PIPE_BUF (4096 bytes).
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning(
                "jsonl sink: write(%s) failed: %s — skipping line for %s",
                self._path,
                exc,
                event_type,
            )


def make_jsonl_sink_from_env() -> JsonlEventSink:
    """Construct a :class:`JsonlEventSink` bound to ``WHILLY_LOG_DIR`` / default.

    Convenience factory for CLI composition roots
    (:func:`whilly.cli.run._async_run`) that don't want to import
    ``os`` and the constant table just to build the sink.
    """
    return JsonlEventSink()
