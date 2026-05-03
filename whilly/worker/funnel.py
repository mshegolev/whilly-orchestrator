"""Funnel URL discovery sources for the M2 worker URL-rotation flow.

The M2 localhost.run sidecar publishes a rotating
``https://<random>.lhr.life`` URL into both Postgres (``funnel_url``
singleton table, migration 010) and a shared-volume file
(``/funnel/url.txt``). Workers absorb the rotation transparently by
polling one of those sources on a configurable cadence.

This module owns the *source* abstraction the worker loop consumes —
each backend exposes the same async ``fetch()`` / ``aclose()`` surface
so the higher-level rotation loop can switch between modes via env
config alone.

Worker-import-path purity (PRD SC-6, ``.importlinter:worker-entry-purity``)
---------------------------------------------------------------------------
This module lives under :mod:`whilly.worker` and is allowed only to
depend on the standard library + already-blessed worker dependencies
(httpx, pydantic, :mod:`whilly.core`). The Postgres backend
intentionally imports ``asyncpg`` lazily via :func:`importlib.import_module`
*inside* the source class so the static import graph for
:mod:`whilly.cli.worker` and :mod:`whilly.worker.remote` stays clean
(no top-level ``import asyncpg``). The eager test-import gate
``tests/unit/test_worker_entrypoint_import_purity.py`` keeps the
runtime closure honest.

Configuration (env-var contract — must match library/environment.md)
--------------------------------------------------------------------

============================  ==========  ===========================================
Env var                       Default     Meaning
============================  ==========  ===========================================
``WHILLY_FUNNEL_URL_SOURCE``  ``static``  ``static`` (no polling, use
                                          ``WHILLY_CONTROL_URL`` verbatim — back-compat
                                          default), ``postgres`` (poll
                                          ``funnel_url`` table) or ``file`` (poll
                                          ``WHILLY_FUNNEL_URL_FILE``).
``WHILLY_FUNNEL_URL_FILE``    ``/funnel/url.txt``  Shared-volume file the sidecar
                                          atomically rewrites with the latest URL.
``WHILLY_FUNNEL_URL_POLL_SECONDS``  ``30`` (postgres) /  Poll cadence per source.
                              ``5`` (file)
============================  ==========  ===========================================
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

log = logging.getLogger(__name__)


FUNNEL_URL_SOURCE_ENV: Final[str] = "WHILLY_FUNNEL_URL_SOURCE"
FUNNEL_URL_FILE_ENV: Final[str] = "WHILLY_FUNNEL_URL_FILE"
FUNNEL_URL_POLL_SECONDS_ENV: Final[str] = "WHILLY_FUNNEL_URL_POLL_SECONDS"
FUNNEL_DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"

DEFAULT_FUNNEL_URL_FILE: Final[str] = "/funnel/url.txt"
DEFAULT_POSTGRES_POLL_SECONDS: Final[float] = 30.0
DEFAULT_FILE_POLL_SECONDS: Final[float] = 5.0

VALID_SOURCES: Final[frozenset[str]] = frozenset({"static", "postgres", "file"})


class FunnelUrlSourceError(RuntimeError):
    """Raised on misconfiguration the operator can fix.

    Distinct from transient lookup failures — those return ``None``
    from :meth:`FunnelUrlSource.fetch` so the rotation loop can keep
    polling.
    """


@runtime_checkable
class FunnelUrlSource(Protocol):
    """Pluggable URL discovery source consumed by the worker rotation loop.

    Each implementation owns its own poll cadence and returns ``None``
    when the URL is currently unavailable (sidecar still warming up,
    DB unreachable, file missing, etc.). The rotation loop treats
    ``None`` as "keep current URL, re-poll later" rather than as an
    error — operators rarely want a transient failure to tear down an
    in-flight task.
    """

    poll_interval: float

    async def fetch(self) -> str | None:
        """Return the latest URL or ``None`` if currently unavailable."""

    async def aclose(self) -> None:
        """Release any resources held by the source (DB conn, file handle)."""


class StaticUrlSource:
    """Trivial source returning a pre-computed URL with no polling.

    The :data:`poll_interval` is set to ``math.inf`` so the rotation
    loop never schedules a wake-up — back-compat invariant for
    ``WHILLY_FUNNEL_URL_SOURCE=static``.
    """

    def __init__(self, url: str) -> None:
        if not url or not url.strip():
            raise FunnelUrlSourceError("StaticUrlSource requires a non-empty URL")
        self._url = url.strip()
        self.poll_interval = float("inf")

    async def fetch(self) -> str:
        return self._url

    async def aclose(self) -> None:
        return None


class FileUrlSource:
    """Poll a local file the funnel sidecar atomically rewrites.

    The sidecar uses ``mv -f tmp dst`` (atomic rename) so a partial
    write is never observable. We do a plain :func:`pathlib.Path.read_text`
    + ``.strip()``; an empty / missing file returns ``None`` so the
    rotation loop simply keeps polling.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        poll_interval: float = DEFAULT_FILE_POLL_SECONDS,
    ) -> None:
        self._path = Path(os.fspath(path))
        if poll_interval <= 0:
            raise FunnelUrlSourceError(f"FileUrlSource poll_interval must be positive, got {poll_interval}")
        self.poll_interval = float(poll_interval)

    async def fetch(self) -> str | None:
        try:
            text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("FileUrlSource: %s unreadable (%s)", self._path, exc)
            return None
        url = text.strip()
        return url or None

    async def aclose(self) -> None:
        return None


class PostgresUrlSource:
    """Poll the ``funnel_url`` singleton table via asyncpg.

    The asyncpg import is intentionally *deferred* to runtime via
    :func:`importlib.import_module`. The module-level static graph
    therefore never references ``asyncpg``, which keeps the
    ``worker-entry-purity`` import-linter contract green and the
    ``test_worker_entrypoint_import_purity`` runtime gate satisfied —
    workers shipping without the ``[server]`` extras (no asyncpg)
    only hit this branch when the operator opts into
    ``WHILLY_FUNNEL_URL_SOURCE=postgres``, in which case asyncpg is
    a documented runtime requirement.
    """

    _SELECT_LATEST_URL_SQL: str = "SELECT url FROM funnel_url ORDER BY updated_at DESC LIMIT 1"

    def __init__(
        self,
        dsn: str,
        poll_interval: float = DEFAULT_POSTGRES_POLL_SECONDS,
    ) -> None:
        if not dsn or not dsn.strip():
            raise FunnelUrlSourceError(
                f"PostgresUrlSource requires a non-empty DSN (set {FUNNEL_DATABASE_URL_ENV} or pass dsn explicitly)"
            )
        if poll_interval <= 0:
            raise FunnelUrlSourceError(f"PostgresUrlSource poll_interval must be positive, got {poll_interval}")
        self._dsn = dsn.strip()
        self.poll_interval = float(poll_interval)
        self._pool: object | None = None
        self._pool_lock = asyncio.Lock()

    async def _ensure_pool(self) -> object:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                asyncpg = importlib.import_module("asyncpg")
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=1,
                    max_size=1,
                )
        assert self._pool is not None
        return self._pool

    async def fetch(self) -> str | None:
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:  # type: ignore[attr-defined]
                row = await conn.fetchval(self._SELECT_LATEST_URL_SQL)
        except Exception as exc:
            log.warning("PostgresUrlSource: lookup failed (%s)", exc)
            return None
        if not row:
            return None
        url = str(row).strip()
        return url or None

    async def aclose(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is None:
            return
        try:
            await pool.close()  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("PostgresUrlSource: pool close error (%s)", exc)


def _parse_poll_interval(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise FunnelUrlSourceError(f"{FUNNEL_URL_POLL_SECONDS_ENV} must be a positive number, got {raw!r}") from exc
    if value <= 0:
        raise FunnelUrlSourceError(f"{FUNNEL_URL_POLL_SECONDS_ENV} must be positive, got {value}")
    return value


def make_funnel_url_source(
    *,
    control_url: str,
    env: dict[str, str] | None = None,
) -> FunnelUrlSource:
    """Build a :class:`FunnelUrlSource` from the M2 env-var contract.

    Parameters
    ----------
    control_url:
        The operator-supplied control-plane base URL. Used as the
        seed value for ``static`` mode and as the fallback URL when
        a non-static source has not yet observed any value.
    env:
        Override mapping (test seam). ``None`` reads from
        :data:`os.environ`.

    Raises
    ------
    FunnelUrlSourceError
        On malformed env values (unknown source, non-positive poll
        cadence, missing DSN for ``postgres`` mode).
    """
    e = env if env is not None else os.environ
    raw_source = (e.get(FUNNEL_URL_SOURCE_ENV) or "static").strip().lower()
    if raw_source not in VALID_SOURCES:
        raise FunnelUrlSourceError(f"{FUNNEL_URL_SOURCE_ENV}={raw_source!r} not in {sorted(VALID_SOURCES)}")

    if raw_source == "static":
        return StaticUrlSource(control_url)

    raw_poll = e.get(FUNNEL_URL_POLL_SECONDS_ENV)
    if raw_source == "file":
        path = (e.get(FUNNEL_URL_FILE_ENV) or DEFAULT_FUNNEL_URL_FILE).strip()
        if not path:
            path = DEFAULT_FUNNEL_URL_FILE
        poll = _parse_poll_interval(raw_poll, DEFAULT_FILE_POLL_SECONDS)
        return FileUrlSource(path, poll_interval=poll)

    dsn = (e.get(FUNNEL_DATABASE_URL_ENV) or "").strip()
    if not dsn:
        raise FunnelUrlSourceError(f"{FUNNEL_URL_SOURCE_ENV}=postgres requires {FUNNEL_DATABASE_URL_ENV}")
    poll = _parse_poll_interval(raw_poll, DEFAULT_POSTGRES_POLL_SECONDS)
    return PostgresUrlSource(dsn, poll_interval=poll)


__all__ = [
    "DEFAULT_FILE_POLL_SECONDS",
    "DEFAULT_FUNNEL_URL_FILE",
    "DEFAULT_POSTGRES_POLL_SECONDS",
    "FUNNEL_DATABASE_URL_ENV",
    "FUNNEL_URL_FILE_ENV",
    "FUNNEL_URL_POLL_SECONDS_ENV",
    "FUNNEL_URL_SOURCE_ENV",
    "FileUrlSource",
    "FunnelUrlSource",
    "FunnelUrlSourceError",
    "PostgresUrlSource",
    "StaticUrlSource",
    "VALID_SOURCES",
    "make_funnel_url_source",
]
