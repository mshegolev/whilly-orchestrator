"""In-process IP-based rate limiter for the authentication endpoints.

A sliding-window (token-bucket equivalent) is maintained per source IP using a
``collections.deque[float]`` of event timestamps.  The window is 60 seconds and
the default cap is 10 requests.  Events outside the window are purged on each
``allow()`` call so memory stays bounded to ``O(cap)`` per unique IP.

Thread safety: a single ``threading.Lock`` protects the shared ``_buckets``
dict.  FastAPI / uvicorn may run several threads (``--workers > 1``), but each
worker process has its own address space, so the limiter is per-process and not
cluster-wide.  For cluster-wide rate limiting, an external Redis store is the
right tool; this module deliberately stays stdlib-only and covers the common
single-process deployment.

Controlled entirely by ``WHILLY_AUTH_RATE_LIMIT_ENABLED`` (default ``"true"``).
When disabled, ``allow()`` always returns True — useful in test environments
where the same source IP hammers the login endpoint with legitimate requests.

PRD-post-auth-hardening §Epic C, Item 8 — cluster awareness:
:func:`build_rate_limiter` selects the limiter at startup based on
``WHILLY_NUM_WORKERS`` and ``WHILLY_REDIS_URL``. With ``NUM_WORKERS > 1``
and no Redis URL, a WARNING is logged and a :class:`NullRateLimiter`
(always allow) takes over — fail-open is the safe default for an
unreliable cluster counter rather than a hard error. With both vars set,
:class:`RedisRateLimiter` is instantiated; the cluster-wide INCR/EXPIRE
counter is best-effort and degrades to allow-on-error.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from typing import Final, Protocol

logger = logging.getLogger(__name__)

_RATE_LIMIT_ENABLED_ENV: Final[str] = "WHILLY_AUTH_RATE_LIMIT_ENABLED"
_NUM_WORKERS_ENV: Final[str] = "WHILLY_NUM_WORKERS"
_REDIS_URL_ENV: Final[str] = "WHILLY_REDIS_URL"
_WINDOW_SECONDS: Final[float] = 60.0
_DEFAULT_CAP: Final[int] = 10


class RateLimiter(Protocol):
    """Minimal interface all rate-limiter implementations share."""

    def allow(self, key: str) -> bool: ...


class IPRateLimiter:
    """Sliding-window IP rate limiter backed by per-key timestamp deques.

    Instantiate once at module level (see :data:`_LIMITER` below) and call
    :meth:`allow` on every incoming auth request.  The instance is thread-safe.
    """

    def __init__(self, *, cap: int = _DEFAULT_CAP, window_seconds: float = _WINDOW_SECONDS) -> None:
        if cap < 1:
            raise ValueError(f"IPRateLimiter: cap must be >= 1, got {cap!r}")
        if window_seconds <= 0:
            raise ValueError(f"IPRateLimiter: window_seconds must be > 0, got {window_seconds!r}")
        self._cap: int = cap
        self._window: float = window_seconds
        self._buckets: dict[str, collections.deque[float]] = {}
        self._lock: threading.Lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Return True if the ``key`` (typically a source IP) is within the rate limit.

        Purges timestamps older than the window on every call.  When the bucket
        is full, returns False without recording a new event so the failure itself
        does not count toward the cap.
        """
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = collections.deque()
            bucket = self._buckets[key]
            # Purge stale events from the left end.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._cap:
                return False
            bucket.append(now)
            return True

    def reset(self, key: str) -> None:
        """Clear the bucket for ``key`` (useful in tests and on successful auth)."""
        with self._lock:
            self._buckets.pop(key, None)


def _rate_limit_enabled() -> bool:
    """Return True unless ``WHILLY_AUTH_RATE_LIMIT_ENABLED`` is explicitly falsy."""
    raw = (os.environ.get(_RATE_LIMIT_ENABLED_ENV) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class NullRateLimiter:
    """No-op limiter — always allows. Used when cluster-correctness is
    impossible (multi-worker without a shared Redis counter) but the auth
    routes still call ``allow()``. Fail-open is the safe default: under-
    counting login attempts is preferable to bricking the auth surface on
    a misconfigured deployment.
    """

    def allow(self, key: str) -> bool:  # noqa: ARG002 — protocol satisfaction
        return True

    def reset(self, key: str) -> None:  # noqa: ARG002 — protocol satisfaction
        return None


class RedisRateLimiter:
    """Cluster-wide rate limiter using Redis ``INCR`` + ``EXPIRE``.

    Best-effort stub per PRD §Epic C Item 8 — the full sliding-window
    implementation is a stretch goal. This version uses a coarse fixed-
    window counter (``INCR key`` then ``EXPIRE key window`` on the first
    hit), which is the standard "imprecise but cheap" cluster-wide
    pattern. A request is allowed iff the counter <= ``cap`` after INCR.

    Any Redis error (unreachable server, auth failure, network timeout)
    fail-opens with a logged warning — the auth path must not crash
    because the rate counter is temporarily unavailable. ``redis-py`` is
    imported lazily so this module stays importable in deployments that
    don't use Redis.
    """

    def __init__(self, *, url: str, cap: int = _DEFAULT_CAP, window_seconds: float = _WINDOW_SECONDS) -> None:
        if cap < 1:
            raise ValueError(f"RedisRateLimiter: cap must be >= 1, got {cap!r}")
        if window_seconds <= 0:
            raise ValueError(f"RedisRateLimiter: window_seconds must be > 0, got {window_seconds!r}")
        self._url: str = url
        self._cap: int = cap
        self._window: int = max(1, int(window_seconds))
        self._client: object | None = None
        # Lazy redis-py import: if the package isn't installed we still want
        # build_rate_limiter() to surface a clear error from this constructor
        # rather than from the first allow() call.
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "RedisRateLimiter: redis-py is not installed; "
                "`pip install redis>=5` or unset WHILLY_REDIS_URL to use the in-process limiter."
            ) from exc
        self._client = redis.Redis.from_url(url, decode_responses=True)

    def allow(self, key: str) -> bool:
        client = self._client
        if client is None:
            return True
        redis_key = f"whilly:ratelimit:{key}"
        try:
            count = int(client.incr(redis_key))  # type: ignore[attr-defined]
            if count == 1:
                client.expire(redis_key, self._window)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — fail-open on any redis error
            logger.warning(
                "RedisRateLimiter: backing redis is unavailable; failing OPEN for key=%r",
                key,
                exc_info=True,
            )
            return True
        return count <= self._cap

    def reset(self, key: str) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.delete(f"whilly:ratelimit:{key}")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("RedisRateLimiter: reset failed for key=%r", key, exc_info=True)


def _parse_num_workers() -> int:
    """Return WHILLY_NUM_WORKERS as a positive int; 1 if unset or malformed."""
    raw = (os.environ.get(_NUM_WORKERS_ENV) or "").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except ValueError:
        return 1
    return max(1, n)


def build_rate_limiter() -> RateLimiter:
    """Select the rate limiter based on cluster topology env vars.

    Decision matrix:

    +-----------------+---------------+-----------------------+
    | NUM_WORKERS     | REDIS_URL set | Limiter               |
    +=================+===============+=======================+
    | 1 (default)     | either        | :class:`IPRateLimiter`|
    +-----------------+---------------+-----------------------+
    | > 1             | no            | :class:`NullRateLimiter` + WARNING |
    +-----------------+---------------+-----------------------+
    | > 1             | yes           | :class:`RedisRateLimiter` |
    +-----------------+---------------+-----------------------+

    Called once at app startup. The returned instance can be installed
    by ``install_rate_limiter()`` so the module-level :func:`allow`
    delegates to it.
    """
    num_workers = _parse_num_workers()
    redis_url = (os.environ.get(_REDIS_URL_ENV) or "").strip()
    if num_workers <= 1:
        return IPRateLimiter()
    if not redis_url:
        logger.warning(
            "rate_limit: %s=%d but %s is not set — in-process limiter would "
            "under-count across workers. Falling back to NullRateLimiter "
            "(fail-open). Set %s=redis://host:port/0 to enable cluster-wide "
            "rate limiting.",
            _NUM_WORKERS_ENV,
            num_workers,
            _REDIS_URL_ENV,
            _REDIS_URL_ENV,
        )
        return NullRateLimiter()
    return RedisRateLimiter(url=redis_url)


def install_rate_limiter(limiter: RateLimiter) -> None:
    """Replace the module-level singleton consulted by :func:`allow`.

    Idempotent — repeated calls overwrite the previous instance. Tests
    typically call this in a fixture and restore the original on teardown.
    """
    global _LIMITER
    _LIMITER = limiter


# Module-level singleton.  Callers import ``_LIMITER`` directly and call
# ``_LIMITER.allow(ip)`` — no factory required for the simple single-process case.
_LIMITER: RateLimiter = IPRateLimiter()


def allow(key: str) -> bool:
    """Module-level helper: check the singleton limiter, or bypass when disabled.

    This is the function the route handlers call.  Checking the env var on
    every call is intentional — operators can flip ``WHILLY_AUTH_RATE_LIMIT_ENABLED``
    at runtime without a restart (e.g. via systemd override + ``systemctl setenv``).
    """
    if not _rate_limit_enabled():
        return True
    result = _LIMITER.allow(key)
    if not result:
        logger.warning("rate_limit: auth request denied for key=%r (window cap reached)", key)
    return result


__all__ = [
    "IPRateLimiter",
    "NullRateLimiter",
    "RateLimiter",
    "RedisRateLimiter",
    "_LIMITER",
    "_NUM_WORKERS_ENV",
    "_RATE_LIMIT_ENABLED_ENV",
    "_REDIS_URL_ENV",
    "allow",
    "build_rate_limiter",
    "install_rate_limiter",
]
