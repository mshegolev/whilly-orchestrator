"""Phase 5 e2e gate (TASK-024a, PRD Day 5 deliverable, SC-3).

Closes Phase 5 by composing the *remote* deployment shape into a single
end-to-end flow over a real HTTP transport: the worker process runs
under a separate OS-level identity, talks to the FastAPI control plane
through a TCP socket, drains one task, and exits 0.

Why this gate exists
--------------------
Each link is unit-tested in isolation:

* :mod:`tests.integration.test_transport_workers` proves register +
  heartbeat round-trip against a real Postgres but in-process ASGI
  (httpx :class:`ASGITransport`).
* :mod:`tests.integration.test_transport_tasks` proves claim / complete
  / fail land in the database via the same in-process ASGI shim.
* :mod:`tests.integration.test_remote_worker_heartbeat` and
  :mod:`...remote_worker_signals` exercise the heartbeat composition
  but against a fake :class:`RemoteWorkerClient` (no real HTTP).
* :mod:`tests.integration.test_phase4_e2e` proves the *local* worker
  drains a plan end-to-end against a real Postgres + a real subprocess
  agent.

The Phase 4 gate covers the whole flow but bypasses the HTTP transport
(local worker talks asyncpg directly). This Phase 5 gate is the *only*
test that exercises:

* The TCP socket between worker and control plane (``127.0.0.1:<port>``,
  not in-process ASGI).
* The whilly-worker console-script entry point as a separate OS process
  with its own argv parsing, env-var resolution, asyncio loop, and
  process exit-code routing.
* The bearer-token authentication path against a real ``Authorization:
  Bearer ...`` header round-trip on the wire.

PRD SC-3 (``Запустить второй процесс whilly-worker --connect URL --token X
на другой VM, он claim'ит задачу через HTTP, выполняет, и завершает её``)
is what this test pins down.
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
from typing import Any

import asyncpg
import pytest
import uvicorn

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app

pytestmark = DOCKER_REQUIRED

# Bearer + bootstrap tokens shared between the FastAPI app's auth deps
# and the whilly-worker subprocess via env vars. Plaintext is fine here:
# testcontainers Postgres + uvicorn live in the test process and are
# torn down on exit; nothing leaks past the test run.
WORKER_TOKEN = "phase5-worker-bearer-token"
BOOTSTRAP_TOKEN = "phase5-bootstrap-secret"

PLAN_ID = "plan-phase5-remote"
PROJECT_NAME = "Phase 5 e2e gate (SC-3)"
TASK_ID = "T-PHASE5-1"
WORKER_ID = "w-phase5-remote"

# Resolved at module load so a missing fixture fails fast at collection
# time rather than mid-spawn with a binary-not-found error. Same pattern
# as test_phase4_e2e.py.
FAKE_CLAUDE_PATH: Path = (Path(__file__).parent.parent / "fixtures" / "fake_claude.sh").resolve()


def _find_free_port() -> int:
    """Bind a transient socket to port 0; return the kernel-assigned port.

    Using port 0 avoids hard-coding a port that might collide with the
    developer's local Postgres / FastAPI / dev server. The bind-and-
    release race is negligible — uvicorn's bind retries through any
    brief TIME_WAIT window.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_started(server: uvicorn.Server, *, timeout: float = 10.0) -> None:
    """Spin-poll ``server.started`` until uvicorn's bootstrap completes.

    Uvicorn flips ``started=True`` after the socket is bound and the
    lifespan startup completes, but exposes no awaitable signal. 50ms
    polling is fast enough to keep the test under a second on warm
    hardware, slow enough not to dominate CPU.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while not server.started:
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"uvicorn did not signal started within {timeout}s")
        await asyncio.sleep(0.05)


async def _seed_plan_and_worker(pool: asyncpg.Pool) -> None:
    """Seed one plan + one PENDING task + one worker row directly via SQL.

    One task is the smallest set that proves SC-3's "claim and complete"
    contract. Two would also exercise the drain loop, but ``--once``
    exits after the first COMPLETE, so a second-task assertion would
    silently pass on PENDING and tell us nothing.

    The worker row is required because ``tasks.claimed_by`` has a foreign
    key to ``workers.worker_id`` (see schema.sql). Without it, the first
    successful ``claim_task`` would crash on the FK constraint at the
    UPDATE statement. We seed the worker out-of-band rather than going
    through ``POST /workers/register`` because:

    1. The whilly-worker CLI itself doesn't register — it assumes the
       worker row exists (TASK-022c docstring: "assumes the worker row
       already exists on the control plane (registered out-of-band via
       the bootstrap-token flow)"). A registration round-trip in the
       test would diverge from the production deployment shape.
    2. ``token_hash`` doesn't have to match the bearer the CLI presents:
       the bearer ``WHILLY_WORKER_TOKEN`` is the *cluster-shared* token
       validated by ``bearer_dep``; ``workers.token_hash`` is per-worker
       state used only by future per-worker token-rotation flows that
       don't ship in this release. We park a placeholder hash here.

    Going direct on the plan/task seed (vs ``whilly plan import``) keeps
    the test's dependency surface to ``asyncpg`` only — the import flow
    is already gated by ``tests.integration.test_phase2_e2e`` and
    reusing it would couple Phase 5 to Phase 4's fixture choices.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            PLAN_ID,
            PROJECT_NAME,
        )
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, token_hash, status)
            VALUES ($1, $2, $3, 'online')
            """,
            WORKER_ID,
            "phase5-test-host",
            "phase5-placeholder-hash",
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
            TASK_ID,
            PLAN_ID,
            "Phase 5 SC-3 canary — remote worker over real HTTP transport.",
            json.dumps(["whilly/cli/worker.py"]),
            json.dumps(["worker subprocess exits 0", "task ends in DONE"]),
            json.dumps(["pytest tests/integration/test_phase5_remote.py -v"]),
            "SC-3",
        )


def _decode_payload(raw: object) -> dict[str, Any]:
    """Decode an asyncpg JSONB cell to a dict.

    asyncpg returns JSONB as raw ``str`` (JSON text) by default — the
    pool deliberately doesn't register a codec.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        assert isinstance(decoded, dict), f"unexpected JSON shape: {decoded!r}"
        return decoded
    raise AssertionError(f"unexpected JSONB payload type {type(raw).__name__}: {raw!r}")


@pytest.fixture
async def control_plane(db_pool: asyncpg.Pool) -> AsyncIterator[str]:
    """Boot a uvicorn-hosted FastAPI control plane on a free port; yield URL.

    Tight ``claim_poll_interval`` + ``claim_long_poll_timeout`` keep the
    test deterministic — a worker that misses the first claim shouldn't
    sit through the production 30s budget on every poll. The seeded task
    is already PENDING so a 50ms re-poll is plenty.

    Lifecycle: ``create_app`` wires deps; uvicorn.Server runs as an
    asyncio task; we spin-poll ``started`` so callers don't race it.
    Teardown sets ``should_exit`` so ``serve()`` returns cleanly, with
    a bounded wait so a hung server fails fast in the test report.
    """
    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=WORKER_TOKEN,
        bootstrap_token=BOOTSTRAP_TOKEN,
        claim_poll_interval=0.05,
        claim_long_poll_timeout=5.0,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        # lifespan="on" forces uvicorn to invoke the FastAPI lifespan
        # protocol — without it the claim/complete handlers crash on
        # AttributeError because app.state.repo is never populated.
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="phase5-uvicorn")
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


async def test_phase5_remote_worker_completes_task_via_http(
    db_pool: asyncpg.Pool,
    control_plane: str,
) -> None:
    """SC-3 demonstrated end-to-end through the remote-worker shape.

    Flow:
      1. Seed a single PENDING task under PLAN_ID via direct SQL.
      2. Spawn ``python -m whilly.cli.worker --once`` as a subprocess
         pointed at the in-test FastAPI server. The subprocess gets a
         minimal env (PATH + HOME + worker config + CLAUDE_BIN) so a
         stray pytest var can't perturb it.
      3. Wait up to 30s for the subprocess to exit; assert exit code 0.
      4. Assert the task transitioned PENDING -> DONE and claimed_by
         matches the worker id we configured.
      5. Assert the events table contains CLAIM and COMPLETE rows in
         the right order — audit trail integrity is half the deal in
         PRD FR-1.6.

    Why ``--once`` rather than env-var equivalent:
        ``max_processed=1`` is unconditionally set when ``--once`` is on
        the argv (see :mod:`whilly.cli.worker`); there's no env-var
        path. Using the flag here keeps the test faithful to the AC
        text "Single-task mode: one task, then exit 0".

    Why a separate OS process rather than in-process worker:
        SC-3's whole point is "remote worker on another VM". An in-
        process worker would share the test's event loop, asyncpg
        pool, and Python process — exactly the things SC-3 promises
        *not* to share. The process boundary forces the test through
        the full transport surface (httpx -> TCP -> uvicorn ->
        Starlette -> FastAPI dep + handler -> asyncpg) instead of
        cheating across the layer.
    """
    await _seed_plan_and_worker(db_pool)

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # whilly-worker bootstrap — these land in the CLI's env-resolution
        # path (whilly.cli.worker.run_worker_command), mirroring how a
        # Kubernetes Pod or systemd unit would inject config into a worker.
        "WHILLY_CONTROL_URL": control_plane,
        "WHILLY_WORKER_TOKEN": WORKER_TOKEN,
        "WHILLY_PLAN_ID": PLAN_ID,
        "WHILLY_WORKER_ID": WORKER_ID,
        "CLAUDE_BIN": str(FAKE_CLAUDE_PATH),
        # Disable Python output buffering so subprocess stderr is
        # interleaved in real time — invaluable for diagnosing hangs.
        "PYTHONUNBUFFERED": "1",
    }
    # Invoke the worker via the *current* interpreter's module entry —
    # NOT via ``shutil.which("whilly-worker")``. PATH on a developer
    # workstation often points at a stale ``pipx`` install of
    # whilly-orchestrator (e.g. ``~/.local/bin/whilly-worker`` resolved
    # through ``~/.local/pipx/venvs/whilly-orchestrator/...``), whose
    # frozen-in-time site-packages may be missing transitive deps the
    # current source tree requires (we have observed
    # ``ModuleNotFoundError: No module named 'fastapi'`` from a stale
    # pipx env in this exact path). ``sys.executable -m whilly.cli.worker``
    # binds the subprocess to the venv that's running the test, which
    # is the venv where ``pip install -e '.[dev]'`` was run, so it is
    # robust to PATH pollution and matches the in-venv install the
    # mission's init.sh prepares.
    cmd: tuple[str, ...] = (sys.executable, "-m", "whilly.cli.worker", "--once")
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
            "whilly-worker subprocess did not exit within 30s — likely a deadlock or the claim long-poll never returned"
        )

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    assert proc.returncode == 0, (
        f"whilly-worker exited with code {proc.returncode}\nstdout:\n{stdout_text}\nstderr:\n{stderr_text}\n"
    )

    async with db_pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT status, claimed_by, version FROM tasks WHERE id = $1",
            TASK_ID,
        )
    assert task_row is not None, f"task {TASK_ID!r} disappeared from tasks table"
    assert task_row["status"] == "DONE", (
        f"expected status DONE but got {task_row['status']!r}\n"
        f"--- worker stdout ---\n{stdout_text}\n"
        f"--- worker stderr ---\n{stderr_text}\n--- end ---"
    )
    assert task_row["claimed_by"] == WORKER_ID, f"expected claimed_by={WORKER_ID!r} but got {task_row['claimed_by']!r}"
    # Version monotonicity: 1 (seed) -> 2 (CLAIM) -> 3 (COMPLETE).
    # version<=1 means complete didn't fire the optimistic-lock UPDATE.
    assert task_row["version"] >= 3, f"expected version >= 3 (seed + claim + complete) but got {task_row['version']}"

    async with db_pool.acquire() as conn:
        event_rows = await conn.fetch(
            "SELECT event_type AS type, payload FROM events WHERE task_id = $1 ORDER BY id",
            TASK_ID,
        )
    event_types = [row["type"] for row in event_rows]
    assert "CLAIM" in event_types, (
        f"expected CLAIM event for task {TASK_ID!r}; got {event_types}\nworker stderr:\n{stderr_text}"
    )
    assert "COMPLETE" in event_types, (
        f"expected COMPLETE event for task {TASK_ID!r}; got {event_types}\nworker stderr:\n{stderr_text}"
    )
    # Order: CLAIM must precede COMPLETE — out-of-order would mean the
    # state-machine transitions fired wrong, corrupting the audit trail.
    claim_ix = event_types.index("CLAIM")
    complete_ix = event_types.index("COMPLETE")
    assert claim_ix < complete_ix, (
        f"expected CLAIM (idx {claim_ix}) to precede COMPLETE (idx {complete_ix}) in event log; got order {event_types}"
    )

    # Spot-check: CLAIM payload must record the worker_id — a regression
    # in the server's CLAIM-event emit path would still pass the type
    # check above but lose audit-trail attribution.
    claim_payload = _decode_payload(event_rows[claim_ix]["payload"])
    assert claim_payload.get("worker_id") == WORKER_ID, (
        f"CLAIM event payload should record worker_id={WORKER_ID!r}; got {claim_payload!r}"
    )
