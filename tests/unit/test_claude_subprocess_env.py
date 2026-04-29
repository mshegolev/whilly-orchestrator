"""Unit tests for Claude subprocess env injection (TASK-109-3).

Pins SC-4 of ``docs/PRD-v41-claude-proxy.md``: HTTPS_PROXY and
NO_PROXY land on the **spawned** Claude env, never on the Whilly
parent process. Both call sites are covered:

* :func:`whilly.adapters.runner.claude_cli._spawn_and_collect`
  (worker → Claude path)
* :func:`whilly.prd_generator._call_claude` (PRD-wizard path)

Strategy: monkeypatch the spawn primitives and inspect the ``env``
kwarg they receive. We don't run real Claude — these tests are
about wiring, not about Claude itself.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from whilly.adapters.runner.proxy import (
    INHERITED_HTTPS_PROXY_ENV,
    WHILLY_PROXY_URL_ENV,
)


# ─── claude_cli._spawn_and_collect ────────────────────────────────────────


@pytest.fixture
def captured_spawn_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``asyncio.create_subprocess_exec`` and capture the env kwarg.

    Returns a dict that the test inspects after the spawn. Keys:
      - ``"called"``: True iff the patch fired
      - ``"env"``: the env kwarg passed (None if missing)
    """
    captured: dict[str, Any] = {"called": False, "env": None}

    async def fake_spawn(*args: Any, **kwargs: Any) -> Any:
        captured["called"] = True
        captured["env"] = kwargs.get("env")
        # Return a proc-shaped mock so _spawn_and_collect can finish.
        proc = MagicMock()
        proc.communicate = _async_return((b'{"result": "ok"}', b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr("whilly.adapters.runner.claude_cli.asyncio.create_subprocess_exec", fake_spawn)
    return captured


def _async_return(value: Any) -> Any:
    """Helper: an async no-arg callable that returns ``value``."""

    async def coro(*_a: Any, **_kw: Any) -> Any:
        return value

    return coro


async def test_claude_cli_no_proxy_default(
    captured_spawn_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without proxy env vars: spawned env has no HTTPS_PROXY / NO_PROXY."""
    monkeypatch.delenv(WHILLY_PROXY_URL_ENV, raising=False)
    monkeypatch.delenv(INHERITED_HTTPS_PROXY_ENV, raising=False)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/true")  # any binary that exists

    from whilly.adapters.runner.claude_cli import _spawn_and_collect

    await _spawn_and_collect(prompt="hi", model="m")

    assert captured_spawn_env["called"]
    env = captured_spawn_env["env"]
    assert env is not None
    assert "HTTPS_PROXY" not in env
    # NO_PROXY may or may not be present (parent env can carry it from
    # tests host); the contract is "no diff vs parent_env".
    if "NO_PROXY" in env:
        assert env["NO_PROXY"] == os.environ.get("NO_PROXY")


async def test_claude_cli_with_whilly_proxy_url_env(
    captured_spawn_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHILLY_CLAUDE_PROXY_URL set → child env carries the diff."""
    monkeypatch.setenv(WHILLY_PROXY_URL_ENV, "http://127.0.0.1:11112")
    monkeypatch.delenv(INHERITED_HTTPS_PROXY_ENV, raising=False)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/true")

    from whilly.adapters.runner.claude_cli import _spawn_and_collect

    await _spawn_and_collect(prompt="hi", model="m")

    env = captured_spawn_env["env"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:11112"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"


async def test_claude_cli_does_not_mutate_os_environ(
    captured_spawn_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-4: parent os.environ untouched after spawn."""
    monkeypatch.setenv(WHILLY_PROXY_URL_ENV, "http://example:1")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/true")

    from whilly.adapters.runner.claude_cli import _spawn_and_collect

    await _spawn_and_collect(prompt="hi", model="m")

    # Parent env was not mutated — Whilly's own asyncpg / httpx sees no proxy.
    assert "HTTPS_PROXY" not in os.environ


# ─── prd_generator._call_claude ───────────────────────────────────────────


def test_prd_generator_call_claude_with_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD-wizard's call site also injects HTTPS_PROXY into spawned env."""
    monkeypatch.setenv(WHILLY_PROXY_URL_ENV, "http://prd-proxy:9999")
    monkeypatch.delenv(INHERITED_HTTPS_PROXY_ENV, raising=False)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/true")

    captured: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["env"] = kwargs.get("env")
            self.stdout = _StreamLike(b'{"ok": 1}')
            self.stderr = _StreamLike(b"")
            self.returncode = 0
            self.pid = 12345

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return ('{"ok": 1}', "")

        def kill(self) -> None:
            pass

        def wait(self) -> int:
            return 0

    monkeypatch.setattr("whilly.prd_generator.subprocess.Popen", FakePopen)

    from whilly.prd_generator import _call_claude

    # _call_claude returns the stdout text; we only care that the spawn
    # got the right env — the return value is irrelevant here.
    _call_claude(prompt="any", model="m")

    env = captured["env"]
    assert env is not None
    assert env["HTTPS_PROXY"] == "http://prd-proxy:9999"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_prd_generator_call_claude_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy env → spawned env has no HTTPS_PROXY (regression-free)."""
    monkeypatch.delenv(WHILLY_PROXY_URL_ENV, raising=False)
    monkeypatch.delenv(INHERITED_HTTPS_PROXY_ENV, raising=False)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/true")

    captured: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["env"] = kwargs.get("env")
            self.stdout = _StreamLike(b'{"ok": 1}')
            self.stderr = _StreamLike(b"")
            self.returncode = 0
            self.pid = 12345

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return ('{"ok": 1}', "")

        def kill(self) -> None:
            pass

        def wait(self) -> int:
            return 0

    monkeypatch.setattr("whilly.prd_generator.subprocess.Popen", FakePopen)

    from whilly.prd_generator import _call_claude

    _call_claude(prompt="any", model="m")

    env = captured["env"]
    assert env is not None
    assert "HTTPS_PROXY" not in env


class _StreamLike:
    """Minimal file-like for the heartbeat / drain threads in _call_claude.

    The production code reads stderr line-by-line and stdout via
    communicate(); we hand back tiny stand-ins that don't block.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload.decode("utf-8")
        self._consumed = False

    def __iter__(self) -> Any:
        return iter(())  # no stderr lines

    def read(self) -> str:
        if self._consumed:
            return ""
        self._consumed = True
        return self._payload
