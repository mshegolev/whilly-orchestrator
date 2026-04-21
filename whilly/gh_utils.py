"""Helpers for invoking the GitHub CLI (`gh`) from whilly.

Centralises how we build the subprocess environment so every caller
agrees on the auth source. Resolution order (first match wins):

1. ``WHILLY_GH_TOKEN`` — whilly-specific token. Placed into ``GITHUB_TOKEN``
   for the subprocess only, so users with a broken ambient ``GITHUB_TOKEN``
   can override it without touching the rest of their shell.
2. ``WHILLY_GH_PREFER_KEYRING=1`` — explicitly strip ``GITHUB_TOKEN`` /
   ``GH_TOKEN`` and force ``gh`` to use its keyring auth. Useful on macOS
   when the ambient ``GITHUB_TOKEN`` is stale but ``gh auth login`` was run.
3. ``[github].token`` in ``whilly.toml`` — optionally routed through
   :mod:`whilly.secrets` (e.g. ``"keyring:whilly/github"``) for
   cross-platform secret storage.
4. Otherwise, ``GITHUB_TOKEN`` / ``GH_TOKEN`` pass through unchanged —
   the cross-platform default that works on Linux, Windows, and CI.
"""

from __future__ import annotations

import os

_TRUTHY = ("1", "true", "yes", "on")


def gh_subprocess_env() -> dict[str, str]:
    """Return an ``os.environ`` copy prepared for a ``gh`` CLI subprocess.

    Consult the module docstring for the full resolution order.
    """
    env = dict(os.environ)

    whilly_token = (env.get("WHILLY_GH_TOKEN") or "").strip()
    if whilly_token:
        env["GITHUB_TOKEN"] = whilly_token
        env.pop("GH_TOKEN", None)
        return env

    prefer_keyring = (env.get("WHILLY_GH_PREFER_KEYRING") or "").strip().lower() in _TRUTHY
    if prefer_keyring:
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        return env

    toml_token = _resolve_toml_github_token()
    if toml_token:
        env["GITHUB_TOKEN"] = toml_token
        env.pop("GH_TOKEN", None)

    return env


def _resolve_toml_github_token() -> str:
    """Return a resolved ``github.token`` from ``whilly.toml`` (or empty string).

    Kept as a separate function so tests can monkeypatch it without stubbing
    the whole config module.
    """
    try:
        from whilly.config import get_toml_section
        from whilly.secrets import resolve as resolve_secret
    except ImportError:
        return ""
    section = get_toml_section("github")
    raw = section.get("token")
    if not raw:
        return ""
    resolved = resolve_secret(raw)
    return resolved if isinstance(resolved, str) else ""


__all__ = ["gh_subprocess_env"]
