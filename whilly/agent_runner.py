"""Compat shim — re-exports the new :mod:`whilly.agents` backend surface under
the legacy ``whilly.agent_runner`` import path.

Historically every runner (orchestrator, decision_gate, decomposer, cli) imported
``run_agent`` / ``AgentResult`` / ``is_api_error`` directly from here. After the
OpenCode backend work (OC-10x), the real implementation lives under
:mod:`whilly.agents` behind the :class:`~whilly.agents.base.AgentBackend` Protocol.

This module keeps all old names working — *same* dataclasses, *same* signatures —
and resolves the active backend once via :func:`whilly.agents.get_backend`,
respecting the ``WHILLY_AGENT_BACKEND`` env (defaults to ``claude``). Prefer
importing from ``whilly.agents`` in new code.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from whilly.agents import AgentBackend, active_backend_from_env
from whilly.agents.base import AgentResult, AgentUsage, COMPLETION_MARKER

log = logging.getLogger("whilly")


__all__ = [
    "AgentResult",
    "AgentUsage",
    "COMPLETION_MARKER",
    "API_ERRORS",
    "MAX_RETRIES",
    "BACKOFF",
    "run_agent",
    "run_agent_async",
    "collect_result",
    "collect_result_from_file",
    "is_api_error",
    "is_auth_error",
]


# Retained constants — referenced elsewhere in whilly for retry logic.
API_ERRORS = {403, 500, 529}
MAX_RETRIES = 3
BACKOFF = [5, 15, 30]


def _active_backend() -> AgentBackend:
    """Resolve the active backend once per call via the shared env helper."""
    return active_backend_from_env()


def run_agent(
    prompt: str,
    model: str = "claude-opus-4-6[1m]",
    timeout: int | None = None,
) -> AgentResult:
    """Legacy synchronous runner. Delegates to the active backend's ``.run()``."""
    return _active_backend().run(prompt, model=model, timeout=timeout)


def run_agent_async(
    prompt: str,
    model: str = "claude-opus-4-6[1m]",
    log_file: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.Popen:
    """Legacy async runner. Delegates to the active backend's ``.run_async()``."""
    return _active_backend().run_async(prompt, model=model, log_file=log_file, cwd=cwd)


def collect_result(
    proc: subprocess.Popen,
    log_file: Path | None = None,
    start_time: float = 0,
) -> AgentResult:
    """Collect result from a finished Popen — defers to the active backend."""
    return _active_backend().collect_result(proc, log_file=log_file, start_time=start_time)


def collect_result_from_file(log_file: Path, start_time: float = 0) -> AgentResult:
    """Parse AgentResult from a log file — defers to the active backend."""
    return _active_backend().collect_result_from_file(log_file, start_time=start_time)


def is_api_error(result: AgentResult) -> bool:
    """Check if *result* is a retriable API error."""
    text = (result.result_text or "").lower()
    return any(f"api error: {code}" in text for code in API_ERRORS) or (
        '"type":"error"' in text or "failed to authenticate" in text
    )


def is_auth_error(result: AgentResult) -> bool:
    """Check if *result* is a non-retriable auth error (403 forbidden)."""
    text = (result.result_text or "").lower()
    return "failed to authenticate" in text or ("403" in text and "forbidden" in text)
