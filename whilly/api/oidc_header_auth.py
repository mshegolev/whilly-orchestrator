"""Reverse-proxy header-trust authentication (PRD §Item 17 — E17).

⚠️  DANGER — this module trusts an identity header. Read the threat model in
``.planning/E15-E17-auth-security-design.md`` before changing it.

When ``WHILLY_TRUST_PROXY_AUTH=1`` a request whose **direct TCP peer** is inside
the ``WHILLY_TRUSTED_PROXY_IPS`` CIDR allowlist may carry an ``X-Forwarded-User``
header naming an existing user; that user is granted a *transient*,
non-persisted session for the duration of the request. When the flag is unset
or ``0`` (the default) the header is ignored entirely and this middleware is not
even mounted.

Security invariants — enforced here, verified by ``tests/unit/test_oidc_header_auth.py``:

* **Fail-closed.** Enabling the feature with an empty or unparseable allowlist
  raises at startup (``create_app`` time). An empty allowlist would trust the
  header from *any* peer — a full authentication bypass — so we refuse to boot.
* **Peer IP by default; allowlisted hops only.** With the default
  ``WHILLY_TRUSTED_PROXY_HOP_COUNT=1`` the allowlist is checked against
  ``request.client.host`` (the direct TCP peer) and ``X-Forwarded-For`` is
  ignored entirely. With ``N > 1`` the N proxies nearest Whilly (direct peer +
  XFF walked right-to-left) must *all* be in the allowlist — only allowlisted
  hops count, so a forged XFF can never widen trust (ADR-001 §P1.8).
* **Transient.** No row is written to ``sessions``; the identity lives only on
  ``request.state.proxy_principal`` and is honoured by
  :func:`whilly.api.auth_routes._authenticate_session` before the cookie path.
* **Audited.** A trusted-peer request carrying the header is recorded in
  ``auth_audit`` (``ok`` when the user exists, ``missing_user`` otherwise).

This feature does **not** implement OAuth/OIDC flows — it is header-trust only,
per the PRD non-goals. The proxy is responsible for stripping any
client-supplied ``X-Forwarded-User`` before forwarding; Whilly cannot enforce
that, so it is a documented precondition (see ``.env.example``).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from dataclasses import dataclass
from typing import Final

import asyncpg
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from whilly.api import auth_audit_repo, users_repo

log = logging.getLogger("whilly")

#: Master switch. Anything other than a truthy value disables the feature and
#: leaves the middleware unmounted.
TRUST_PROXY_AUTH_ENV: Final[str] = "WHILLY_TRUST_PROXY_AUTH"
#: Comma-separated CIDR / IP allowlist of *direct peers* permitted to assert
#: the identity header. Required (non-empty, parseable) when the feature is on.
TRUSTED_PROXY_IPS_ENV: Final[str] = "WHILLY_TRUSTED_PROXY_IPS"
#: Number of trusted proxy hops in front of Whilly. Default 1 (the direct peer
#: only — X-Forwarded-For ignored). When > 1, the N proxies closest to Whilly
#: (direct peer + X-Forwarded-For walked right-to-left) must ALL be in the
#: allowlist. See ADR-001 §P1.8.
TRUSTED_PROXY_HOP_COUNT_ENV: Final[str] = "WHILLY_TRUSTED_PROXY_HOP_COUNT"
#: Defensive upper bound on the hop count — a realistic chain is 1–3 hops.
_MAX_TRUSTED_HOPS: Final[int] = 16
#: The trusted identity header set by the reverse proxy.
FORWARDED_USER_HEADER: Final[str] = "X-Forwarded-User"
#: Standard hop-recording header. Only consulted when ``trusted_hops > 1``, and
#: even then only entries matching the allowlist count toward trust.
FORWARDED_FOR_HEADER: Final[str] = "X-Forwarded-For"

#: Nominal TTL stamped onto the transient principal. The session is not
#: persisted, so this only bounds how long downstream code treats the principal
#: as fresh within the request lifecycle.
_PROXY_SESSION_TTL_SECONDS: Final[int] = 300

_TruthyTokens: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TruthyTokens


def _resolve_hop_count(raw: str | None) -> int:
    """Parse the hop count, defaulting to 1 and failing closed on a bad value."""
    text = (raw or "").strip()
    if not text:
        return 1
    try:
        hops = int(text)
    except ValueError as exc:
        raise RuntimeError(
            f"{TRUSTED_PROXY_HOP_COUNT_ENV}={text!r} is not an integer. Refusing to start "
            f"rather than guessing how many proxy hops to trust."
        ) from exc
    if hops < 1 or hops > _MAX_TRUSTED_HOPS:
        raise RuntimeError(
            f"{TRUSTED_PROXY_HOP_COUNT_ENV}={hops} is out of range — must be 1..{_MAX_TRUSTED_HOPS}. "
            f"1 = trust the direct peer only; higher = that many trusted proxies in front of Whilly."
        )
    return hops


@dataclass(frozen=True)
class ProxyHeaderAuthConfig:
    """Resolved configuration for header-trust auth.

    Build via :meth:`from_env`, which performs the fail-closed validation. A
    disabled config (``enabled=False``) carries no networks and the middleware
    must not be mounted for it.
    """

    enabled: bool
    networks: tuple[_IPNetwork, ...] = ()
    trusted_hops: int = 1

    @classmethod
    def from_env(cls) -> ProxyHeaderAuthConfig:
        """Resolve config from the environment, failing closed on misconfig.

        Returns a disabled config when ``WHILLY_TRUST_PROXY_AUTH`` is not
        truthy. When it *is* truthy, ``WHILLY_TRUSTED_PROXY_IPS`` must be a
        non-empty, fully-parseable CIDR/IP list — otherwise this raises
        :class:`RuntimeError` so the app refuses to start rather than trusting
        the identity header from an unbounded set of peers.

        ``WHILLY_TRUSTED_PROXY_HOP_COUNT`` defaults to 1 (direct-peer trust). A
        value that is not an integer in ``[1, _MAX_TRUSTED_HOPS]`` raises (fail-
        closed — a broken hop count must not silently widen or disable trust).
        """
        if not _truthy(os.environ.get(TRUST_PROXY_AUTH_ENV)):
            return cls(enabled=False)

        raw = (os.environ.get(TRUSTED_PROXY_IPS_ENV) or "").strip()
        entries = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        if not entries:
            raise RuntimeError(
                f"{TRUST_PROXY_AUTH_ENV}=1 but {TRUSTED_PROXY_IPS_ENV} is empty. "
                f"Refusing to start: an empty allowlist would trust the "
                f"{FORWARDED_USER_HEADER} header from any peer — a full "
                f"authentication bypass. Set {TRUSTED_PROXY_IPS_ENV} to the "
                f"CIDR(s) of your reverse proxy (e.g. '10.0.0.0/24,127.0.0.1/32')."
            )

        networks: list[_IPNetwork] = []
        for entry in entries:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError as exc:
                raise RuntimeError(
                    f"{TRUSTED_PROXY_IPS_ENV} contains an invalid CIDR/IP {entry!r}: {exc}. "
                    f"Refusing to start rather than silently trusting a broken allowlist."
                ) from exc

        trusted_hops = _resolve_hop_count(os.environ.get(TRUSTED_PROXY_HOP_COUNT_ENV))
        return cls(enabled=True, networks=tuple(networks), trusted_hops=trusted_hops)

    def peer_is_trusted(self, peer_ip: str | None) -> bool:
        """True iff ``peer_ip`` (a single hop) is inside the allowlist."""
        if not peer_ip:
            return False
        try:
            addr = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        return any(addr in network for network in self.networks)

    def chain_is_trusted(self, *, peer_ip: str | None, forwarded_for: str | None) -> bool:
        """True iff the ``trusted_hops`` nearest hops are ALL allowlisted.

        For the default ``trusted_hops == 1`` this is exactly
        :meth:`peer_is_trusted` on the direct peer — ``X-Forwarded-For`` is
        ignored, so a forged XFF cannot widen trust (the single-hop invariant).

        For ``N > 1`` the nearest-first chain is ``[direct peer, *reversed(XFF)]``:
        ``X-Forwarded-For`` is appended oldest→newest, so its rightmost entry is
        the proxy one hop further out, etc. We require the first ``N`` of that
        chain to all be allowlisted. The ``(N+1)``-th entry is the purported
        client and is never required to be trusted; a missing/short XFF (fewer
        than ``N`` hops available) fails closed.
        """
        if self.trusted_hops <= 1:
            return self.peer_is_trusted(peer_ip)
        xff = [chunk.strip() for chunk in (forwarded_for or "").split(",") if chunk.strip()]
        chain = [peer_ip, *reversed(xff)]
        if len(chain) < self.trusted_hops:
            return False
        return all(self.peer_is_trusted(hop) for hop in chain[: self.trusted_hops])


class ProxyHeaderAuthMiddleware(BaseHTTPMiddleware):
    """Attach a transient principal for trusted-proxy + ``X-Forwarded-User`` requests.

    Mounted only when :class:`ProxyHeaderAuthConfig` is enabled. On every
    request it inspects the *direct peer* IP and, if trusted and the header
    names an existing user, sets ``request.state.proxy_principal``. It never
    raises on the request path: an untrusted peer, a missing header, or an
    unknown user simply leaves the principal unset (the request then falls
    through to normal cookie auth, ending in 401 if unauthenticated).
    """

    def __init__(self, app: ASGIApp, *, pool: asyncpg.Pool, config: ProxyHeaderAuthConfig) -> None:
        super().__init__(app)
        if not config.enabled:
            # Defensive: create_app only mounts an enabled config. A disabled
            # mount would be a wiring bug — fail loud rather than silently
            # inspecting headers with an empty allowlist.
            raise RuntimeError("ProxyHeaderAuthMiddleware mounted with a disabled config")
        self._pool = pool
        self._config = config

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        forwarded_user = request.headers.get(FORWARDED_USER_HEADER)
        # Trust is gated on the direct peer (and, only when trusted_hops > 1, the
        # additional nearest hops from X-Forwarded-For — but only allowlisted
        # entries count, so a forged XFF can never widen trust). With the default
        # single hop, X-Forwarded-For is ignored entirely.
        peer_ip = request.client.host if request.client else None
        if forwarded_user and self._config.chain_is_trusted(
            peer_ip=peer_ip, forwarded_for=request.headers.get(FORWARDED_FOR_HEADER)
        ):
            await self._resolve_proxy_identity(request, forwarded_user=forwarded_user, peer_ip=peer_ip)
        return await call_next(request)

    async def _resolve_proxy_identity(self, request: Request, *, forwarded_user: str, peer_ip: str | None) -> None:
        user_agent = request.headers.get("user-agent")
        user = await users_repo.get_user_by_username(self._pool, username=forwarded_user)
        if user is None:
            log.warning(
                "proxy header auth: trusted peer %s asserted unknown user %r — ignored",
                peer_ip,
                forwarded_user,
            )
            await auth_audit_repo.insert_attempt(
                self._pool,
                username=forwarded_user,
                ip=peer_ip,
                user_agent=user_agent,
                outcome="missing_user",
            )
            return
        request.state.proxy_principal = {
            "email": user.email or user.username,
            "session_id": f"proxy:{user.username}",
            "expires_at_unix": int(time.time()) + _PROXY_SESSION_TTL_SECONDS,
        }
        await auth_audit_repo.insert_attempt(
            self._pool,
            username=user.username,
            ip=peer_ip,
            user_agent=user_agent,
            outcome="ok",
        )


__all__ = [
    "FORWARDED_FOR_HEADER",
    "FORWARDED_USER_HEADER",
    "ProxyHeaderAuthConfig",
    "ProxyHeaderAuthMiddleware",
    "TRUSTED_PROXY_HOP_COUNT_ENV",
    "TRUSTED_PROXY_IPS_ENV",
    "TRUST_PROXY_AUTH_ENV",
]
