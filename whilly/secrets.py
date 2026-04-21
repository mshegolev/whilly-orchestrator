"""Secret reference resolver.

Config values in `whilly.toml` can opt into an OS-appropriate secret store
without changing the file format. The resolver understands four reference
schemes; anything else is treated as a literal value:

- ``env:NAME``           — read from ``os.environ[NAME]`` (empty string if missing).
- ``keyring:service``    — read from the OS keyring (username defaults to ``"default"``).
- ``keyring:service/user`` — same, with explicit keyring username.
- ``file:/path/to/file`` — read and ``.strip()`` the file contents; ``~`` is expanded.

Literal strings pass through unchanged. Non-string values pass through unchanged.

Rationale: keeps behaviour config in plain ``whilly.toml`` while routing the
secret fetch through ``keyring`` on macOS / libsecret on Linux / Windows
Credential Manager on Windows. Never logs the resolved value.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("whilly")


_KEYRING_DEFAULT_USER = "default"


def resolve(value: Any) -> Any:
    """Resolve a single config value, following its prefix.

    Returns the original value unchanged when it isn't a string or doesn't
    match any known scheme. Never raises for a missing secret — returns an
    empty string instead, so callers can decide whether that's fatal.
    """
    if not isinstance(value, str):
        return value

    if value.startswith("env:"):
        return os.environ.get(value[len("env:") :], "")

    if value.startswith("keyring:"):
        return _resolve_keyring(value[len("keyring:") :])

    if value.startswith("file:"):
        return _resolve_file(value[len("file:") :])

    return value


def _resolve_keyring(ref: str) -> str:
    service, _, user = ref.partition("/")
    if not service:
        return ""
    try:
        import keyring
    except ImportError:
        log.warning("keyring not installed — cannot resolve keyring:%s", ref)
        return ""
    try:
        secret = keyring.get_password(service, user or _KEYRING_DEFAULT_USER)
    except Exception as exc:
        log.warning("keyring lookup failed for %s: %s", ref, exc)
        return ""
    return secret or ""


def _resolve_file(path_str: str) -> str:
    try:
        path = Path(path_str).expanduser()
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("file secret unreadable at %s: %s", path_str, exc)
        return ""


def redact(value: Any) -> str:
    """Stable placeholder for logging — never reveals the actual secret."""
    if not isinstance(value, str) or not value:
        return "<unset>"
    return f"<redacted: {len(value)} chars>"


__all__ = ["resolve", "redact"]
