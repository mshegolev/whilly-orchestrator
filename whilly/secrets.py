"""Secret reference resolver + per-worker credential storage (M1).

Two responsibilities:

1. **Read-side reference resolver** — config values in ``whilly.toml`` can
   opt into an OS-appropriate secret store without changing the file
   format. Recognised reference schemes:

   - ``env:NAME``             — read from ``os.environ[NAME]`` (empty string if missing).
   - ``keyring:service``      — read from the OS keyring (username defaults to ``"default"``).
   - ``keyring:service/user`` — same, with explicit keyring username.
   - ``file:/path/to/file``   — read and ``.strip()`` the file contents; ``~`` is expanded.

   Literal strings pass through unchanged. Non-string values pass through
   unchanged.

2. **Write-side credential storage** (M1, ``whilly worker connect``) —
   :func:`store_worker_credential` and :func:`load_worker_credential`
   persist a per-control-plane bearer token. The default backend is the
   OS keychain (``keyring>=24.0``: macOS Keychain / Linux Secret Service
   / Windows Credential Locker). On headless Linux (no D-Bus) or any
   other ``set_password`` failure we fall back to a chmod-600 JSON file
   under ``$XDG_CONFIG_HOME/whilly/credentials.json`` (default
   ``~/.config/whilly/credentials.json``) with parent dir mode 0700.
   File writes are atomic (``tmp + os.replace``) so a SIGINT mid-write
   never leaves a partial JSON document on disk.

Rationale: keeping the secret-reading helpers and the credential-storage
helpers in one module means the keyring monkey-patch surface is a single
import site (``whilly.secrets``), which is what the tests rely on for
forcing the file fallback.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("whilly")


_KEYRING_DEFAULT_USER = "default"

#: Service name under which per-control-plane bearer tokens are persisted
#: in the OS keychain. Matches the M1 contract (VAL-M1-CONNECT-001):
#: "keyring service='whilly', user=<control-url>". Exposed as a module
#: attribute (rather than hard-coded into call sites) so callers and
#: tests share a single source of truth.
WHILLY_KEYRING_SERVICE = "whilly"

#: Filename for the chmod-600 credential fallback file. Lives under
#: ``$XDG_CONFIG_HOME/whilly/`` (default ``~/.config/whilly/``). Chosen
#: name matches the operator-facing path mentioned throughout the M1
#: docs and the VAL-M1-CONNECT-013 assertion.
CREDENTIALS_FILENAME = "credentials.json"


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


# ---------------------------------------------------------------------------
# Per-worker credential storage (M1, ``whilly worker connect``)
# ---------------------------------------------------------------------------


def canonical_control_url(url: str) -> str:
    """Return ``url`` with a trailing slash stripped and no trailing path.

    Used as the keychain "username" key so two ``whilly worker connect``
    invocations against ``http://h:8000`` and ``http://h:8000/`` resolve
    to the same stored entry (VAL-M1-CONNECT-901). The contract treats
    URLs with explicit non-trivial path segments as a separate concern
    handled by :mod:`whilly.cli.worker` — this helper only canonicalises
    a *bare* host[:port] form, never strips a meaningful path.
    """
    return url.rstrip("/")


def credentials_dir() -> Path:
    """Return the directory under which the fallback credentials file lives.

    Honours ``$XDG_CONFIG_HOME`` (default ``~/.config``) per the
    XDG Base Directory spec. Does not create the directory; callers
    that intend to write must call :func:`store_worker_credential`,
    which handles ``mkdir(mode=0o700)``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "whilly"


def credentials_file_path() -> Path:
    """Return the absolute path of the fallback credentials JSON file."""
    return credentials_dir() / CREDENTIALS_FILENAME


def _set_keyring_password(service: str, username: str, password: str) -> None:
    """Thin wrapper around ``keyring.set_password`` — the test monkey-patch seam.

    Tests force the file-fallback branch by monkeypatching this function
    to raise. Centralising the import + call here keeps the patch site
    stable: ``monkeypatch.setattr("whilly.secrets._set_keyring_password",
    raising_stub)``.
    """
    import keyring  # local import — keeps cold-start cost off the hot path

    keyring.set_password(service, username, password)


def _get_keyring_password(service: str, username: str) -> str | None:
    """Thin wrapper around ``keyring.get_password`` — symmetric to setter."""
    import keyring

    return keyring.get_password(service, username)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path`` atomically, mode 0600.

    Strategy: serialise to a sibling ``NamedTemporaryFile`` in the same
    directory (so the rename is on the same filesystem and therefore
    atomic per POSIX), ``fchmod`` to 0600 *before* the rename so the
    file is never observable at a wider mode, then ``os.replace`` onto
    the target. A SIGINT between the two syscalls leaves the original
    file untouched (or absent if this was the first write).
    """
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Best-effort tighten the parent dir even if it pre-existed at a
    # wider mode (e.g. the operator created ~/.config manually). We do
    # not chmod ancestor directories — only ours.
    try:
        os.chmod(parent, 0o700)
    except OSError:
        # Non-fatal: fall through to the file write. The file itself is
        # still 0600 which is the secrecy-bearing surface.
        pass

    fd, tmp_path = tempfile.mkstemp(prefix=".credentials.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any error / interrupt.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_credentials_file() -> dict[str, str]:
    """Return the existing fallback credentials map, or {} if absent / unreadable."""
    path = credentials_file_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        log.warning("credentials file unreadable at %s; treating as empty", path)
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("credentials file corrupt at %s; treating as empty", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def store_worker_credential(
    control_url: str,
    bearer: str,
    *,
    service: str = WHILLY_KEYRING_SERVICE,
) -> str:
    """Persist ``bearer`` for ``control_url`` and return the backend used.

    Order of operations:

    1. Try the OS keyring. On success, return ``"keyring"``.
    2. On *any* exception from the keyring (no D-Bus, locked keychain,
       Windows Credential Locker quota, ...) fall back to the chmod-600
       JSON file under ``credentials_file_path()`` and return ``"file"``.

    Returns the literal string identifying which backend was used so the
    caller can surface that information for ops triage. Never raises on
    expected failures — if the file fallback also fails (disk full,
    permission denied), the underlying :class:`OSError` propagates.

    The control-url key is canonicalised via :func:`canonical_control_url`
    (trailing slash stripped) so that ``http://h:8000`` and
    ``http://h:8000/`` map to the same entry (VAL-M1-CONNECT-901).
    """
    key = canonical_control_url(control_url)
    try:
        _set_keyring_password(service, key, bearer)
        return "keyring"
    except Exception as exc:
        log.info("keyring backend unavailable (%s); falling back to credentials file", type(exc).__name__)

    # File fallback: read existing map, update entry, atomic rewrite.
    path = credentials_file_path()
    payload = _read_credentials_file()
    payload[key] = bearer
    _atomic_write_json(path, payload)
    return "file"


def load_worker_credential(
    control_url: str,
    *,
    service: str = WHILLY_KEYRING_SERVICE,
) -> str | None:
    """Return the previously-stored bearer for ``control_url``, or ``None``.

    Mirrors :func:`store_worker_credential` precedence: keyring first,
    then the file fallback. A keyring backend that raises on
    ``get_password`` (broken keychain, no D-Bus) is treated as "absent"
    so the file fallback gets a chance.
    """
    key = canonical_control_url(control_url)
    try:
        secret = _get_keyring_password(service, key)
        if secret:
            return secret
    except Exception:
        # Fall through to the file fallback.
        pass
    return _read_credentials_file().get(key)


def normalise_control_url_for_storage(url: str) -> str:
    """Strip whitespace and trailing slash from ``url``; no scheme parsing.

    Convenience wrapper kept distinct from :func:`canonical_control_url`
    so call sites can express intent ("normalise for storage" vs "the
    canonical key form"). Identical behaviour today; split exists so a
    future revision can add e.g. lowercasing of scheme / host without
    touching every call site.
    """
    parts = urlsplit(url.strip())
    rebuilt = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, parts.fragment))
    return rebuilt or url.strip().rstrip("/")


__all__ = [
    "CREDENTIALS_FILENAME",
    "WHILLY_KEYRING_SERVICE",
    "canonical_control_url",
    "credentials_dir",
    "credentials_file_path",
    "load_worker_credential",
    "normalise_control_url_for_storage",
    "redact",
    "resolve",
    "store_worker_credential",
]
