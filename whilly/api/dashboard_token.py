"""Short-lived JWT-style bearer for the same-origin dashboard SSE channel.

The HTMX dashboard at ``GET /`` is rendered anonymously (no bearer
required). Its live-update channel — ``GET /events/stream`` — is
bearer-protected, which means the browser's native ``EventSource``
needs a credential to connect. This module mints a *narrow* signed
token that the dashboard template embeds in a ``<meta
name='whilly-events-token'>`` tag; the inline SSE wiring forwards it
on the connect URL so the browser-issued GET carries the bearer.

Wire shape (JWT-compatible HS256)
---------------------------------
``base64url(header).base64url(payload).base64url(hmac_sha256(secret, header + "." + payload))``

* ``header`` — ``{"alg":"HS256","typ":"JWT"}``
* ``payload`` — ``{"iss":"whilly","scope":[...],"iat":<int>,"exp":<int>}``
* signature — HMAC-SHA256 over the canonical encoded header + ``.`` +
  encoded payload, base64url-encoded without padding

Everything is base64url (RFC 4648 §5) without padding so the token
survives unescaped placement in URLs and HTML attributes.

Why not pull in ``PyJWT``?
-------------------------
The ``[server]`` extras already drag in starlette, fastapi, asyncpg,
sse-starlette, jinja2, prometheus-client + instrumentator. Adding a
hard dep purely so we can mint a 200-byte signed blob is unnecessary
when ``hmac`` + ``hashlib`` + ``base64`` + ``json`` from stdlib give
us the same spec-conformant output. Worker import-path purity (PRD
SC-6, ``.importlinter``) also stays trivially clean — this module
imports nothing beyond stdlib.

Why a per-process random secret?
--------------------------------
The token's only job is to authorise a same-origin browser SSE
connection that was just minted by the same control-plane process.
Tokens have ``exp ≤ 3600`` by contract; even if the JVM-equivalent of
"a JWT leaked" happens, the blast radius is bounded by the TTL. A
per-process random secret regenerated on restart invalidates every
outstanding token at restart time — that's the right cleanup signal.
We deliberately do NOT key off ``WHILLY_WORKER_TOKEN`` /
``WHILLY_WORKER_BOOTSTRAP_TOKEN`` so a leak of the dashboard secret
cannot mint cross-surface tokens, and rotation of the worker tokens
does not invalidate dashboard sessions.

Scope claim
-----------
The token carries an explicit ``scope`` claim listing the surfaces it
may unlock (``events.stream``, ``tasks.read``). Verification helpers
take an expected scope and reject tokens that do not include it. This
keeps a stolen events-stream token from being replayed against a
future hypothetical write surface that also accepts the meta token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Final

DEFAULT_TTL_SECONDS: Final[int] = 3600

MAX_TTL_SECONDS: Final[int] = 3600

DEFAULT_DASHBOARD_SCOPES: Final[tuple[str, ...]] = (
    "events.stream",
    "tasks.read",
)

EVENTS_STREAM_SCOPE: Final[str] = "events.stream"
TASKS_READ_SCOPE: Final[str] = "tasks.read"

_DEFAULT_HEADER: Final[dict[str, str]] = {"alg": "HS256", "typ": "JWT"}
_DEFAULT_HEADER_BYTES: Final[bytes] = json.dumps(_DEFAULT_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8")
_TOKEN_ISSUER: Final[str] = "whilly"


class DashboardTokenError(ValueError):
    """Base class for verification failures (invalid format / signature / claims)."""


class ExpiredDashboardTokenError(DashboardTokenError):
    """Raised when a syntactically-valid token's ``exp`` has elapsed."""


class InvalidDashboardTokenError(DashboardTokenError):
    """Raised when format / signature / required-claims checks fail."""


def generate_dashboard_secret() -> bytes:
    return secrets.token_bytes(32)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError) as exc:
        raise InvalidDashboardTokenError(f"base64url decode failed: {exc}") from None


def _sign(secret: bytes, signing_input: bytes) -> bytes:
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def mint_dashboard_token(
    secret: bytes,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    scope: tuple[str, ...] | list[str] = DEFAULT_DASHBOARD_SCOPES,
    now: float | None = None,
) -> str:
    """Mint a short-lived signed dashboard token."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) == 0:
        raise ValueError("mint_dashboard_token: secret must be non-empty bytes")
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(f"mint_dashboard_token: ttl_seconds must be in (0, {MAX_TTL_SECONDS}]; got {ttl_seconds!r}")
    scope_list = sorted({str(s) for s in scope})
    if not scope_list:
        raise ValueError("mint_dashboard_token: scope must list at least one surface")
    issued_at = int(now if now is not None else time.time())
    payload = {
        "iss": _TOKEN_ISSUER,
        "scope": scope_list,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    header_segment = _b64url_encode(_DEFAULT_HEADER_BYTES)
    payload_segment = _b64url_encode(payload_bytes)
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature_segment = _b64url_encode(_sign(secret, signing_input))
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def verify_dashboard_token(
    token: str,
    secret: bytes,
    *,
    expected_scope: str | None = EVENTS_STREAM_SCOPE,
    now: float | None = None,
    leeway_seconds: int = 0,
) -> dict[str, Any]:
    """Verify a dashboard token and return its decoded claims.

    Raises :class:`InvalidDashboardTokenError` for format / signature
    / required-claim failures; :class:`ExpiredDashboardTokenError` when
    the token's ``exp`` has elapsed (taking the optional
    ``leeway_seconds`` into account).
    """
    if not isinstance(token, str) or not token:
        raise InvalidDashboardTokenError("empty token")
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidDashboardTokenError("malformed token: expected 3 segments")
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, TypeError) as exc:
        raise InvalidDashboardTokenError(f"header decode failed: {exc}") from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidDashboardTokenError("unsupported alg; expected HS256")
    expected_signature = _sign(secret, f"{header_segment}.{payload_segment}".encode("ascii"))
    presented_signature = _b64url_decode(signature_segment)
    if not hmac.compare_digest(expected_signature, presented_signature):
        raise InvalidDashboardTokenError("signature mismatch")
    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, TypeError) as exc:
        raise InvalidDashboardTokenError(f"payload decode failed: {exc}") from None
    if not isinstance(payload, dict):
        raise InvalidDashboardTokenError("payload must be a JSON object")
    if payload.get("iss") != _TOKEN_ISSUER:
        raise InvalidDashboardTokenError("issuer claim mismatch")
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise InvalidDashboardTokenError("missing or invalid exp claim")
    iat = payload.get("iat")
    if not isinstance(iat, int):
        raise InvalidDashboardTokenError("missing or invalid iat claim")
    if exp - iat > MAX_TTL_SECONDS:
        raise InvalidDashboardTokenError(f"exp-iat exceeds max ttl {MAX_TTL_SECONDS}s")
    current = float(now) if now is not None else time.time()
    if current > float(exp) + max(0, int(leeway_seconds)):
        raise ExpiredDashboardTokenError("token expired")
    raw_scope = payload.get("scope")
    if not isinstance(raw_scope, list) or not all(isinstance(s, str) for s in raw_scope):
        raise InvalidDashboardTokenError("scope claim must be a list of strings")
    if expected_scope is not None and expected_scope not in raw_scope:
        raise InvalidDashboardTokenError(f"token missing required scope: {expected_scope}")
    return {
        "iss": payload["iss"],
        "iat": iat,
        "exp": exp,
        "scope": list(raw_scope),
    }


__all__ = [
    "DEFAULT_DASHBOARD_SCOPES",
    "DEFAULT_TTL_SECONDS",
    "EVENTS_STREAM_SCOPE",
    "MAX_TTL_SECONDS",
    "TASKS_READ_SCOPE",
    "DashboardTokenError",
    "ExpiredDashboardTokenError",
    "InvalidDashboardTokenError",
    "generate_dashboard_secret",
    "mint_dashboard_token",
    "verify_dashboard_token",
]
