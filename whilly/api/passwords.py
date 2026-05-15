"""Pure-stdlib password hashing for the operator login form.

PBKDF2-HMAC-SHA256, 200k iterations, 16-byte salt — chosen over scrypt /
argon2 specifically so the install closure stays "stdlib only" matching the
rest of whilly's auth surface (:mod:`whilly.api.auth_tokens`,
:mod:`whilly.api.dashboard_token`). No bcrypt / passlib dep.

Storage format on disk (``users.password_hash`` column): the salt-and-
digest are stored as separate columns (``password_salt`` + ``password_hash``)
rather than the multi-field ``$pbkdf2$...$`` envelope so SQL inspection is
easy. ``verify_password`` re-derives with the stored salt and constant-
time-compares.

Iteration count chosen to land around 100 ms on a 2026-era M-series laptop —
slow enough to make offline dictionary attacks expensive, fast enough that
interactive login stays sub-200 ms. Adjust via
``WHILLY_PASSWORD_PBKDF2_ITERATIONS`` env when running on slower hardware,
but never below 100 000 (OWASP 2024 floor).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Final

DEFAULT_ITERATIONS: Final[int] = 200_000
MIN_ITERATIONS: Final[int] = 100_000
SALT_BYTES: Final[int] = 16
HASH_BYTES: Final[int] = 32
_DIGEST: Final[str] = "sha256"


def _resolve_iterations() -> int:
    raw = (os.environ.get("WHILLY_PASSWORD_PBKDF2_ITERATIONS") or "").strip()
    if not raw:
        return DEFAULT_ITERATIONS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_ITERATIONS
    return max(parsed, MIN_ITERATIONS)


def hash_password(plain: str, *, salt: bytes | None = None) -> tuple[str, str]:
    """Return ``(salt_hex, hash_hex)`` for storage in the ``users`` table.

    ``salt`` is generated when omitted; passing it explicitly is the test
    seam so unit tests can assert on known output bytes.
    """
    if not isinstance(plain, str) or plain == "":
        raise ValueError("hash_password: plain must be a non-empty str")
    salt_bytes = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    if len(salt_bytes) < 8:
        raise ValueError("hash_password: salt must be >= 8 bytes")
    digest = hashlib.pbkdf2_hmac(
        _DIGEST,
        plain.encode("utf-8"),
        salt_bytes,
        _resolve_iterations(),
        dklen=HASH_BYTES,
    )
    return salt_bytes.hex(), digest.hex()


def verify_password(plain: str, *, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time check of ``plain`` against the stored salt+hash."""
    if not isinstance(plain, str) or not isinstance(salt_hex, str) or not isinstance(hash_hex, str):
        return False
    if not plain or not salt_hex or not hash_hex:
        return False
    try:
        salt_bytes = bytes.fromhex(salt_hex)
        stored_digest = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        _DIGEST,
        plain.encode("utf-8"),
        salt_bytes,
        _resolve_iterations(),
        dklen=len(stored_digest),
    )
    return hmac.compare_digest(candidate, stored_digest)


__all__ = [
    "DEFAULT_ITERATIONS",
    "HASH_BYTES",
    "MIN_ITERATIONS",
    "SALT_BYTES",
    "hash_password",
    "verify_password",
]
