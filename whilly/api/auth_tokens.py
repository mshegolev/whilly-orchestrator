"""Pure-stdlib mint/verify for WUI auth tokens (magic-link + session cookie value).

PRD-wui-multi-plan v2 §6.1 specifies that token cryptography lives in a
module with NO FastAPI / SQL / Jinja imports — only stdlib. Mirror the
shape of :mod:`whilly.api.dashboard_token`: HMAC-SHA256 sign-then-encode,
url-safe base64 segments joined with dots, ``iss=whilly`` claim, ``iat`` +
``exp`` claims, format ``header.payload.signature``.

Two surfaces:

* Magic-link tokens — short-lived (default 15 min) opaque single-use values
  encoded in the URL. The token returned to the operator is the *full*
  signed string; the database stores only the SHA-256 hash of the token
  (column ``magic_links.token_hash``). On verification we hash the
  presented token and look up the row.

* Session cookie values — longer-lived (default 30 days) signed envelopes
  carrying ``session_id`` + ``email`` claims. The actual session record
  lives in the ``sessions`` table; the cookie value is a self-describing
  pointer signed with the same HMAC secret. Verification:
  signature → expiry → DB lookup of ``session_id``.

Both share the same HMAC secret as :func:`whilly.api.dashboard_token.generate_dashboard_secret`
so a single ``WHILLY_DASHBOARD_TOKEN_SECRET`` env var (or in-process random
default) governs the whole auth surface — operators do not juggle keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Final

DEFAULT_MAGIC_LINK_TTL_SECONDS: Final[int] = 15 * 60
DEFAULT_SESSION_TTL_SECONDS: Final[int] = 30 * 24 * 60 * 60
MAX_TTL_SECONDS: Final[int] = 90 * 24 * 60 * 60

_DEFAULT_HEADER: Final[dict[str, str]] = {"alg": "HS256", "typ": "JWT"}
_DEFAULT_HEADER_BYTES: Final[bytes] = json.dumps(_DEFAULT_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8")
_TOKEN_ISSUER: Final[str] = "whilly"

MAGIC_LINK_TOKEN_TYPE: Final[str] = "ml"
SESSION_TOKEN_TYPE: Final[str] = "sess"


class AuthTokenError(ValueError):
    """Base class for auth-token verification failures."""


class ExpiredAuthTokenError(AuthTokenError):
    """Raised when ``exp`` has elapsed."""


class InvalidAuthTokenError(AuthTokenError):
    """Raised when format / signature / required-claims checks fail."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError) as exc:
        raise InvalidAuthTokenError(f"base64url decode failed: {exc}") from None


def _sign(secret: bytes, signing_input: bytes) -> bytes:
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw token string.

    The ``magic_links`` table stores this hash, never the raw token. Hash is
    the lookup key on ``/auth/magic`` verification.
    """
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("hash_token: token must be non-empty str")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def mint_magic_link_token(
    secret: bytes,
    *,
    email: str,
    ttl_seconds: int = DEFAULT_MAGIC_LINK_TTL_SECONDS,
    now: float | None = None,
) -> tuple[str, str]:
    """Mint a magic-link token and its DB hash.

    Returns a tuple ``(raw_token, token_hash)``. ``raw_token`` is the value
    embedded in the link sent to the operator; ``token_hash`` is what the
    application stores in ``magic_links.token_hash``.
    """
    if not isinstance(email, str) or not email.strip():
        raise ValueError("mint_magic_link_token: email must be non-empty str")
    _check_secret(secret)
    _check_ttl(ttl_seconds)
    issued_at = int(now if now is not None else time.time())
    payload = {
        "iss": _TOKEN_ISSUER,
        "typ": MAGIC_LINK_TOKEN_TYPE,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
        # 16 bytes of random material so two links minted in the same second
        # for the same email never collide (defence-in-depth — the partial
        # unique index in migration 018 prevents inserting a second
        # unconsumed row, but a reused token would be a separate failure).
        "jti": _b64url_encode(secrets.token_bytes(16)),
    }
    raw_token = _sign_envelope(secret, payload)
    return raw_token, hash_token(raw_token)


def verify_magic_link_token(
    token: str,
    secret: bytes,
    *,
    now: float | None = None,
    leeway_seconds: int = 0,
) -> dict[str, Any]:
    """Verify signature + expiry on a magic-link token.

    Returns decoded claims (``email``, ``iat``, ``exp``, ``jti``). Does NOT
    consult the database — the caller still needs to look up the token hash
    and ensure ``consumed_at IS NULL``. Raises :class:`InvalidAuthTokenError`
    on format/signature failures, :class:`ExpiredAuthTokenError` on expiry.
    """
    return _verify_envelope(token, secret, expected_typ=MAGIC_LINK_TOKEN_TYPE, now=now, leeway_seconds=leeway_seconds)


def mint_session_cookie_value(
    secret: bytes,
    *,
    session_id: str,
    email: str,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint the signed envelope used as the session cookie value."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("mint_session_cookie_value: session_id must be non-empty str")
    if not isinstance(email, str) or not email.strip():
        raise ValueError("mint_session_cookie_value: email must be non-empty str")
    _check_secret(secret)
    _check_ttl(ttl_seconds)
    issued_at = int(now if now is not None else time.time())
    payload = {
        "iss": _TOKEN_ISSUER,
        "typ": SESSION_TOKEN_TYPE,
        "sid": session_id,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
    }
    return _sign_envelope(secret, payload)


def verify_session_cookie_value(
    cookie_value: str,
    secret: bytes,
    *,
    now: float | None = None,
    leeway_seconds: int = 0,
) -> dict[str, Any]:
    """Verify signature + expiry on a session cookie value.

    Returns decoded claims (``sid``, ``email``, ``iat``, ``exp``). Caller is
    responsible for confirming the ``sessions`` row exists with matching
    ``email`` and ``revoked_at IS NULL``.
    """
    return _verify_envelope(
        cookie_value, secret, expected_typ=SESSION_TOKEN_TYPE, now=now, leeway_seconds=leeway_seconds
    )


def generate_session_id() -> str:
    """Generate a 32-byte url-safe random session identifier."""
    return _b64url_encode(secrets.token_bytes(32))


def _sign_envelope(secret: bytes, payload: dict[str, Any]) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    header_segment = _b64url_encode(_DEFAULT_HEADER_BYTES)
    payload_segment = _b64url_encode(payload_bytes)
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature_segment = _b64url_encode(_sign(secret, signing_input))
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def _verify_envelope(
    token: str,
    secret: bytes,
    *,
    expected_typ: str,
    now: float | None,
    leeway_seconds: int,
) -> dict[str, Any]:
    _check_secret(secret)
    if not isinstance(token, str) or not token:
        raise InvalidAuthTokenError("empty token")
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidAuthTokenError("malformed token: expected 3 segments")
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, TypeError) as exc:
        raise InvalidAuthTokenError(f"header decode failed: {exc}") from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidAuthTokenError("unsupported alg; expected HS256")
    expected_signature = _sign(secret, f"{header_segment}.{payload_segment}".encode("ascii"))
    presented_signature = _b64url_decode(signature_segment)
    if not hmac.compare_digest(expected_signature, presented_signature):
        raise InvalidAuthTokenError("signature mismatch")
    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, TypeError) as exc:
        raise InvalidAuthTokenError(f"payload decode failed: {exc}") from None
    if not isinstance(payload, dict):
        raise InvalidAuthTokenError("payload must be a JSON object")
    if payload.get("iss") != _TOKEN_ISSUER:
        raise InvalidAuthTokenError("issuer claim mismatch")
    if payload.get("typ") != expected_typ:
        raise InvalidAuthTokenError(f"token type mismatch: expected {expected_typ!r}")
    exp = payload.get("exp")
    iat = payload.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        raise InvalidAuthTokenError("missing or invalid iat/exp claim")
    if exp - iat > MAX_TTL_SECONDS:
        raise InvalidAuthTokenError(f"exp-iat exceeds max ttl {MAX_TTL_SECONDS}s")
    current = float(now) if now is not None else time.time()
    if current > float(exp) + max(0, int(leeway_seconds)):
        raise ExpiredAuthTokenError("token expired")
    return payload


def _check_secret(secret: bytes) -> None:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) == 0:
        raise ValueError("auth_tokens: secret must be non-empty bytes")


def _check_ttl(ttl_seconds: int) -> None:
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(f"auth_tokens: ttl_seconds must be in (0, {MAX_TTL_SECONDS}]; got {ttl_seconds!r}")


__all__ = [
    "AuthTokenError",
    "DEFAULT_MAGIC_LINK_TTL_SECONDS",
    "DEFAULT_SESSION_TTL_SECONDS",
    "ExpiredAuthTokenError",
    "InvalidAuthTokenError",
    "MAGIC_LINK_TOKEN_TYPE",
    "MAX_TTL_SECONDS",
    "SESSION_TOKEN_TYPE",
    "generate_session_id",
    "hash_token",
    "mint_magic_link_token",
    "mint_session_cookie_value",
    "verify_magic_link_token",
    "verify_session_cookie_value",
]
