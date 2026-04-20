"""Agent backends — pluggable LLM CLI wrappers (Claude, OpenCode, ...).

The package exposes a stable :class:`AgentBackend` Protocol plus concrete
implementations. Backwards compatibility with `whilly.agent_runner` is kept by
re-exporting the legacy names; existing import paths continue to work.

Selecting a backend:

    from whilly.agents import get_backend
    backend = get_backend("claude")          # default
    backend = get_backend("opencode")        # opt-in
    result  = backend.run("hello", model=backend.default_model())
"""

from __future__ import annotations

import os

from whilly.agents.base import AgentBackend, AgentResult, AgentUsage
from whilly.agents.claude import ClaudeBackend
from whilly.agents.opencode import OpenCodeBackend

__all__ = [
    "AgentBackend",
    "AgentResult",
    "AgentUsage",
    "ClaudeBackend",
    "OpenCodeBackend",
    "DEFAULT_BACKEND",
    "get_backend",
    "available_backends",
    "active_backend_from_env",
]

# Single source of truth for the built-in default. Mirror of
# ``whilly.config.WhillyConfig.AGENT_BACKEND``; callers that care about user
# config should prefer the config field, but the env-var-only path (tmux
# children, subprocess runners) uses this constant so there's exactly one
# place to change the name.
DEFAULT_BACKEND = "claude"


_REGISTRY: dict[str, type[AgentBackend]] = {
    "claude": ClaudeBackend,
    "opencode": OpenCodeBackend,
}


def available_backends() -> list[str]:
    """Return the list of backend names registered in this whilly build."""
    return sorted(_REGISTRY.keys())


def get_backend(name: str) -> AgentBackend:
    """Resolve a backend by name, returning a ready-to-use instance.

    Args:
        name: backend identifier (case-insensitive). Currently ``"claude"``
            or ``"opencode"``.

    Raises:
        ValueError: when *name* is unknown — message includes available
            backends to make the failure mode obvious.
    """
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown agent backend {name!r}. Available: {', '.join(available_backends())}")
    return _REGISTRY[key]()


def active_backend_from_env() -> AgentBackend:
    """Resolve the backend indicated by ``WHILLY_AGENT_BACKEND`` (or the
    built-in default when unset).

    Used by every runner that doesn't receive an explicit backend instance —
    :func:`whilly.agent_runner.run_agent`, :func:`whilly.tmux_runner.launch_agent`,
    and :func:`whilly.decision_gate._default_runner`. Having a single
    resolver means the env-var name and default value live in exactly one
    place in the codebase.
    """
    return get_backend(os.environ.get("WHILLY_AGENT_BACKEND", DEFAULT_BACKEND))
