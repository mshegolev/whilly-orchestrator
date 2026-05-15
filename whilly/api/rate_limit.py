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
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from typing import Final

logger = logging.getLogger(__name__)

_RATE_LIMIT_ENABLED_ENV: Final[str] = "WHILLY_AUTH_RATE_LIMIT_ENABLED"
_WINDOW_SECONDS: Final[float] = 60.0
_DEFAULT_CAP: Final[int] = 10


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


# Module-level singleton.  Callers import ``_LIMITER`` directly and call
# ``_LIMITER.allow(ip)`` — no factory required for the simple single-process case.
_LIMITER: IPRateLimiter = IPRateLimiter()


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
    "_LIMITER",
    "_RATE_LIMIT_ENABLED_ENV",
    "allow",
]
