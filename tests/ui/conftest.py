"""Pytest fixtures for the WUI browser test suite.

Playwright drives a real Chromium against a live uvicorn process; the
in-process httpx/ASGITransport pattern used by the backend test suite does
not work here because the browser needs a real TCP socket. Each test class
gets its own random port, a temp event-log file (so magic-link parsing is
hermetic), and a clean database via the shared ``postgres_dsn`` fixture
from :mod:`tests.conftest`.

Locator priority (recorded in ``feedback_ui_test_ids.md``):

1. ``page.get_by_role(role, name=...)`` for interactive elements.
2. ``page.get_by_label(text)`` for form fields.
3. ``page.get_by_text(text)`` for static text.
4. ``page.get_by_test_id(id)`` only as a fallback (repeating rows,
   container scopes, ambiguous duplicates).

All tests are skipped automatically when Docker is unreachable (the
session-scoped ``postgres_dsn`` is testcontainers-backed).
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_BIN = Path(sys.executable)
SERVER_BOOT_TIMEOUT_SECONDS = 15.0


@pytest.fixture(scope="session")
def chromium_available() -> bool:
    """Skip the UI suite if chromium isn't installed locally."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            pw.chromium.executable_path  # noqa: B018 — touch the attr
    except Exception:
        return False
    return True


@pytest.fixture(scope="session", autouse=True)
def _ui_suite_environment_check(chromium_available: bool) -> None:
    if not chromium_available:
        pytest.skip("pytest-playwright + chromium not installed; install via `playwright install chromium`")


def _pick_free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    # trust_env=False so HTTP_PROXY / HTTPS_PROXY inherited from the operator
    # shell do not redirect loopback health checks through a corporate proxy.
    with httpx.Client(trust_env=False, timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/health")
                if response.status_code == 200:
                    return
            except Exception as exc:
                last_err = exc
            time.sleep(0.2)
    raise RuntimeError(f"uvicorn at {base_url} did not become healthy in {timeout}s: {last_err}")


@pytest.fixture
def event_log_path(tmp_path: Path) -> Path:
    """Per-test event-log path so the magic-link helper reads a hermetic file."""
    log = tmp_path / "whilly_events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    return log


@pytest.fixture
def live_server(postgres_dsn: str, event_log_path: Path, tmp_path: Path) -> Iterator[str]:
    """Launch ``whilly server`` against the testcontainers Postgres.

    Yields the base URL ``http://127.0.0.1:<port>``. The process is
    sent SIGTERM at teardown and joined with a short grace window. The
    server's stdout+stderr are tee'd to a per-test logfile so failures
    surface a useful message instead of "did not become healthy".
    """
    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "uvicorn.log"
    env = {
        **os.environ,
        "WHILLY_DATABASE_URL": postgres_dsn,
        "WHILLY_WORKER_BOOTSTRAP_TOKEN": "ui-test-bootstrap",
        "WHILLY_EVENT_LOG_PATH": str(event_log_path),
        "WHILLY_CSRF_ORIGIN_ALLOWLIST": f"http://127.0.0.1:{port},http://localhost:{port}",
        "WHILLY_SESSION_COOKIE_SECURE": "false",
        "PYTHONPATH": str(REPO_ROOT),
    }
    # Strip outbound proxy vars — the browser hits 127.0.0.1 directly, but
    # the python httpx health-check inherits these otherwise.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(var, None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = env["NO_PROXY"]

    log_fh = log_path.open("wb")
    proc = subprocess.Popen(  # noqa: S603 — args fully controlled by this fixture
        [str(PYTHON_BIN), "-m", "whilly.cli", "server", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    try:
        try:
            _wait_for_health(base_url, SERVER_BOOT_TIMEOUT_SECONDS)
        except RuntimeError:
            log_fh.flush()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"uvicorn at {base_url} did not become healthy.\nLog tail:\n{tail}") from None
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        log_fh.close()


def _psql_args(dsn: str) -> tuple[list[str], dict[str, str]]:
    """Convert a Postgres URL into ``psql`` argv + env (with PGPASSWORD)."""
    parsed = urlparse(dsn)
    args = [
        "psql",
        "-h",
        parsed.hostname or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "postgres",
        "-d",
        (parsed.path or "/postgres").lstrip("/"),
        "-v",
        "ON_ERROR_STOP=1",
        "-X",
        "-q",
        "-A",
        "-t",
    ]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return args, env


def _psql_run(dsn: str, sql: str) -> str:
    args, env = _psql_args(dsn)
    args.extend(["-c", sql])
    result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


@pytest.fixture(autouse=True)
def _truncate_auth_state(postgres_dsn: str) -> Iterator[None]:
    """Empty WUI auth state before every UI test (sync — Playwright owns the loop)."""
    _psql_run(
        postgres_dsn,
        "TRUNCATE TABLE magic_links, sessions, events, tasks, pull_requests, plans, workers RESTART IDENTITY CASCADE",
    )
    yield


@pytest.fixture
def magic_link_reader(event_log_path: Path):
    """Return a callable that reads the latest magic-link URL for ``email``."""

    def _read(email: str) -> str:
        import json

        text = event_log_path.read_text(encoding="utf-8").strip()
        if not text:
            raise AssertionError(f"event log {event_log_path} is empty — no magic link issued")
        for line in reversed(text.splitlines()):
            event = json.loads(line)
            if event.get("event_type") == "auth.magic_link.issued" and event.get("email") == email:
                return event["magic_link_url"]
        raise AssertionError(f"no auth.magic_link.issued event for {email!r} in {event_log_path}")

    return _read


@pytest.fixture
def signed_in_page(page, live_server: str):
    """Browser page already logged in as the bootstrap ``admin/admin``.

    Migration 020 seeds the admin user; the autouse truncate fixture does NOT
    include the ``users`` table, so this row survives between tests. We log
    in through the canonical ``/login`` username+password form so every UI
    test exercises the production auth path (not the magic-link fallback at
    ``/login/magic``).
    """
    page.goto(f"{live_server}/login")
    page.get_by_label("username").fill("admin")
    page.get_by_label("password").fill("admin")
    page.get_by_role("button", name="[ sign in ]").click()
    # Successful auth = 303 to /, page header shows "Signed in as ...".
    page.get_by_text("Signed in as").wait_for()
    return page


def _sql_str(value: str | None) -> str:
    """Quote a string for safe inline SQL (psql, not parameterised)."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


@pytest.fixture
def insert_task(postgres_dsn: str):
    """Sync helper to seed a task row directly for UI tests that need state."""

    def _insert(
        *,
        plan_id: str,
        task_id: str,
        status: str = "PENDING",
        priority: str = "medium",
        description: str = "",
        version: int = 0,
        claimed_by: str | None = None,
    ) -> None:
        # ck_tasks_claim_pair_consistent requires claimed_by + claimed_at
        # to be both set or both NULL. When the caller seeds a claimed
        # task we generate claimed_at = NOW() to satisfy the check.
        claimed_at_sql = "NOW()" if claimed_by else "NULL"
        sql = (
            "INSERT INTO tasks (id, plan_id, status, priority, description, version, "
            "key_files, dependencies, acceptance_criteria, test_steps, claimed_by, "
            "claimed_at) VALUES ("
            f"{_sql_str(task_id)}, {_sql_str(plan_id)}, {_sql_str(status)}, "
            f"{_sql_str(priority)}, {_sql_str(description)}, {int(version)}, "
            f"'{{}}', '{{}}', '{{}}', '{{}}', {_sql_str(claimed_by)}, {claimed_at_sql})"
        )
        _psql_run(postgres_dsn, sql)

    return _insert


@pytest.fixture
def insert_plan(postgres_dsn: str):
    """Sync helper to seed a plan row directly for UI tests."""

    def _insert(*, plan_id: str, name: str = "", budget_usd: float | None = None) -> None:
        budget_sql = "NULL" if budget_usd is None else str(float(budget_usd))
        sql = (
            "INSERT INTO plans (id, name, prd_file, budget_usd, archived_at, last_event_at) "
            f"VALUES ({_sql_str(plan_id)}, {_sql_str(name or plan_id)}, '', "
            f"{budget_sql}, NULL, NOW())"
        )
        _psql_run(postgres_dsn, sql)

    return _insert


# ── Browser config helpers ──────────────────────────────────────────────────


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Make the headed window large enough that all surface tabs are visible."""
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}
