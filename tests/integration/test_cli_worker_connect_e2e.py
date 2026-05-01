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
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

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

# Resolved at module load so a missing fixture fails fast at collection
# time rather than mid-spawn with a binary-not-found error. Mirrors
# tests/integration/test_phase5_remote.py.
_FAKE_CLAUDE_PATH: Path = (Path(__file__).parent.parent / "fixtures" / "fake_claude.sh").resolve()


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


# ---------------------------------------------------------------------------
# Identity hand-off regression: connect must forward --worker-id so the
# exec'd whilly-worker uses the registered identity bound to the bearer
# token. Without this, every /tasks/claim and /workers/<id>/heartbeat
# returns 403 because _require_token_owner rejects the mismatch.
# (M1 user-testing round 1 blocking finding: VAL-M1-CONNECT-008,
# VAL-M1-CONNECT-021, VAL-M1-ENTRYPOINT-002.)
# ---------------------------------------------------------------------------


async def test_handoff_argv_includes_registered_worker_id(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    force_file_fallback: None,
    patch_execvp: list[tuple[str, list[str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``connect``'s exec argv carries ``--worker-id <minted_id>`` so whilly-worker reuses the registered identity.

    Regression for the M1 round-1 blocking finding: without this, the
    exec'd worker auto-generated a fresh id and every /tasks/claim and
    /workers/<id>/heartbeat returned 403.
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

    # Pull the registered worker_id from stdout (the canonical surface).
    out = capsys.readouterr().out
    worker_id_line = next(ln for ln in out.splitlines() if ln.startswith("worker_id: "))
    registered_worker_id = worker_id_line.removeprefix("worker_id: ")

    # Exec argv must carry --worker-id and it must equal the registered id.
    assert len(patch_execvp) == 1
    _, exec_argv = patch_execvp[0]
    assert "--worker-id" in exec_argv, (
        f"connect must forward --worker-id to whilly-worker (M1 handoff fix); got argv={exec_argv}"
    )
    idx = exec_argv.index("--worker-id")
    assert exec_argv[idx + 1] == registered_worker_id, (
        f"forwarded worker_id ({exec_argv[idx + 1]!r}) must match registered id ({registered_worker_id!r})"
    )

    # And the DB row exists with this id (sanity).
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT worker_id FROM workers WHERE worker_id = $1",
            registered_worker_id,
        )
    assert row is not None


async def test_handoff_claim_and_heartbeat_return_200_not_403(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    force_file_fallback: None,
    patch_execvp: list[tuple[str, list[str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact ``(worker_id, bearer)`` pair connect hands off authenticates on claim + heartbeat.

    This is the M1 round-1 blocking-finding regression test: with the
    pre-fix code, the worker_id passed to /tasks/claim and
    /workers/<id>/heartbeat would be the auto-generated id from
    whilly-worker's startup, NOT the registered id, so
    _require_token_owner would reject both with 403. The fix forwards
    ``--worker-id <registered_id>`` so the exec'd worker reuses the
    registered identity.

    We don't actually spawn whilly-worker here — we drive the exact
    HTTP contract whilly-worker would drive (claim + heartbeat with the
    bearer + the worker_id from connect's exec argv). That's the
    layer where the 403 happened.
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

    # Pull bearer + worker_id straight out of the exec argv — that's
    # what whilly-worker would have received.
    assert len(patch_execvp) == 1
    _, exec_argv = patch_execvp[0]
    bearer = exec_argv[exec_argv.index("--token") + 1]
    argv_worker_id = exec_argv[exec_argv.index("--worker-id") + 1]

    # Seed a PENDING task so /tasks/claim has something to hand back —
    # otherwise the long-poll burns its budget. The seed mirrors the
    # phase5 fixture but with a minimal column set.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            _PLAN_ID,
            "M1 connect handoff regression",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, priority,
                description, key_files, acceptance_criteria, test_steps,
                prd_requirement, version, created_at, updated_at
            ) VALUES (
                $1, $2, 'PENDING', 'critical',
                $3, $4::jsonb, $5::jsonb, $6::jsonb,
                $7, 1, NOW(), NOW()
            )
            """,
            "T-HANDOFF-1",
            _PLAN_ID,
            "M1 handoff regression — claim must return 200, not 403.",
            json.dumps(["whilly/cli/worker.py"]),
            json.dumps(["claim returns 200"]),
            json.dumps(["pytest tests/integration/test_cli_worker_connect_e2e.py -v -k handoff"]),
            "M1-handoff",
        )

    # Drive the exact RPCs whilly-worker would drive.
    async with AsyncClient(base_url=control_plane) as client:
        claim_resp = await client.post(
            "/tasks/claim",
            json={"worker_id": argv_worker_id, "plan_id": _PLAN_ID},
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=10.0,
        )
        # 200 means a task came back; 204 would mean the queue was empty
        # — neither path is the 403 the regression produced. We seeded a
        # task above so 200 is the expected.
        assert claim_resp.status_code == 200, (
            f"expected 200 on /tasks/claim with handed-off identity but got {claim_resp.status_code}: {claim_resp.text}"
        )

        heartbeat_resp = await client.post(
            f"/workers/{argv_worker_id}/heartbeat",
            json={"worker_id": argv_worker_id},
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=10.0,
        )
        assert heartbeat_resp.status_code == 200, (
            f"expected 200 on /workers/<id>/heartbeat with handed-off identity but got "
            f"{heartbeat_resp.status_code}: {heartbeat_resp.text}"
        )


async def test_handoff_subprocess_drains_one_task_exit_0(
    db_pool: asyncpg.Pool,
    control_plane: str,
    patched_xdg: str,
    force_file_fallback: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: connect → register → exec whilly-worker → claim 1 task → COMPLETE → exit 0.

    Spawns the actual ``whilly-worker`` (via ``python -m whilly.cli.worker``)
    using the exact argv that ``run_connect_command`` would have
    execvp'd, then waits for the subprocess to exit 0. Confirms the
    integrated flow against a live HTTP transport, not just the argv
    surface.

    Skipped if the ``fake_claude.sh`` fixture is missing (matches
    test_phase5_remote.py's resilience).
    """
    if not _FAKE_CLAUDE_PATH.is_file():
        pytest.skip(f"fake claude binary missing at {_FAKE_CLAUDE_PATH}")

    captured: list[tuple[str, list[str]]] = []

    def _capture(file: str, args: list[str]) -> None:
        captured.append((file, list(args)))

    monkeypatch.setattr(cli_worker.os, "execvp", _capture)

    # Seed plan + a PENDING task before connect runs.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            _PLAN_ID,
            "M1 connect handoff e2e",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, priority,
                description, key_files, acceptance_criteria, test_steps,
                prd_requirement, version, created_at, updated_at
            ) VALUES (
                $1, $2, 'PENDING', 'critical',
                $3, $4::jsonb, $5::jsonb, $6::jsonb,
                $7, 1, NOW(), NOW()
            )
            """,
            "T-HANDOFF-E2E",
            _PLAN_ID,
            "M1 handoff e2e — exec'd worker drains one task and exits 0.",
            json.dumps(["whilly/cli/worker.py"]),
            json.dumps(["worker exits 0"]),
            json.dumps(["pytest tests/integration/test_cli_worker_connect_e2e.py"]),
            "M1-handoff-e2e",
        )

    # Drive connect (captures exec argv).
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
            # Use the passthrough sentinel to forward --once so the
            # exec'd worker is self-terminating after one COMPLETE.
            "--",
            "--once",
        ],
    )
    assert code == EXIT_OK
    assert len(captured) == 1
    _, exec_argv = captured[0]
    assert "--worker-id" in exec_argv, "connect must hand off --worker-id (M1 handoff fix)"
    registered_worker_id = exec_argv[exec_argv.index("--worker-id") + 1]

    # Translate the captured argv (which targets the ``whilly-worker``
    # binary) into a python-module invocation so we always run the venv
    # currently under test (avoiding stale pipx installs on PATH).
    # ``exec_argv[0]`` is "whilly-worker"; the rest is the worker-runtime
    # argv (``--connect URL --token X --plan P --worker-id ID --once``).
    worker_args = exec_argv[1:]
    cmd: tuple[str, ...] = (sys.executable, "-m", "whilly.cli.worker", *worker_args)

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_BIN": str(_FAKE_CLAUDE_PATH),
        "PYTHONUNBUFFERED": "1",
    }

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        pytest.fail(
            "exec'd whilly-worker did not exit within 30s — handoff identity may have produced 403 on claim/heartbeat"
        )

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    assert proc.returncode == 0, (
        f"exec'd whilly-worker exited with code {proc.returncode}\nstdout:\n{stdout_text}\nstderr:\n{stderr_text}"
    )

    # Audit trail: CLAIM + COMPLETE for the seeded task, both attributed
    # to the *registered* worker_id (the regression's smoking gun).
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type AS type, payload FROM events WHERE task_id = $1 ORDER BY id",
            "T-HANDOFF-E2E",
        )
    types = [r["type"] for r in rows]
    assert "CLAIM" in types and "COMPLETE" in types, (
        f"expected CLAIM+COMPLETE events; got {types}\nworker stderr:\n{stderr_text}"
    )

    def _payload(raw: object) -> dict:
        if isinstance(raw, str):
            return json.loads(raw)
        assert isinstance(raw, dict)
        return raw

    claim_payload = _payload(rows[types.index("CLAIM")]["payload"])
    assert claim_payload.get("worker_id") == registered_worker_id, (
        f"CLAIM must be attributed to the registered worker_id ({registered_worker_id!r}) — got {claim_payload!r}"
    )
