"""Build explicit environments for future Whilly swarm child processes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
PROXY_KEYS = frozenset(("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"))
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


def proxy_environment(raw: Any) -> dict[str, str]:
    """Map an explicit ``execution.proxy`` block to child proxy variables.

    Only a loopback proxy (a local tunnel) is accepted, so the registry cannot
    route agents through an arbitrary remote host.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw or set(raw) - {"https", "http", "no_proxy"}:
        raise ValueError("execution.proxy must contain only https, http and no_proxy")
    environment: dict[str, str] = {}
    for scheme in ("https", "http"):
        url = raw.get(scheme)
        if url is None:
            continue
        if not isinstance(url, str):
            raise ValueError(f"execution.proxy.{scheme} must be a URL string")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.hostname not in _LOOPBACK_HOSTS or parts.username:
            raise ValueError(f"execution.proxy.{scheme} must be a loopback http(s) URL without credentials")
        environment[f"{scheme.upper()}_PROXY"] = environment[f"{scheme}_proxy"] = url
    if not environment:
        raise ValueError("execution.proxy requires https or http")
    no_proxy = raw.get("no_proxy", "localhost,127.0.0.1,::1")
    if not isinstance(no_proxy, str):
        raise ValueError("execution.proxy.no_proxy must be a string")
    environment["NO_PROXY"] = environment["no_proxy"] = no_proxy
    return environment


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
    if phase not in OFFLINE_PHASES:
        environment.update({name: base[name] for name in PROXY_KEYS if name in base})
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


__all__ = ["build_swarm_environment", "proxy_environment"]
