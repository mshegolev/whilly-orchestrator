"""HTTPS-proxy helpers for Claude child-process invocations (TASK-109-1..2).

In corporate environments where Anthropic API is reachable only via an
SSH tunnel + HTTPS proxy (gpt-proxy:8888 mapped to 127.0.0.1:11112),
Whilly v4 needs to inject ``HTTPS_PROXY`` and ``NO_PROXY`` into the
*child-process* env when spawning Claude — but **not** into Whilly's
own process env, because that would route Postgres/asyncpg and
control-plane/httpx traffic through the same proxy.

This module owns: priority-chain resolution, env-diff building, and a
pre-flight TCP probe with a friendly error.

PRD: ``docs/PRD-v41-claude-proxy.md``.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

logger = logging.getLogger("whilly.proxy")

DEFAULT_NO_PROXY: str = "localhost,127.0.0.1,::1"

WHILLY_PROXY_URL_ENV: str = "WHILLY_CLAUDE_PROXY_URL"
WHILLY_NO_PROXY_ENV: str = "WHILLY_CLAUDE_NO_PROXY"
WHILLY_PROXY_PROBE_ENV: str = "WHILLY_CLAUDE_PROXY_PROBE"

INHERITED_HTTPS_PROXY_ENV: str = "HTTPS_PROXY"


@dataclass(frozen=True)
class ProxySettings:
    """Resolved proxy configuration for a single Whilly invocation.

    ``url=None`` means "no proxy" — env-injection is a no-op, no probe
    runs. Any non-None value triggers both the env-diff and the
    pre-flight probe before Claude is invoked.

    ``no_proxy`` is always a non-empty string by default; an operator
    can explicitly set ``WHILLY_CLAUDE_NO_PROXY=""`` to mean "exclude
    nothing" — that's a legitimate override, not a bug.
    """

    url: str | None
    no_proxy: str = DEFAULT_NO_PROXY

    @property
    def is_active(self) -> bool:
        """True iff a proxy URL is set."""
        return self.url is not None and self.url != ""


def resolve_proxy_settings(
    *,
    cli_url: str | None = None,
    cli_disabled: bool = False,
    env: Mapping[str, str] | None = None,
    default_no_proxy: str = DEFAULT_NO_PROXY,
) -> ProxySettings:
    """Pick proxy URL from the priority chain (PRD FR-1, FR-6, OQ-2).

    First match wins:

    1. ``cli_disabled=True`` → url=None (operator opt-out).
    2. ``cli_url`` non-empty → use that.
    3. ``env[WHILLY_CLAUDE_PROXY_URL]`` non-empty → use that.
    4. ``env[HTTPS_PROXY]`` non-empty → use that (inherited from shell).
    5. Otherwise → url=None.

    Args:
        cli_url: Value from ``--claude-proxy`` CLI flag, or None.
        cli_disabled: True iff ``--no-claude-proxy`` was passed.
        env: Mapping to read from (defaults to ``os.environ`` lazily
            so monkeypatched tests just work).
        default_no_proxy: Fallback when ``WHILLY_CLAUDE_NO_PROXY`` is
            not set.

    Returns:
        Frozen :class:`ProxySettings` with the resolved URL and
        ``no_proxy`` string.
    """
    if env is None:
        import os

        env = os.environ

    if cli_disabled:
        return ProxySettings(url=None, no_proxy=default_no_proxy)

    no_proxy = _resolve_no_proxy(env, default_no_proxy)

    if cli_url:
        return ProxySettings(url=cli_url, no_proxy=no_proxy)

    whilly_env_url = env.get(WHILLY_PROXY_URL_ENV, "").strip()
    if whilly_env_url:
        return ProxySettings(url=whilly_env_url, no_proxy=no_proxy)

    inherited = env.get(INHERITED_HTTPS_PROXY_ENV, "").strip()
    if inherited:
        return ProxySettings(url=inherited, no_proxy=no_proxy)

    return ProxySettings(url=None, no_proxy=no_proxy)


def _resolve_no_proxy(env: Mapping[str, str], default: str) -> str:
    """Read ``WHILLY_CLAUDE_NO_PROXY`` override, fall back to ``default``.

    Uses ``in env`` rather than ``env.get(KEY, default)`` so that an
    explicit empty-string override ("exclude nothing") wins over the
    default — operator's word is law.
    """
    if WHILLY_NO_PROXY_ENV in env:
        return env[WHILLY_NO_PROXY_ENV]
    return default


def build_subprocess_env(
    parent_env: Mapping[str, str],
    settings: ProxySettings,
) -> dict[str, str]:
    """Return env dict suitable for the ``env=`` kwarg of a child-process spawn.

    Two cases:

    * ``settings.is_active=False`` → return ``dict(parent_env)``
      unchanged. The proxy logic is a no-op.
    * ``settings.is_active=True`` → return ``parent_env`` plus
      ``HTTPS_PROXY`` + ``NO_PROXY`` keys from ``settings``. Existing
      values in ``parent_env`` are overridden so the operator's
      CLI/env override always wins.

    ``parent_env`` is read-only — we always allocate a fresh dict so
    callers passing ``os.environ`` are not mutated. This matters
    because Whilly worker loops reuse the same parent_env across many
    spawns; mutating it would leak the proxy into the parent process.
    """
    result = dict(parent_env)
    # Bind to a local so the truthy check narrows for the type checker
    # without an `assert` — assertions are stripped under ``python -O``,
    # so a real branch is the safer guard.
    url = settings.url
    if url:
        result["HTTPS_PROXY"] = url
        result["NO_PROXY"] = settings.no_proxy
    return result


def spawn_env_for_claude(parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve proxy settings from ``parent_env`` and build the spawn-env in one step.

    Single entry-point used by both ``claude_cli._spawn_and_collect`` and
    ``prd_generator._call_claude``. CLI overrides from ``whilly init`` are
    already materialised into env vars by ``run_init_command`` before any
    spawn happens, so the env-only resolution path is correct here.
    """
    if parent_env is None:
        import os

        parent_env = os.environ
    settings = resolve_proxy_settings(env=parent_env)
    return build_subprocess_env(parent_env, settings)


def should_probe(env: Mapping[str, str] | None = None) -> bool:
    """Return ``True`` iff the TCP probe should run before invoking Claude.

    The probe is enabled by default; an operator opts out with
    ``WHILLY_CLAUDE_PROXY_PROBE=0`` (e.g. for proxies that legitimately
    reject bare TCP probes). Centralised here so any future relaxation of
    the opt-out grammar (``"false"``, ``"no"``, …) lands in one place.
    """
    if env is None:
        import os

        env = os.environ
    return env.get(WHILLY_PROXY_PROBE_ENV, "1") != "0"


def probe_proxy_or_raise(url: str, *, timeout: float = 0.5) -> None:
    """Open a TCP socket to the proxy ``host:port``; raise on failure (PRD FR-3).

    Cheap pre-flight: confirm the SSH tunnel is up before letting Claude
    time out deep inside its HTTPS client. Runs once on startup, not
    per-spawn.

    Args:
        url: Proxy URL like ``http://127.0.0.1:11112``. Scheme is
            ignored for the TCP check — only host+port matter. If
            port is missing, scheme-default is used (80 for http, 443
            for https; anything else is rejected).
        timeout: Socket connect timeout in seconds.

    Raises:
        RuntimeError: Connection refused, host unreachable, timeout,
            or unparseable URL. Message names the URL and includes
            the actionable ``ssh -fN -L ...`` hint.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        raise RuntimeError(
            f"whilly: cannot parse Claude proxy URL {url!r} "
            f"(expected http://host:port). To skip this check: {WHILLY_PROXY_PROBE_ENV}=0"
        )

    port = parsed.port
    if port is None:
        if parsed.scheme == "http":
            port = 80
        elif parsed.scheme == "https":
            port = 443
        else:
            raise RuntimeError(
                f"whilly: Claude proxy URL {url!r} has no port and unknown scheme "
                f"{parsed.scheme!r}; specify host:port explicitly."
            )

    try:
        with socket.create_connection((host, port), timeout=timeout):
            logger.info("whilly: Claude proxy probe ok (%s:%d)", host, port)
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"whilly: Claude proxy unreachable at {url} ({exc.__class__.__name__}: {exc})\n"
            f"Hint: bring up the SSH tunnel first, e.g.:\n"
            f"  ssh -fN -L {port}:127.0.0.1:8888 gpt-proxy\n"
            f"To skip this check: {WHILLY_PROXY_PROBE_ENV}=0"
        ) from exc


__all__ = [
    "DEFAULT_NO_PROXY",
    "INHERITED_HTTPS_PROXY_ENV",
    "ProxySettings",
    "WHILLY_NO_PROXY_ENV",
    "WHILLY_PROXY_PROBE_ENV",
    "WHILLY_PROXY_URL_ENV",
    "build_subprocess_env",
    "probe_proxy_or_raise",
    "resolve_proxy_settings",
    "should_probe",
    "spawn_env_for_claude",
]
