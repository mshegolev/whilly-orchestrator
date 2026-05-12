"""Rate limiting and backoff strategies for scheduler."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

log = logging.getLogger(__name__)


class BackoffStrategy(Enum):
    """Backoff strategy types."""

    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    FIBONACCI = "fibonacci"


@dataclass
class RateLimiter:
    """Rate limiter with configurable backoff strategy."""

    max_retries: int = 5
    initial_delay: float = 1.0
    max_delay: float = 60.0
    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    jitter: bool = True

    async def call_with_retry(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call a function with retry logic.

        Args:
            func: Async function to call
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result from function call

        Raises:
            Exception: If all retries exhausted
        """
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                last_exception = exc

                if attempt >= self.max_retries:
                    log.error(
                        "Max retries (%d) exhausted for %s",
                        self.max_retries,
                        func.__name__,
                    )
                    raise

                delay = self._calculate_delay(attempt)
                log.warning(
                    "Attempt %d/%d failed: %s. Retrying in %.1fs",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        raise last_exception

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for attempt number.

        Args:
            attempt: 0-based attempt number

        Returns:
            Delay in seconds
        """
        if self.strategy == BackoffStrategy.LINEAR:
            delay = self.initial_delay * (attempt + 1)
        elif self.strategy == BackoffStrategy.EXPONENTIAL:
            delay = self.initial_delay * (2**attempt)
        elif self.strategy == BackoffStrategy.FIBONACCI:
            fib_sequence = [1, 1, 2, 3, 5, 8, 13, 21, 34]
            fib_index = min(attempt, len(fib_sequence) - 1)
            delay = self.initial_delay * fib_sequence[fib_index]
        else:
            delay = self.initial_delay

        delay = min(delay, self.max_delay)

        if self.jitter:
            import random

            jitter = random.uniform(0, delay * 0.1)
            delay += jitter

        return delay


class PollRateLimiter:
    """Rate limiter for poll cycles to respect Jira API limits."""

    def __init__(
        self,
        min_interval_seconds: float = 1.0,
        max_requests_per_minute: int = 60,
    ) -> None:
        """Initialize poll rate limiter.

        Args:
            min_interval_seconds: Minimum time between polls
            max_requests_per_minute: Maximum API requests per minute
        """
        self.min_interval_seconds = min_interval_seconds
        self.max_requests_per_minute = max_requests_per_minute
        self.last_poll_time = 0.0
        self.poll_times: list[float] = []

    async def wait_until_ready(self) -> None:
        """Wait until rate limit allows next poll."""
        now = time.time()

        # Check minimum interval
        time_since_last = now - self.last_poll_time
        if time_since_last < self.min_interval_seconds:
            wait_time = self.min_interval_seconds - time_since_last
            log.debug("Rate limited: waiting %.2fs", wait_time)
            await asyncio.sleep(wait_time)

        # Check requests per minute
        minute_ago = now - 60.0
        recent_polls = [t for t in self.poll_times if t > minute_ago]

        if len(recent_polls) >= self.max_requests_per_minute:
            wait_time = recent_polls[0] + 60.0 - now + 0.1
            log.warning(
                "API rate limit: %d requests in last minute, waiting %.1fs",
                len(recent_polls),
                wait_time,
            )
            await asyncio.sleep(wait_time)

        self.last_poll_time = time.time()
        self.poll_times.append(self.last_poll_time)

        # Keep only last minute of poll times
        minute_ago = self.last_poll_time - 60.0
        self.poll_times = [t for t in self.poll_times if t > minute_ago]
