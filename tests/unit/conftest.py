"""Shared fixtures for ``tests/unit/``.

The proxy/probe test suites bind real localhost sockets to validate the
TCP-handshake probe end-to-end (mocking would bypass the very
``socket.create_connection`` we want to exercise). The fixtures are
identical across test_claude_proxy_probe.py and test_cli_init_proxy.py,
so they live here for pytest auto-discovery rather than being copied
file-to-file.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest


@pytest.fixture
def listening_port() -> Iterator[int]:
    """Bind a real listener on an ephemeral port; yield it; close on teardown.

    ``backlog=1`` is plenty — the probe never ``accept()``s, it just
    completes the handshake. The yield/close pattern guarantees the
    socket is released even if the test raises mid-assertion.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
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
