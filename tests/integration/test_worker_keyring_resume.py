"""Keyring-resume read path for ``whilly-worker`` (M2, VAL-M1-DEMO-009).

Locks in the M1-deferred contract that ``whilly-worker`` can recover a
bearer previously stored by ``whilly worker connect`` (or any caller of
:func:`whilly.secrets.store_worker_credential`) without the operator
re-supplying ``--token`` / ``WHILLY_WORKER_TOKEN`` after a reboot or
re-login.

Why an integration test (and not a pure unit test):

* The store-then-read cycle uses the file-fallback backend so the test
  runs identically on darwin / Linux CI / headless Docker — no live OS
  keychain dependency. ``XDG_CONFIG_HOME`` is patched to a fresh tmp
  dir so the test never touches the developer's ``~/.config/whilly``.
* The bearer is minted by a real ``POST /workers/register`` round-trip
  against a uvicorn-hosted FastAPI control-plane (testcontainers
  Postgres in the loop). That guarantees the persisted bearer is the
  same shape an operator would see — no synthetic shortcuts that could
  mask a regression in the auth surface.
* The follow-up ``whilly-worker`` invocation patches ``_async_worker``
  to capture the kwargs the CLI hands the loop. Asserting on the
  captured ``token`` proves the bearer flowed through the keychain
  read path; running the real long-poll in unit-friendly time would
  require a fake runner *and* a seeded task and is already covered by
  :mod:`tests.integration.test_phase5_remote`.

Cleanup: ``patched_xdg`` (matches the fixture in
:mod:`tests.integration.test_cli_worker_connect_e2e`) sandboxes the
fallback file under ``tmp_path``; pytest tears the directory down
automatically. We additionally unlink the credential file so a future
test running in the same process never sees a stale entry.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import asyncpg
import pytest
import uvicorn
from httpx import AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly import secrets as whilly_secrets
from whilly.adapters.transport.server import create_app
from whilly.cli import worker as cli_worker
from whilly.cli.worker import (
    BOOTSTRAP_TOKEN_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    PLAN_ID_ENV,
    WORKER_TOKEN_ENV,
    run_worker_command,
)
from whilly.worker.remote import RemoteWorkerStats

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "TEST_BOOTSTRAP_RESUME"
_PLAN_ID = "demo-keyring-resume"
_HOSTNAME = "host-keyring-resume"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_started(server: uvicorn.Server, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not server.started:
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"uvicorn did not start within {timeout}s")
        await asyncio.sleep(0.05)


@pytest.fixture
async def control_plane(db_pool: asyncpg.Pool) -> AsyncIterator[str]:
    """Boot a uvicorn FastAPI control-plane on a free loopback port; yield URL."""
    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=2.0,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="keyring-resume-uvicorn")
    try:
        await _wait_until_started(server)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)
        if not server_task.done():
            server_task.cancel()
            with suppress(asyncio.CancelledError, BaseException):
                await server_task


@pytest.fixture
def patched_xdg(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    cfg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return str(cfg)


@pytest.fixture
def force_file_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the fallback-file path so the test never touches the real keychain."""

    def _raise(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated headless keyring backend")

    monkeypatch.setattr(whilly_secrets, "_set_keyring_password", _raise)

    def _no_keyring(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(whilly_secrets, "_get_keyring_password", _no_keyring)


async def _register_worker(control_plane_url: str) -> tuple[str, str]:
    """POST /workers/register → return ``(worker_id, plaintext_bearer)``."""
    async with AsyncClient(base_url=control_plane_url) as client:
        resp = await client.post(
            "/workers/register",
            json={"hostname": _HOSTNAME},
            headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
        )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    return body["worker_id"], body["token"]


async def test_worker_reads_bearer_from_keychain_and_proceeds(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    force_file_fallback: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store-then-read cycle: bearer in keychain → ``whilly-worker`` reaches the loop.

    Mirrors the operator flow:

    1. ``whilly worker connect`` (or any other caller) registers and
       writes the bearer to the keychain.
    2. The host reboots / re-logs in; ``--token`` and
       ``WHILLY_WORKER_TOKEN`` are not provided.
    3. ``whilly-worker`` reads the bearer from the keychain and
       proceeds to long-poll under the registered identity.

    This test exercises (1)–(3) end-to-end with the file fallback as
    the storage backend (so the assertion is portable across CI hosts).
    """
    worker_id, bearer = await _register_worker(control_plane)
    backend = whilly_secrets.store_worker_credential(control_plane, bearer)
    assert backend == "file", f"expected file fallback, got {backend!r}"

    cred_path = whilly_secrets.credentials_file_path()
    try:
        assert cred_path.is_file(), f"fallback file not written at {cred_path}"
        # Sanity: load_worker_credential round-trips the bearer.
        assert whilly_secrets.load_worker_credential(control_plane) == bearer
        # And so does the M2 alias used by the worker entry point.
        assert whilly_secrets.fetch_worker_credential(control_plane, _PLAN_ID) == bearer

        # No --token, no env: the keychain read path must satisfy the
        # required input. Patch _async_worker so we can assert on the
        # token that flowed through without spinning up the actual loop.
        monkeypatch.delenv(WORKER_TOKEN_ENV, raising=False)
        monkeypatch.delenv(BOOTSTRAP_TOKEN_ENV, raising=False)
        monkeypatch.delenv(PLAN_ID_ENV, raising=False)

        captured: list[dict[str, Any]] = []

        async def _fake_async_worker(**kwargs: Any) -> RemoteWorkerStats:
            captured.append(kwargs)
            return RemoteWorkerStats()

        monkeypatch.setattr(cli_worker, "_async_worker", _fake_async_worker)

        code = await asyncio.to_thread(
            run_worker_command,
            [
                "--connect",
                control_plane,
                "--plan",
                _PLAN_ID,
                "--worker-id",
                worker_id,
                "--max-iterations",
                "1",
            ],
        )
        assert code == EXIT_OK, "worker should exit cleanly when bearer comes from keychain"
        assert len(captured) == 1
        assert captured[0]["token"] == bearer, (
            "the bearer handed to _async_worker must be the one persisted by store_worker_credential"
        )
        assert captured[0]["connect_url"] == control_plane
        assert captured[0]["plan_id"] == _PLAN_ID
        assert captured[0]["worker_id"] == worker_id
    finally:
        with suppress(FileNotFoundError):
            cred_path.unlink()


async def test_keychain_bearer_authenticates_real_long_poll(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    force_file_fallback: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the keychain-resolved bearer authenticates a real ``/tasks/claim`` HTTP call.

    Stronger evidence than the kwargs-capture test: the bearer that
    ``whilly-worker`` pulls from the keychain is actually accepted by
    the live control-plane on the same RPC the production loop uses
    (``POST /tasks/claim``), proving the read path produces a usable
    bearer (not e.g. a malformed string from a corrupt fallback file).
    """
    worker_id, bearer = await _register_worker(control_plane)
    whilly_secrets.store_worker_credential(control_plane, bearer)

    cred_path = whilly_secrets.credentials_file_path()
    try:
        # Replay what the worker would do on startup: read from keychain,
        # POST /tasks/claim with the resolved bearer.
        resolved = whilly_secrets.fetch_worker_credential(control_plane, _PLAN_ID)
        assert resolved == bearer

        async with AsyncClient(base_url=control_plane) as client:
            claim_resp = await client.post(
                "/tasks/claim",
                json={"worker_id": worker_id, "plan_id": _PLAN_ID},
                headers={"Authorization": f"Bearer {resolved}"},
                timeout=10.0,
            )
        # 204 (no PENDING task) is the expected outcome — the queue is
        # empty. 200 would indicate a task came back. 401/403 would
        # mean the keychain bearer didn't authenticate.
        assert claim_resp.status_code in (200, 204), (
            f"keychain bearer must authenticate /tasks/claim; got {claim_resp.status_code}: {claim_resp.text}"
        )
    finally:
        with suppress(FileNotFoundError):
            cred_path.unlink()


async def test_missing_token_with_empty_keychain_emits_canonical_diagnostic(
    patched_xdg: str,
    force_file_fallback: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No bearer anywhere → ``whilly-worker`` exits 2 with the M2 diagnostic.

    The error message is the canonical "operator has no bearer" copy
    pinned by the feature spec — it must mention all three sources
    (``--token``, ``WHILLY_WORKER_TOKEN``, and the keychain via
    ``whilly worker connect``) so the operator can self-recover.
    """
    monkeypatch.delenv(WORKER_TOKEN_ENV, raising=False)
    monkeypatch.delenv(BOOTSTRAP_TOKEN_ENV, raising=False)

    cred_path = whilly_secrets.credentials_file_path()
    assert not cred_path.exists(), "fallback file should not exist on a fresh XDG sandbox"

    code = await asyncio.to_thread(
        run_worker_command,
        ["--connect", "http://127.0.0.1:9999", "--plan", "P-empty"],
    )
    assert code == EXIT_ENVIRONMENT_ERROR
    err = capsys.readouterr().err
    assert "--token" in err
    assert WORKER_TOKEN_ENV in err
    assert "whilly worker connect" in err
    assert "keychain" in err.lower()
