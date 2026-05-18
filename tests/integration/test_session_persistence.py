"""Integration test for ``WHILLY_DASHBOARD_TOKEN_SECRET`` persistence.

PRD-post-auth-hardening §Epic B, Item 5. Verifies that:

1. With a FIXED ``WHILLY_DASHBOARD_TOKEN_SECRET``, a ``whilly_session``
   cookie minted by one server lifetime is accepted by a subsequent
   server lifetime — proves the secret survives restarts.
2. WITHOUT the env var (ephemeral secret regenerated at startup), the
   same cookie is rejected after restart with 401.

This is a true integration test: it boots the FastAPI app in a
subprocess (``python -m whilly server``) so the cross-process secret
invariant is what's being measured. The asyncpg pool fixture from
:mod:`tests.conftest` provides a testcontainers Postgres so the server
has a real DB to bind against; Alembic migrations are pre-applied.

Marked ``@pytest.mark.integration`` — skipped by default in
``pytest -q``, runs in the dedicated integration job.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing

import httpx
import pytest

from tests.conftest import DOCKER_REQUIRED

pytestmark = [pytest.mark.integration, DOCKER_REQUIRED]

# Fixed secret used across the "persistence" branch — any 32-byte token is fine.
_FIXED_SECRET: str = "fixed-test-secret-32-bytes-paddxx"
# 32 bytes, urlsafe-base64-like alphabet so it's accepted by the verifier.
_DIFFERENT_SECRET: str = "different-secret-32-bytes-paddx!"

_BOOTSTRAP_USERNAME: str = "admin"
_BOOTSTRAP_PASSWORD: str = "admin"


def _find_free_port() -> int:
    """Pick an ephemeral TCP port the kernel is currently willing to give us."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    """Poll until ``host:port`` accepts TCP, or raise after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                s.settimeout(0.5)
                s.connect((host, port))
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.1)
    raise TimeoutError(f"server did not start listening on {host}:{port} within {timeout}s (last error: {last_exc!r})")


def _start_server(*, dsn: str, port: int, secret: str | None) -> subprocess.Popen[bytes]:
    """Launch ``python -m whilly server --port=PORT`` with the given env."""
    env = os.environ.copy()
    env["WHILLY_DATABASE_URL"] = dsn
    env["WHILLY_PROD_MODE"] = "false"  # loopback, no TLS
    # Ensure no leftover from the parent process — caller controls the secret.
    env.pop("WHILLY_DASHBOARD_TOKEN_SECRET", None)
    if secret is not None:
        env["WHILLY_DASHBOARD_TOKEN_SECRET"] = secret
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "whilly",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port("127.0.0.1", port, timeout=20.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        raise
    return proc


def _stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def server_runner(
    postgres_dsn: str,
) -> Iterator[
    callable  # type: ignore[type-arg]
]:
    """Yield a builder that spawns a server bound to the testcontainers DSN."""
    procs: list[subprocess.Popen[bytes]] = []

    def _spawn(*, secret: str | None) -> tuple[int, subprocess.Popen[bytes]]:
        port = _find_free_port()
        proc = _start_server(dsn=postgres_dsn, port=port, secret=secret)
        procs.append(proc)
        return port, proc

    try:
        yield _spawn
    finally:
        for p in procs:
            if p.poll() is None:
                _stop_server(p)


def _login_and_capture_cookie(port: int) -> str:
    """POST admin/admin to /auth/login, return the whilly_session cookie value."""
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=False, timeout=10.0) as client:
        resp = client.post(
            "/auth/login",
            data={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )
    # The login route returns 303 on success and sets the cookie. The
    # must_change_password flag is True for the bootstrap row, but the
    # cookie is set on the 303 response regardless.
    assert resp.status_code in (200, 303), f"unexpected login status {resp.status_code}: {resp.text[:200]}"
    cookie = resp.cookies.get("whilly_session")
    assert cookie, f"no whilly_session cookie set; cookies: {dict(resp.cookies)}"
    return cookie


# ─── Branch 1: fixed secret survives restart ────────────────────────────────


def test_session_cookie_survives_restart_with_fixed_secret(
    server_runner: callable,  # type: ignore[type-arg]
) -> None:
    """With WHILLY_DASHBOARD_TOKEN_SECRET set, the cookie minted by
    lifetime A is accepted by lifetime B.
    """
    # Lifetime A: login, capture cookie, stop.
    port_a, proc_a = server_runner(secret=_FIXED_SECRET)
    cookie = _login_and_capture_cookie(port_a)
    _stop_server(proc_a)

    # Lifetime B: same secret, NEW port (so we don't race the kernel for the old one).
    port_b, _ = server_runner(secret=_FIXED_SECRET)
    with httpx.Client(base_url=f"http://127.0.0.1:{port_b}", timeout=10.0) as client:
        resp = client.get("/me", cookies={"whilly_session": cookie})
    # /me is a session-only JSON endpoint — 200 means the cookie was accepted.
    assert resp.status_code == 200, f"cookie was rejected by lifetime B; body: {resp.text[:200]}"


# ─── Branch 2: ephemeral secret rejects across restart ──────────────────────


def test_session_cookie_rejected_after_restart_without_fixed_secret(
    server_runner: callable,  # type: ignore[type-arg]
) -> None:
    """Without WHILLY_DASHBOARD_TOKEN_SECRET, each server picks a new
    random secret at startup. A cookie minted by lifetime A is rejected
    by lifetime B with 401.
    """
    port_a, proc_a = server_runner(secret=None)
    cookie = _login_and_capture_cookie(port_a)
    _stop_server(proc_a)

    port_b, _ = server_runner(secret=None)
    with httpx.Client(base_url=f"http://127.0.0.1:{port_b}", timeout=10.0) as client:
        resp = client.get("/me", cookies={"whilly_session": cookie})
    assert resp.status_code == 401, (
        f"cookie was unexpectedly accepted across ephemeral-secret restart "
        f"(status={resp.status_code}, body={resp.text[:200]})"
    )


# ─── Branch 3 (bonus): same secret across lifetimes A and B, but different
#     secret in lifetime C → C rejects the cookie even though A and B accept ─


def test_session_cookie_rejected_when_secret_changes(
    server_runner: callable,  # type: ignore[type-arg]
) -> None:
    """Defence-in-depth: rotating the secret invalidates outstanding cookies."""
    port_a, proc_a = server_runner(secret=_FIXED_SECRET)
    cookie = _login_and_capture_cookie(port_a)
    _stop_server(proc_a)

    port_c, _ = server_runner(secret=_DIFFERENT_SECRET)
    with httpx.Client(base_url=f"http://127.0.0.1:{port_c}", timeout=10.0) as client:
        resp = client.get("/me", cookies={"whilly_session": cookie})
    assert resp.status_code == 401
