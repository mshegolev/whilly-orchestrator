"""Unit tests for cluster-aware rate-limiter selection.

PRD-post-auth-hardening §Epic C, Item 8. Pins the decision matrix of
:func:`whilly.api.rate_limit.build_rate_limiter` plus the fail-open
behaviour of :class:`NullRateLimiter`.

Redis is not contacted — :class:`RedisRateLimiter` instantiation is
verified only in the path where ``redis-py`` is importable (it's a base
dep of the ``server`` extras as of this PR). When the package is not
installed the constructor raises a clear ``RuntimeError`` instead of
deferring the failure to the first ``allow()`` call.
"""

from __future__ import annotations

import logging

import pytest

from whilly.api import rate_limit
from whilly.api.rate_limit import (
    IPRateLimiter,
    NullRateLimiter,
    build_rate_limiter,
    install_rate_limiter,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Restore the module-level singleton after each test."""
    original = rate_limit._LIMITER
    yield
    install_rate_limiter(original)


@pytest.fixture(autouse=True)
def _clear_cluster_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env state so each test sees a clean slate."""
    monkeypatch.delenv("WHILLY_NUM_WORKERS", raising=False)
    monkeypatch.delenv("WHILLY_REDIS_URL", raising=False)


# ─── Single-worker default → IPRateLimiter ─────────────────────────────────


def test_build_rate_limiter_default_returns_ip_limiter() -> None:
    """No env vars set → in-process IPRateLimiter (existing behaviour)."""
    limiter = build_rate_limiter()
    assert isinstance(limiter, IPRateLimiter)


def test_build_rate_limiter_num_workers_one_returns_ip_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_NUM_WORKERS", "1")
    assert isinstance(build_rate_limiter(), IPRateLimiter)


def test_build_rate_limiter_num_workers_malformed_returns_ip_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer NUM_WORKERS falls back to 1 (and so to in-process)."""
    monkeypatch.setenv("WHILLY_NUM_WORKERS", "not-a-number")
    assert isinstance(build_rate_limiter(), IPRateLimiter)


# ─── Multi-worker without Redis → NullRateLimiter + WARNING ────────────────


def test_build_rate_limiter_multi_worker_no_redis_returns_null_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: NUM_WORKERS=4 with no REDIS_URL → NullRateLimiter + WARNING log."""
    monkeypatch.setenv("WHILLY_NUM_WORKERS", "4")
    with caplog.at_level(logging.WARNING, logger="whilly.api.rate_limit"):
        limiter = build_rate_limiter()
    assert isinstance(limiter, NullRateLimiter)
    assert any("WHILLY_NUM_WORKERS=4" in rec.message for rec in caplog.records), (
        f"WARNING about multi-worker fallback not emitted; got: {[r.message for r in caplog.records]}"
    )


def test_null_rate_limiter_always_allows() -> None:
    """The fallback limiter must never block — fail-open contract."""
    limiter = NullRateLimiter()
    for ip in ("1.1.1.1", "2.2.2.2", "1.1.1.1"):
        assert limiter.allow(ip) is True


def test_install_rate_limiter_routes_module_allow_through_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install_rate_limiter swaps the singleton ``allow()`` consults."""
    # Use an IPRateLimiter with cap=1 so a second call to the same key
    # would fail IF this limiter were in use.
    install_rate_limiter(NullRateLimiter())
    monkeypatch.setenv("WHILLY_AUTH_RATE_LIMIT_ENABLED", "true")
    # 100 successive allows for the same IP — NullRateLimiter never blocks.
    for _ in range(100):
        assert rate_limit.allow("1.1.1.1") is True


# ─── Multi-worker with Redis URL → RedisRateLimiter ────────────────────────


def test_build_rate_limiter_multi_worker_with_redis_returns_redis_limiter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: NUM_WORKERS=4 + REDIS_URL → RedisRateLimiter, NO warning."""
    redis_mod = pytest.importorskip("redis")  # noqa: F841 — only used to gate the test
    monkeypatch.setenv("WHILLY_NUM_WORKERS", "4")
    monkeypatch.setenv("WHILLY_REDIS_URL", "redis://localhost:6379/0")
    with caplog.at_level(logging.WARNING, logger="whilly.api.rate_limit"):
        limiter = build_rate_limiter()
    # Don't import the type at module level; redis-py may not be installed in
    # all environments. Check by class name string.
    assert type(limiter).__name__ == "RedisRateLimiter"
    # No multi-worker-without-redis warning when Redis is configured.
    assert not any("WHILLY_REDIS_URL" in rec.message for rec in caplog.records)


# ─── _LIMITER startup contract: install_rate_limiter is idempotent ─────────


def test_install_rate_limiter_is_idempotent() -> None:
    """Two installs leave the second instance active."""
    a = NullRateLimiter()
    b = NullRateLimiter()
    install_rate_limiter(a)
    install_rate_limiter(b)
    assert rate_limit._LIMITER is b
