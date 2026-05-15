"""Production-mode umbrella: one env var that flips correct defaults and fails-loud on misconfig.

``WHILLY_PROD_MODE=true`` signals that the process is running in a real deployment
(not a developer laptop). In prod mode, three security-relevant defaults flip to their
hardened values and :func:`validate_prod_config` is expected to be called by
:func:`whilly.adapters.transport.server.create_app` before any routing is wired.
Any missing or clearly-weak config causes an immediate ``RuntimeError`` with an
actionable, single-line message so the operator sees the exact fix required.
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

_PROD_MODE_ENV: str = "WHILLY_PROD_MODE"
_SECRET_ENV: str = "WHILLY_DASHBOARD_TOKEN_SECRET"
_CSRF_ALLOWLIST_ENV: str = "WHILLY_CSRF_ORIGIN_ALLOWLIST"
_COOKIE_SECURE_ENV: str = "WHILLY_SESSION_COOKIE_SECURE"

_MIN_SECRET_BYTES: int = 32


def is_prod_mode() -> bool:
    """Return True when ``WHILLY_PROD_MODE`` is set to a truthy value.

    Accepted truthy strings (case-insensitive): ``true``, ``1``, ``yes``.
    Any other value, including an unset variable, returns False.
    """
    raw = (os.environ.get(_PROD_MODE_ENV) or "").strip().lower()
    return raw in {"true", "1", "yes"}


def _decode_secret_env() -> bytes | None:
    """Try to decode ``WHILLY_DASHBOARD_TOKEN_SECRET`` into raw bytes.

    Returns ``None`` when the env var is unset or empty.  Tries base64url /
    standard base64 first (operators may copy-paste a ``secrets.token_urlsafe``
    value), then plain hex, then raw UTF-8 as a last resort.  This mirrors the
    logic in :func:`whilly.api.dashboard_token.generate_dashboard_secret` so
    that a secret that *loads* at boot is the same one that *validates* here.
    """
    raw = os.environ.get(_SECRET_ENV, "")
    if not raw:
        return None
    raw = raw.strip()
    # Attempt base64url / standard base64 (most likely for random secrets).
    for alphabet_fix in (
        raw,
        raw.replace("-", "+").replace("_", "/"),
    ):
        padded = alphabet_fix + "=" * (-len(alphabet_fix) % 4)
        try:
            decoded = base64.b64decode(padded)
            if len(decoded) >= _MIN_SECRET_BYTES:
                return decoded
        except Exception:  # noqa: BLE001
            pass
    # Attempt hex.
    try:
        decoded_hex = bytes.fromhex(raw)
        if len(decoded_hex) >= _MIN_SECRET_BYTES:
            return decoded_hex
    except ValueError:
        pass
    # Raw UTF-8 fallback — only accepted when >= _MIN_SECRET_BYTES.
    utf8 = raw.encode("utf-8")
    if len(utf8) >= _MIN_SECRET_BYTES:
        return utf8
    return None


def validate_prod_config() -> None:
    """Raise ``RuntimeError`` with a human-actionable message on any prod misconfig.

    Must be called before routing is wired inside :func:`create_app` when
    :func:`is_prod_mode` returns True.  Three invariants are checked:

    1. ``WHILLY_DASHBOARD_TOKEN_SECRET`` must be present and decode to >= 32 bytes.
    2. ``WHILLY_CSRF_ORIGIN_ALLOWLIST`` must be non-empty (at least one entry).
    3. ``WHILLY_SESSION_COOKIE_SECURE`` must not be explicitly set to a falsy value
       (only ``true`` or *unset* are accepted in prod mode).
    """
    secret_bytes = _decode_secret_env()
    if secret_bytes is None or len(secret_bytes) < _MIN_SECRET_BYTES:
        raise RuntimeError(
            f"WHILLY_PROD_MODE is on but {_SECRET_ENV} is missing or decodes to "
            f"< {_MIN_SECRET_BYTES} bytes. "
            f'Fix: export {_SECRET_ENV}=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")'
        )

    csrf_raw = (os.environ.get(_CSRF_ALLOWLIST_ENV) or "").strip()
    csrf_entries = [s.strip() for s in csrf_raw.split(",") if s.strip()] if csrf_raw else []
    if not csrf_entries:
        raise RuntimeError(
            f"WHILLY_PROD_MODE is on but {_CSRF_ALLOWLIST_ENV} is empty. "
            f"Fix: export {_CSRF_ALLOWLIST_ENV}=https://your-dashboard-host.example.com"
        )

    cookie_secure_raw = (os.environ.get(_COOKIE_SECURE_ENV) or "").strip().lower()
    if cookie_secure_raw and cookie_secure_raw not in {"true", "1", "yes", "on"}:
        raise RuntimeError(
            f"WHILLY_PROD_MODE is on but {_COOKIE_SECURE_ENV} is set to {cookie_secure_raw!r}. "
            f"In prod mode the Secure cookie flag must be enabled. "
            f"Fix: unset {_COOKIE_SECURE_ENV} (it defaults to true in prod) or set it to 'true'."
        )


def cookie_secure_default() -> bool:
    """Return the correct ``Secure`` flag default for the session cookie.

    When prod mode is active the flag defaults to ``True``; in dev mode it
    defaults to ``False`` so loopback HTTP still works without TLS.  The actual
    ``WHILLY_SESSION_COOKIE_SECURE`` env var is read by the caller and overrides
    this default when explicitly set.
    """
    return is_prod_mode()


__all__ = [
    "cookie_secure_default",
    "is_prod_mode",
    "validate_prod_config",
]
