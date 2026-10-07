"""Build explicit environments for future Whilly swarm child processes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

SWARM_PHASES = (
    "discussion",
    "planner",
    "escalation",
    "worker",
    "review",
    "verify",
    "git",
    "host_script",
    "publication",
)
OFFLINE_PHASES = frozenset(("verify", "git", "host_script"))
BASE_KEYS = frozenset(("LANG", "LC_ALL", "LC_CTYPE", "TZ"))
IDENTITY_KEYS = frozenset(
    (
        "WHILLY_SWARM_SESSION",
        "WHILLY_SWARM_TASK",
        "WHILLY_SWARM_REVIEW",
        "WHILLY_SWARM_MAILBOX",
    )
)
PROVIDER_KEYS = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}


def build_swarm_environment(
    *,
    phase: str,
    base: Mapping[str, str],
    home: Path,
    temporary: Path,
    provider: str | None,
    provider_values: Mapping[str, str],
    identity: Mapping[str, str],
    path: str,
) -> dict[str, str]:
    """Return a child environment from explicit host and task-owned values."""
    if phase not in SWARM_PHASES:
        raise ValueError("unknown phase")
    if provider not in (None, *PROVIDER_KEYS):
        raise ValueError("unknown provider")

    unknown_identity = set(identity) - IDENTITY_KEYS
    if unknown_identity:
        raise ValueError("unknown identity key")

    selected_key = PROVIDER_KEYS.get(provider) if provider else None
    unknown_provider = set(provider_values) - ({selected_key} if selected_key else set())
    if unknown_provider:
        raise ValueError("unknown provider key")

    environment = {name: base[name] for name in BASE_KEYS if name in base}
    environment.update(
        {
            "PATH": path,
            "HOME": str(home),
            "TMPDIR": str(temporary),
        }
    )
    environment.update(identity)
    if phase not in OFFLINE_PHASES and selected_key in provider_values:
        environment[selected_key] = provider_values[selected_key]
    return environment


__all__ = ["build_swarm_environment"]
