"""Magic-link reuse window behavioural test (SC-2.3, Block 6).

PRD-wui-multi-plan v2 §SC-2.3 — submitting ``POST /auth/login`` twice
for the same email within ``REUSE_WINDOW_SECONDS`` must produce
exactly **one** ``auth.magic_link.issued`` event in ``whilly_events.jsonl``.
After consuming the link, a fresh submission must mint a new link
(event count rises to 2).

Why a per-test event-log path?
    The event log defaults to ``whilly_logs/whilly_events.jsonl`` —
    a shared file across the whole repo. Running this test in
    parallel with anything else would either contaminate the
    assertions (extra events from sibling tests) or get
    contaminated by them. We point ``WHILLY_EVENT_LOG_PATH`` at a
    tempdir per test so the count is isolated.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.api import auth_tokens, sessions
from whilly.api.auth_routes import EVENT_LOG_PATH_ENV, build_auth_router
from whilly.api.csrf import WhillySessionCSRFMiddleware

pytestmark = DOCKER_REQUIRED

_TEST_SECRET: bytes = b"reuse-test-secret-32-bytes-long!"
_TEST_EMAIL: str = "reuse@example.com"


@pytest.fixture(autouse=True)
async def _truncate_auth_tables(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Reset ``magic_links`` and ``sessions`` between tests.

    The session-scoped DB conftest truncates ``events / tasks / plans /
    workers / bootstrap_tokens / control_state`` but not the new v2
    auth tables. Without this autouse hook, a row left over by a
    sibling test would short-circuit the route layer's "mint a fresh
    link" branch via the reuse-window match — silently breaking the
    SC-2.3 assertion below.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE magic_links, sessions")
    yield


@pytest.fixture
def isolated_event_log() -> Iterator[Path]:
    """Point ``WHILLY_EVENT_LOG_PATH`` at a per-test file under tempdir."""
    tmpdir = Path(tempfile.mkdtemp(prefix="whilly-reuse-test-"))
    log_path = tmpdir / "events.jsonl"
    prior = os.environ.get(EVENT_LOG_PATH_ENV)
    os.environ[EVENT_LOG_PATH_ENV] = str(log_path)
    try:
        yield log_path
    finally:
        if prior is None:
            os.environ.pop(EVENT_LOG_PATH_ENV, None)
        else:
            os.environ[EVENT_LOG_PATH_ENV] = prior
        # Best-effort cleanup of the tempdir.
        try:
            if log_path.exists():
                log_path.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


@pytest.fixture
async def reuse_app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    """Minimal app with auth router + CSRF (CSRF is no-op for /auth/login)."""
    app = FastAPI()
    app.add_middleware(WhillySessionCSRFMiddleware, allowlist=["http://127.0.0.1:8000"])
    app.include_router(build_auth_router(pool=db_pool, secret=_TEST_SECRET))
    yield app


@pytest.fixture
async def reuse_client(reuse_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=reuse_app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        yield client


def _count_issued_events(log_path: Path, email: str) -> int:
    """Count ``auth.magic_link.issued`` lines in the event log for ``email``."""
    if not log_path.exists():
        return 0
    count = 0
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") == "auth.magic_link.issued" and event.get("email") == email:
            count += 1
    return count


def _extract_latest_magic_token(log_path: Path, email: str) -> str:
    """Pull the ``token=<...>`` value out of the most recent issued event."""
    assert log_path.exists()
    url: str | None = None
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("event_type") == "auth.magic_link.issued" and event.get("email") == email:
            url = event["magic_link_url"]
    assert url is not None, "no auth.magic_link.issued event for email"
    # Token is the value of the ?token= query string parameter.
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    token_values = parse_qs(parsed.query).get("token") or []
    assert token_values, f"no token in magic URL: {url}"
    return token_values[0]


@pytest.mark.asyncio
async def test_repeated_login_within_reuse_window_emits_one_event(
    reuse_client: AsyncClient,
    isolated_event_log: Path,
) -> None:
    """Two POST /auth/login for the same email → exactly one issued event.

    The route layer reuses the existing unconsumed magic-link row when
    it falls inside ``REUSE_WINDOW_SECONDS`` (= 5 minutes by default).
    The audit event is appended only on the *first* mint.
    """
    response_one = await reuse_client.post("/auth/login", data={"email": _TEST_EMAIL})
    assert response_one.status_code == 200, response_one.text
    response_two = await reuse_client.post("/auth/login", data={"email": _TEST_EMAIL})
    assert response_two.status_code == 200, response_two.text

    issued = _count_issued_events(isolated_event_log, _TEST_EMAIL)
    assert issued == 1, f"expected exactly one issued event for repeat-within-window, got {issued}"


@pytest.mark.asyncio
async def test_fresh_login_after_consume_mints_new_event(
    reuse_client: AsyncClient,
    isolated_event_log: Path,
    db_pool: asyncpg.Pool,
) -> None:
    """After consuming the link, a fresh POST /auth/login mints a new link.

    Total ``auth.magic_link.issued`` events for the email rises to 2 —
    confirming the reuse logic does not silently swallow legitimate
    re-issuance once the prior link is consumed.
    """
    # First mint.
    response_one = await reuse_client.post("/auth/login", data={"email": _TEST_EMAIL})
    assert response_one.status_code == 200, response_one.text
    assert _count_issued_events(isolated_event_log, _TEST_EMAIL) == 1

    # Consume the link via the repository (skipping /auth/magic so the
    # test stays focused on the reuse invariant — /auth/magic is
    # exercised by the auth-matrix tests).
    raw_token = _extract_latest_magic_token(isolated_event_log, _TEST_EMAIL)
    token_hash = auth_tokens.hash_token(raw_token)
    consumed = await sessions.consume_magic_link(db_pool, token_hash=token_hash)
    assert consumed is not None, "magic link should be consumable on first read"

    # Second mint after consume — the partial unique index slot is now
    # free (consumed rows are excluded from the predicate), so this
    # must INSERT a fresh row and emit a new event.
    response_two = await reuse_client.post("/auth/login", data={"email": _TEST_EMAIL})
    assert response_two.status_code == 200, response_two.text

    issued = _count_issued_events(isolated_event_log, _TEST_EMAIL)
    assert issued == 2, f"expected 2 issued events after consume + re-login, got {issued}"
