"""Fail-closed research fetcher with per-hop DNS validation and IP pinning."""

from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

from whilly.swarm.learning.research import FetchPolicy, ResearchBlocked, ResearchDocument, ResearchNetwork


class RestrictedResearchFetcher:
    def __init__(self, network: ResearchNetwork) -> None:
        self._network = network

    async def fetch(self, url: str, policy: FetchPolicy) -> ResearchDocument:
        current = url
        for redirect_count in range(policy.max_redirects + 1):
            parsed = urlsplit(current)
            host = (parsed.hostname or "").lower()
            if (
                parsed.scheme != "https"
                or not host
                or parsed.username
                or parsed.password
                or host not in policy.allowed_hosts
            ):
                raise ResearchBlocked("destination_not_allowed")
            addresses = await self._network.resolve(host)
            if not addresses:
                raise ResearchBlocked("dns_unavailable")
            public = tuple(address for address in addresses if ip_address(address).is_global)
            if len(public) != len(addresses):
                raise ResearchBlocked("non_public_destination")
            response = await self._network.request(
                current,
                connect_ip=public[0],
                host=host,
                timeout_seconds=policy.timeout_seconds,
            )
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count == policy.max_redirects:
                    raise ResearchBlocked("redirect_limit")
                location = response.headers.get("location")
                if not location:
                    raise ResearchBlocked("redirect_without_location")
                current = urljoin(current, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ResearchBlocked(f"http_status_{response.status}")
            if len(response.body) > policy.max_bytes:
                raise ResearchBlocked("document_too_large")
            return ResearchDocument(
                uri=current,
                retrieved_at=datetime.now(timezone.utc),
                media_type=response.headers.get("content-type", "application/octet-stream"),
                body=response.body,
            )
        raise ResearchBlocked("redirect_limit")


__all__ = ["RestrictedResearchFetcher"]
