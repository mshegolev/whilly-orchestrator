"""Audit sinks for local orchestrator events.

This package contains parallel event sinks that mirror the canonical
``events`` table writes onto operator-facing surfaces (``whilly_logs/``).
The primary consumer today is the v4.3.1 backwards-compatibility
contract (VAL-CROSS-BACKCOMPAT-907): operators who used to ``tail -f
whilly_logs/whilly_events.jsonl`` in v3 / v4.3.1 must continue to see
the same line shape after the v4.4 distributed split.

Importantly: this is **NOT** the control-plane's
``events`` table writer. The HTTP control-plane mutates Postgres
directly (via :class:`whilly.adapters.db.repository.TaskRepository`
running inside its own process) and is the canonical, durable source
of truth. The JSONL sink here is a *parallel*, best-effort, file-based
mirror — failures to write to disk MUST never roll back a database
commit.
"""

from whilly.audit.jsonl_sink import (
    DEFAULT_JSONL_FILENAME,
    DEFAULT_LOG_DIR,
    LOG_DIR_ENV,
    JsonlEventSink,
    make_jsonl_sink_from_env,
)

__all__ = [
    "DEFAULT_JSONL_FILENAME",
    "DEFAULT_LOG_DIR",
    "JsonlEventSink",
    "LOG_DIR_ENV",
    "make_jsonl_sink_from_env",
]
