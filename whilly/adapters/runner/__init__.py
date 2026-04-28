"""Agent-runner adapters for Whilly v4.0 (PRD FR-1.6, TC-7).

This package owns the bridge between the orchestrator and an external coding
agent (Claude CLI today, others later). It is split deliberately:

* :mod:`whilly.adapters.runner.result_parser` — **pure** parsing of the
  agent's stdout into an :class:`AgentResult`. No subprocess, no asyncio, no
  filesystem; trivially unit-testable. Owned by TASK-017a.
* :mod:`whilly.adapters.runner.claude_cli` (TASK-017b) — the
  :func:`asyncio.create_subprocess_exec` wrapper that actually launches the
  ``claude`` binary, with retry/backoff, and delegates parsing to
  ``result_parser.parse_output``.

The parser sits in :mod:`whilly.adapters` (not :mod:`whilly.core`) because it
encodes knowledge of an external system (the Claude CLI's JSON envelope) — but
it stays subprocess-free so the I/O wrapper has a small, testable seam.
"""

from whilly.adapters.runner.result_parser import (
    COMPLETION_MARKER,
    AgentResult,
    AgentUsage,
    parse_output,
)

__all__ = ["AgentResult", "AgentUsage", "COMPLETION_MARKER", "parse_output"]
