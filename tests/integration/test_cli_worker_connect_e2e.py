"""Integration tests for ``whilly worker connect`` (M1, end-to-end over real HTTP).

Boots a real uvicorn-hosted FastAPI control-plane against testcontainers
Postgres, then drives :func:`whilly.cli.worker.run_connect_command` in
the same process (with ``os.execvp`` monkeypatched so the test runner
survives). Assertions cover:

- The ``POST /workers/register`` round-trip lands a row in the
  ``workers`` table with the configured hostname.
- Stdout has the canonical ``worker_id: ...\\ntoken: ...\\n`` shape with
  no banners between, pipeable via grep/awk (VAL-M1-CONNECT-007).
- The persisted bearer survives a follow-up
  :func:`whilly.secrets.load_worker_credential` lookup (round-trip via
  the file-fallback when keyring is forced to fail).
- The bearer that came back from the register call authenticates
  follow-up RPCs against the live server (not asserted by SQL alone —
  we use httpx to hit ``POST /workers/<id>/heartbeat`` with the bearer
  to prove end-to-end auth).
- ``--no-keychain`` does not write to the keyring or to the fallback
  file (zero side effects on the storage backend), but the bearer is
  still authentic.
- A wrong bootstrap token surfaces the live server's 401 as exit 1 + a
  diagnostic, with no row in the workers table.

Why a real uvicorn socket and not :class:`httpx.ASGITransport`?
    The CLI ultimately constructs its own :class:`RemoteWorkerClient`
    inside :func:`_async_register`, which opens an
    :class:`httpx.AsyncClient` against the operator-supplied URL. We
    can't slip a transport into that — the operator-facing surface is
    the URL string. A real socket on ``127.0.0.1`` keeps the test
    faithful to the production code path while staying in-process.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import suppress

import asyncpg
import pytest
import uvicorn
from httpx import AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly import secrets as whilly_secrets
from whilly.adapters.transport.server import create_app
from whilly.cli import worker as cli_worker
from whilly.cli.worker import EXIT_CONNECT_ERROR, EXIT_OK, run_connect_command

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "TEST_BOOTSTRAP_PLACEHOLDER"
_PLAN_ID = "demo-cli-connect"
_HOSTNAME = "host-cli-connect"


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


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
    server_task = asyncio.create_task(server.serve(), name="cli-connect-uvicorn")
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
    """Force the fallback-file path by making the keyring set raise."""

    def _raise(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated headless keyring backend")

    monkeypatch.setattr(whilly_secrets, "_set_keyring_password", _raise)


@pytest.fixture
def patch_execvp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Replace ``os.execvp`` so the connect call returns instead of replacing the test."""
    calls: list[tuple[str, list[str]]] = []

    def _fake(file: str, args: list[str]) -> None:
        calls.append((file, list(args)))

    monkeypatch.setattr(cli_worker.os, "execvp", _fake)
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_connect_against_live_server_persists_to_file_fallback(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    force_file_fallback: None,
    patch_execvp: list[tuple[str, list[str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``whilly worker connect <real-url>`` registers, persists, and execs.

    Forces the fallback file path so we can read the bearer back from
    disk — cheaper than mocking the keyring twice.
    """
    code = await asyncio.to_thread(
        run_connect_command,
        [
            control_plane,
            "--bootstrap-token",
            _BOOTSTRAP_TOKEN,
            "--plan",
            _PLAN_ID,
            "--hostname",
            _HOSTNAME,
        ],
    )
    assert code == EXIT_OK

    # Stdout shape: two `key: value` lines, no banner between.
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln]
    worker_id_line = next(ln for ln in lines if ln.startswith("worker_id: "))
    token_line = next(ln for ln in lines if ln.startswith("token: "))
    worker_id = worker_id_line.removeprefix("worker_id: ")
    bearer = token_line.removeprefix("token: ")
    assert worker_id.startswith("w-"), f"unexpected worker_id shape: {worker_id!r}"
    assert bearer
    # No banner between the two key lines (they appear adjacent in the
    # stdout stream; banners would land on stderr instead).
    pos_worker = out.index(worker_id_line)
    pos_token = out.index(token_line)
    between = out[pos_worker + len(worker_id_line) : pos_token].strip()
    assert between == "", f"unexpected stdout between key lines: {between!r}"

    # ``execvp`` was invoked with the right argv (whilly-worker --connect <url> ...).
    assert len(patch_execvp) == 1
    exec_file, exec_argv = patch_execvp[0]
    assert exec_file == "whilly-worker"
    assert exec_argv[:2] == ["whilly-worker", "--connect"]
    assert control_plane in exec_argv
    assert "--token" in exec_argv
    assert bearer in exec_argv
    assert "--plan" in exec_argv and _PLAN_ID in exec_argv

    # Server-side: a workers row exists with the requested hostname.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT worker_id, hostname FROM workers WHERE worker_id=$1",
            worker_id,
        )
    assert row is not None
    assert row["hostname"] == _HOSTNAME

    # Fallback file persisted the bearer with the canonical URL key.
    cred_path = whilly_secrets.credentials_file_path()
    assert cred_path.is_file(), f"fallback file not written at {cred_path}"
    data = json.loads(cred_path.read_text(encoding="utf-8"))
    assert data == {control_plane: bearer}

    # And the read-side helper finds it.
    assert whilly_secrets.load_worker_credential(control_plane) == bearer

    # The returned bearer authenticates a follow-up RPC.
    async with AsyncClient(base_url=control_plane) as client:
        resp = await client.post(
            f"/workers/{worker_id}/heartbeat",
            json={"worker_id": worker_id},
            headers={"Authorization": f"Bearer {bearer}"},
        )
    assert resp.status_code == 200, resp.text


async def test_connect_no_keychain_does_not_touch_storage(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    monkeypatch: pytest.MonkeyPatch,
    patch_execvp: list[tuple[str, list[str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--no-keychain`` skips both keyring and the fallback file (VAL-M1-CONNECT-006)."""
    set_calls: list[tuple[str, str]] = []

    def _record_set(service: str, username: str, password: str) -> None:
        set_calls.append((service, username))

    monkeypatch.setattr(whilly_secrets, "_set_keyring_password", _record_set)

    code = await asyncio.to_thread(
        run_connect_command,
        [
            control_plane,
            "--bootstrap-token",
            _BOOTSTRAP_TOKEN,
            "--plan",
            _PLAN_ID,
            "--no-keychain",
        ],
    )
    assert code == EXIT_OK

    # No keyring writes.
    assert set_calls == []
    # No fallback file written.
    cred_path = whilly_secrets.credentials_file_path()
    assert not cred_path.exists(), f"unexpected fallback file at {cred_path}"

    # Bearer was still printed on stdout and exec'd into.
    out = capsys.readouterr().out
    assert "worker_id: w-" in out
    assert "token: " in out
    assert len(patch_execvp) == 1


async def test_connect_with_wrong_bootstrap_token_exits_1(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    monkeypatch: pytest.MonkeyPatch,
    patch_execvp: list[tuple[str, list[str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wrong bootstrap token → exit 1, no exec, no workers row (VAL-M1-CONNECT-009 / VAL-M1-DEMO-011)."""
    set_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        whilly_secrets,
        "_set_keyring_password",
        lambda s, u, p: set_calls.append((s, u)),
    )

    async with db_pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM workers")

    code = await asyncio.to_thread(
        run_connect_command,
        [
            control_plane,
            "--bootstrap-token",
            "WRONG",
            "--plan",
            _PLAN_ID,
        ],
    )
    assert code == EXIT_CONNECT_ERROR

    err = capsys.readouterr().err
    assert "401" in err

    # No workers row added.
    async with db_pool.acquire() as conn:
        after = await conn.fetchval("SELECT count(*) FROM workers")
    assert after == before

    # No exec, no keyring write.
    assert patch_execvp == []
    assert set_calls == []


async def test_connect_unreachable_server_exits_1(
    patched_xdg: str,
    monkeypatch: pytest.MonkeyPatch,
    patch_execvp: list[tuple[str, list[str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live-but-unbound URL surfaces as exit 1 with a clear diagnostic (VAL-M1-CONNECT-010)."""
    set_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        whilly_secrets,
        "_set_keyring_password",
        lambda s, u, p: set_calls.append((s, u)),
    )
    # Pick a free port so the connect attempt cleanly refuses.
    free_port = _find_free_port()
    code = await asyncio.to_thread(
        run_connect_command,
        [
            f"http://127.0.0.1:{free_port}",
            "--bootstrap-token",
            "anything",
            "--plan",
            _PLAN_ID,
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    # Either the unreachable diagnostic (transport-level error) or the
    # mapped server-error path is acceptable — both surface a non-empty
    # stderr that names the URL we tried.
    assert "127.0.0.1" in err
    assert patch_execvp == []
    assert set_calls == []
