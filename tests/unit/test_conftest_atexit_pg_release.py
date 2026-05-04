"""Unit tests for the atexit pg-release semantics in tests/conftest.py.

Pins the closure-local ``pg`` reference release performed by
``_register_pg_atexit_stop`` (fix-m3-testcontainers-vsock-flake-mitigation):

* The hook is idempotent — calling it twice still only invokes ``stop()`` once.
* After the first invocation the closure drops its ``pg`` reference (sets the
  internal slot to ``None``) so the DockerClient socket reference held by the
  ``PostgresContainer`` instance can be GC'd between fixture teardown and the
  final ``atexit`` flush. This is the key vsock-proxy mitigation: a long
  pytest session that accumulates dozens of stopped containers must not pin
  their requests-unixsocket / DockerClient sockets in the closure forever.
* Even if ``pg.stop()`` raises, the ``pg`` reference is still cleared.
* The hook still no-ops if called after ``state["pg"]`` is already ``None``.
"""

from __future__ import annotations

import atexit
import gc
import weakref
from typing import Any

from tests.conftest import _register_pg_atexit_stop


class _FakePostgresContainer:
    """Lightweight stand-in for testcontainers' :class:`PostgresContainer`."""

    def __init__(self) -> None:
        self.stop_call_count = 0
        self.stop_should_raise: BaseException | None = None

    def stop(self) -> None:
        self.stop_call_count += 1
        if self.stop_should_raise is not None:
            raise self.stop_should_raise


def _without_atexit(hook: Any) -> None:
    """Best-effort unregister so the test's hook doesn't fire at interpreter exit."""
    try:
        atexit.unregister(hook)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def test_register_atexit_stop_is_idempotent() -> None:
    """Two calls to the returned hook still only invoke ``pg.stop()`` once."""
    pg = _FakePostgresContainer()
    hook = _register_pg_atexit_stop(pg)  # type: ignore[arg-type]
    try:
        hook()
        hook()
        assert pg.stop_call_count == 1
    finally:
        _without_atexit(hook)


def test_register_atexit_stop_releases_pg_reference_after_stop() -> None:
    """After the first stop fires the closure drops its ``pg`` reference.

    Verified via :class:`weakref.ref` — when the only strong references in
    the test scope are dropped, the container becomes garbage-collectable
    only if the closure has released its slot.
    """
    pg = _FakePostgresContainer()
    pg_ref = weakref.ref(pg)
    hook = _register_pg_atexit_stop(pg)  # type: ignore[arg-type]
    try:
        hook()
        assert pg.stop_call_count == 1
        del pg
        gc.collect()
        assert pg_ref() is None, (
            "Closure still pins PostgresContainer after stop fired — DockerClient socket cannot be GC'd."
        )
    finally:
        _without_atexit(hook)


def test_register_atexit_stop_releases_pg_reference_even_when_stop_raises() -> None:
    """``pg`` is dropped even if ``pg.stop()`` raises — atexit best-effort policy."""
    pg = _FakePostgresContainer()
    pg.stop_should_raise = RuntimeError("docker daemon evaporated mid-stop")
    pg_ref = weakref.ref(pg)
    hook = _register_pg_atexit_stop(pg)  # type: ignore[arg-type]
    try:
        hook()
        assert pg.stop_call_count == 1
        del pg
        gc.collect()
        assert pg_ref() is None, "Closure must release PostgresContainer even on stop() exception."
    finally:
        _without_atexit(hook)


def test_register_atexit_stop_second_call_after_release_is_noop() -> None:
    """A second hook invocation after the pg slot is cleared does not crash."""
    pg = _FakePostgresContainer()
    hook = _register_pg_atexit_stop(pg)  # type: ignore[arg-type]
    try:
        hook()
        # Simulate any path that nulls the slot independently — the second
        # call still returns cleanly without raising AttributeError on None.
        hook()
        assert pg.stop_call_count == 1
    finally:
        _without_atexit(hook)
