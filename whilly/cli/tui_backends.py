"""Transport backends for `whilly tui`: direct Postgres (full) and
read-only HTTP against the WUI control-plane."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from whilly.operator_snapshot_codec import snapshot_from_dict
from whilly.operator_views import OperatorSnapshot

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def build_scheme_guard_error(url: str) -> ValueError:
    return ValueError(
        f"whilly tui: refusing plain http:// to non-loopback host in {url!r}; "
        "use https:// or pass --insecure (WHILLY_INSECURE=1)."
    )


@runtime_checkable
class OperatorBackend(Protocol):
    read_only: bool

    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot: ...

    async def close(self) -> None: ...


class DbOperatorBackend:
    """Direct Postgres pool — full capability (view + control + review)."""

    read_only = False

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot:
        # Lazy import keeps the HTTP path free of the DB view module's import cost.
        from whilly.operator_views import fetch_operator_snapshot

        return await fetch_operator_snapshot(self._pool, plan_id=plan_id)

    @property
    def pool(self) -> Any:
        return self._pool

    async def close(self) -> None:
        from whilly.adapters.db import close_pool

        await close_pool(self._pool)


class HttpOperatorBackend:
    """Read-only HTTP backend against GET /api/v1/operator/snapshot."""

    read_only = True

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        insecure: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK_HOSTS and not insecure:
            raise build_scheme_guard_error(base_url)
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=10.0,
        )

    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot:
        params = {"plan": plan_id} if plan_id else None
        resp = await self._client.get(f"{self._base}/api/v1/operator/snapshot", params=params)
        resp.raise_for_status()
        return snapshot_from_dict(resp.json())

    async def close(self) -> None:
        await self._client.aclose()
