"""Shared fixtures for ``tests/unit/``.

The proxy/probe test suites bind real localhost sockets to validate the
TCP-handshake probe end-to-end (mocking would bypass the very
``socket.create_connection`` we want to exercise). The fixtures are
identical across test_claude_proxy_probe.py and test_cli_init_proxy.py,
so they live here for pytest auto-discovery rather than being copied
file-to-file.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_os_environ() -> Iterator[None]:
    """Snapshot ``os.environ`` before every unit test and restore it after.

    Several CLI composition tests (``test_cli_run``, ``test_run_slack_hook``,
    ``test_cli_worker``) exercise the *real* ``run_run_command`` →
    :func:`whilly.config.load_layered` → :func:`whilly.config.load_dotenv`
    chain, which writes the developer's local ``.env`` straight into
    ``os.environ`` — bypassing :class:`pytest.MonkeyPatch`'s bookkeeping.
    Without this guard a local ``.env`` containing ``JIRA_VERIFY_SSL=false``
    leaks into later Jira tests (``test_qa_release_collector``,
    ``test_prompt_sanitizer_wiring``), flipping ``JiraAuth.verify_ssl`` so
    ``_jira_get`` takes the ``urlopen(..., context=ctx)`` branch and their
    ``fake_urlopen`` stub — which has no ``context`` parameter — raises
    ``TypeError``. CI never had a ``.env`` so the breakage only reproduced
    locally (the "3 pre-existing test-order flakes" in the 2026-05-19 handoff).

    A full snapshot is required rather than a tracked allow-list because
    ``load_dotenv`` injects whatever keys the operator's ``.env`` holds.
    This fixture is dependency-free so it initialises before every other
    autouse/explicit fixture and finalises last — i.e. it is the outermost
    env guard, restoring true pre-test state regardless of what inner
    fixtures or production code mutated.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _reset_worker_insecure_warning_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    from whilly.cli import worker as _cli_worker

    monkeypatch.setattr(_cli_worker, "_INSECURE_WARNING_EMITTED", False, raising=True)


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
