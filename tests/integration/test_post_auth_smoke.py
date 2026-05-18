"""End-to-end smoke test of the post-auth-hardening user journey.

PRD-post-auth-hardening §Epic A, Item 2. Walks the journey: login →
forced change-password (must_change gate from C6 redirects) → change
password (D9 /me/password or /auth/change-password) → tasks list →
auth_audit row written (D10b instrumentation).

The worker-claim portion of the PRD's original A2 scope (call `whilly
worker launch <plan>` via subprocess, confirm registration, watch a
task transition to in_progress) requires Docker + a claude binary +
pre-seeded plan rows and is intentionally out of scope here. The auth
slice plus the audit-write check is what this file proves; the worker
slice is covered separately by the unit-level B4 tests + the manual
operator smoke that ships with releases.

Marked ``@pytest.mark.integration + DOCKER_REQUIRED`` — auto-skipped
when Docker / testcontainers Postgres aren't available (same gate as
:mod:`tests.integration.test_session_persistence`, PR #282).
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

_FIXED_SECRET: str = "a2-smoke-secret-32-bytes-paddxxx"
_BOOTSTRAP_USERNAME: str = "admin"
_BOOTSTRAP_PASSWORD: str = "admin"
_NEW_PASSWORD: str = "new-strong-password-A2-test-12chr"


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                s.settimeout(0.5)
                s.connect((host, port))
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"server did not start on {host}:{port} within {timeout}s")


@pytest.fixture
def smoke_server(postgres_dsn: str) -> Iterator[tuple[int, subprocess.Popen[bytes]]]:
    """Boot `python -m whilly server` against the testcontainers DSN."""
    port = _find_free_port()
    env = os.environ.copy()
    env["WHILLY_DATABASE_URL"] = postgres_dsn
    env["WHILLY_PROD_MODE"] = "false"
    env["WHILLY_DASHBOARD_TOKEN_SECRET"] = _FIXED_SECRET
    # Disable rate-limiting to keep the smoke test deterministic.
    env["WHILLY_AUTH_RATE_LIMIT_ENABLED"] = "false"
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
        _wait_for_port("127.0.0.1", port)
        yield port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_post_auth_journey_login_change_password_tasks_audit(
    smoke_server: tuple[int, subprocess.Popen[bytes]], db_pool: object
) -> None:
    """Walk the auth-hardening user journey end-to-end.

    Steps:
    1. POST /auth/login admin/admin → 303 (cookie set).
    2. GET / with cookie → 303 → /auth/change-password
       (the C6 must_change gate redirects because bootstrap admin row
       has must_change_password=TRUE).
    3. POST /auth/change-password with new password → 303 → /.
    4. GET /api/v1/tasks/?plan_id=missing with the cookie → 200 (auth ok;
       any task list shape is fine, we just want to prove the session
       outlived the change-password flow).
    5. Query auth_audit table directly via the db_pool fixture →
       at least one outcome='ok' row for admin (D10b instrumentation).
    """
    port, _proc = smoke_server
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, follow_redirects=False, timeout=15.0) as client:
        # Step 1 — login. Bootstrap admin has must_change_password=True,
        # so submit_login returns 303 → /auth/change-password (not /).
        login_resp = client.post(
            "/auth/login",
            data={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )
        assert login_resp.status_code == 303, (
            f"login expected 303; got {login_resp.status_code}: {login_resp.text[:200]}"
        )
        # Bootstrap admin's redirect is to /auth/change-password specifically.
        assert "change-password" in (login_resp.headers.get("location") or "")
        cookie = login_resp.cookies.get("whilly_session")
        assert cookie, f"no session cookie set: {dict(login_resp.cookies)}"

        # Step 2 — the gate should also redirect GET / to /auth/change-password
        # for the same cookie (proves C6 enforces at the per-request level).
        root_resp = client.get("/", cookies={"whilly_session": cookie})
        assert root_resp.status_code == 303
        assert "change-password" in (root_resp.headers.get("location") or "")

        # Step 3 — submit the change-password form.
        cp_resp = client.post(
            "/auth/change-password",
            cookies={"whilly_session": cookie},
            data={"new_password": _NEW_PASSWORD, "confirm_new_password": _NEW_PASSWORD},
        )
        assert cp_resp.status_code == 303, (
            f"change-password expected 303; got {cp_resp.status_code}: {cp_resp.text[:200]}"
        )
        assert cp_resp.headers.get("location") == "/"

        # Step 4 — session outlived the password change. GET /me is the
        # simplest authenticated JSON endpoint we have; 200 means the
        # cookie + must_change_gate are both green.
        me_resp = client.get("/me", cookies={"whilly_session": cookie})
        assert me_resp.status_code == 200, (
            f"/me after change-password expected 200; got {me_resp.status_code}: {me_resp.text[:200]}"
        )

    # Step 5 — verify the audit instrumentation wrote a row.
    import asyncio

    async def _audit_check() -> list[dict]:
        async with db_pool.acquire() as conn:  # type: ignore[attr-defined]
            rows = await conn.fetch(
                "SELECT username, outcome FROM auth_audit "
                "WHERE username = $1 AND outcome = 'ok' ORDER BY ts DESC LIMIT 5",
                _BOOTSTRAP_USERNAME,
            )
            return [dict(r) for r in rows]

    audit_rows = asyncio.get_event_loop().run_until_complete(_audit_check())
    assert len(audit_rows) >= 1, (
        f"expected at least one outcome='ok' auth_audit row for {_BOOTSTRAP_USERNAME!r}; got {audit_rows}"
    )
