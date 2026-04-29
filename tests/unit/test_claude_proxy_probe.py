"""Unit tests for the TCP-handshake probe (TASK-109-2).

Covers :func:`whilly.adapters.runner.proxy.probe_proxy_or_raise`:

* Happy path against a real (in-test) listening socket on an
  ephemeral port.
* Fail paths: refused connection, unparseable URL, missing port +
  unknown scheme.
* Diagnostic message contains the SSH-tunnel hint that the PRD
  requires.

The fake server is a `socket.socket` bound to ``127.0.0.1:0`` (kernel
picks the port). Two reasons for using a real socket rather than a
mock:

* The probe does ``socket.create_connection``; mocking that would
  bypass the actual code path we want to validate.
* Localhost TCP handshake takes ~0.1ms — adds nothing to test time.

PRD: ``docs/PRD-v41-claude-proxy.md`` FR-3 / SC-3.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from whilly.adapters.runner.proxy import probe_proxy_or_raise


@pytest.fixture
def listening_port() -> Iterator[int]:
    """Bind a real listener on an ephemeral port; yield it; close on teardown.

    ``backlog=1`` means a single pending connection slot which is all the
    probe ever needs (it doesn't ``accept()``, just connects). The yield/
    close pattern guarantees the socket goes away even if the test
    raises mid-assertion.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        port = sock.getsockname()[1]
        yield port
    finally:
        sock.close()


@pytest.fixture
def closed_port() -> int:
    """Return a port that is *not* listening.

    Bind + close releases the port; the kernel won't immediately reuse
    it for a competing listener within the test, so a probe against
    this port reliably gets ``ConnectionRefusedError``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return port


# ─── happy path ────────────────────────────────────────────────────────────


def test_probe_succeeds_against_listening_port(listening_port: int) -> None:
    """Probe should return None silently when the port is up."""
    url = f"http://127.0.0.1:{listening_port}"
    # Returns None on success; assert no exception.
    assert probe_proxy_or_raise(url) is None


def test_probe_uses_explicit_port_over_scheme_default(listening_port: int) -> None:
    """Even with `https://` scheme, an explicit port wins over 443 default."""
    url = f"https://127.0.0.1:{listening_port}"
    assert probe_proxy_or_raise(url) is None


# ─── fail paths ────────────────────────────────────────────────────────────


def test_probe_raises_on_refused_connection(closed_port: int) -> None:
    """Closed port → RuntimeError with SSH-tunnel hint."""
    url = f"http://127.0.0.1:{closed_port}"

    with pytest.raises(RuntimeError) as exc_info:
        probe_proxy_or_raise(url, timeout=0.5)

    msg = str(exc_info.value)
    # PRD FR-3: error must name the URL and include the actionable hint.
    assert url in msg
    assert "ssh -fN -L" in msg
    assert "WHILLY_CLAUDE_PROXY_PROBE=0" in msg  # opt-out hint


def test_probe_raises_on_unparseable_url() -> None:
    """URL with no host → RuntimeError before any socket call."""
    with pytest.raises(RuntimeError) as exc_info:
        probe_proxy_or_raise("not-a-url", timeout=0.5)
    assert "cannot parse" in str(exc_info.value)


def test_probe_raises_on_missing_port_and_unknown_scheme() -> None:
    """``socks5://host`` without port — probe rejects, not a TCP issue."""
    with pytest.raises(RuntimeError) as exc_info:
        probe_proxy_or_raise("socks5://host", timeout=0.5)
    msg = str(exc_info.value)
    assert "no port" in msg
    assert "specify host:port explicitly" in msg


def test_probe_uses_default_http_port_when_missing() -> None:
    """``http://localhost`` without port → default 80 (which is closed in test).

    No happy-path version of this test (we'd have to bind to port 80,
    needs root). What we verify here is that the probe *tries* port 80
    (failure path uses default scheme→port mapping) instead of bailing
    on missing port.
    """
    with pytest.raises(RuntimeError) as exc_info:
        probe_proxy_or_raise("http://127.0.0.1", timeout=0.5)
    # Diagnostic must reference port 80 — the default we tried.
    msg = str(exc_info.value)
    assert "ssh -fN -L 80:" in msg


def test_probe_respects_timeout(closed_port: int) -> None:
    """A short timeout completes well within the test's wallclock budget.

    On a closed port the OS fails synchronously (sub-ms) so the timeout
    is never actually consumed; this test just pins that the function
    *accepts* a custom timeout argument and returns within reasonable
    time.
    """
    import time

    url = f"http://127.0.0.1:{closed_port}"
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        probe_proxy_or_raise(url, timeout=0.5)
    elapsed = time.monotonic() - t0
    # 0.5s timeout + safety margin; closed port should fail in ~ms.
    assert elapsed < 1.0
