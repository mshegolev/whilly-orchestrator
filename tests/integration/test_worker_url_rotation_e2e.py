"""End-to-end check for the M2 ``m2-worker-url-refresh-on-rotation`` feature.

What this test proves
---------------------
The localhost.run sidecar publishes a rotating ``https://<rand>.lhr.life``
URL to a shared file (``WHILLY_FUNNEL_URL_FILE``). When the sidecar
rotates that URL, the worker MUST:

1. Detect the change within ``WHILLY_FUNNEL_URL_POLL_SECONDS``.
2. Tear down the current ``RemoteWorkerClient`` (and any in-flight
   long-poll), and release any in-flight CLAIMED task back to
   ``PENDING`` via ``POST /tasks/{id}/release`` so a peer can pick it
   up immediately rather than waiting for the visibility-timeout sweep.
3. Open a fresh client against the rotated URL while reusing the SAME
   ``worker_id`` and bearer that ``POST /workers/register`` originally
   minted — i.e. NO second register call (no duplicate-worker row).
4. Resume the long-poll loop against the rotated URL.

The integration shape uses the existing in-process uvicorn pattern from
``tests/integration/test_worker_keyring_resume.py``: the control-plane
runs on a free loopback port, the worker is the real
:func:`run_remote_worker_with_url_rotation` supervisor, and the URL
"rotation" is achieved by writing two distinct values into a tmp file
that the supervisor polls — both pointing at the *same* uvicorn
endpoint so the second session's claim/release calls actually reach a
live control-plane and we can read the resulting state from the
``workers`` and ``events`` tables.

This is deliberately not a docker-compose test: the docker funnel
sidecar is exercised in
``tests/integration/test_funnel_sidecar_url_publish.py``; here we
focus on the worker-side state-machine and DB invariants the M2 spec
contracts on.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import uvicorn
from httpx import AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.runner.result_parser import AgentResult
from whilly.adapters.transport.client import RemoteWorkerClient
from whilly.adapters.transport.server import create_app
from whilly.core.models import Plan, Task
from whilly.worker.funnel import FileUrlSource
from whilly.worker.remote import (
    RotationStats,
    run_remote_worker_with_url_rotation,
)

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "TEST_BOOTSTRAP_ROTATION"
_PLAN_ID = "demo-rotation"
_HOSTNAME = "host-rotation"


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
        claim_long_poll_timeout=1.0,
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
    server_task = asyncio.create_task(server.serve(), name="rotation-uvicorn")
    try:
        await _wait_until_started(server)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)
        if not server_task.done():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await server_task


async def _register_worker(control_plane_url: str) -> tuple[str, str]:
    """``POST /workers/register`` → ``(worker_id, plaintext_bearer)``."""
    async with AsyncClient(base_url=control_plane_url) as client:
        resp = await client.post(
            "/workers/register",
            json={"hostname": _HOSTNAME},
            headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
        )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    return body["worker_id"], body["token"]


async def test_url_rotation_reuses_worker_id_and_releases_in_flight_task(
    db_pool: asyncpg.Pool,
    control_plane: str,
    tmp_path: Path,
) -> None:
    """End-to-end rotation: same worker_id, no re-register, in-flight task released.

    Scenario:

    1. Register a worker via the bootstrap-token endpoint to mint
       ``(worker_id, bearer)``.
    2. Insert one PENDING task into the plan so the rotation supervisor's
       *first* session can claim it.
    3. Seed ``url.txt`` with the live control-plane URL (URL_A) — same
       endpoint we'll switch to under URL_B so the second session's
       claim/release actually hit the real control-plane.
    4. Start :func:`run_remote_worker_with_url_rotation` with a runner
       that blocks long enough for the watcher to observe a rotation.
    5. Wait until session 1 has claimed the task, then rewrite the file
       to URL_B (same host, different ``127.0.0.1:port`` alias via
       ``localhost`` to make the URL string textually distinct).
    6. Watcher detects the change → inner loop tears down → release
       fires against the live control-plane → second session opens.
    7. After we observe the rotation, set the outer stop event so the
       supervisor returns.

    Assertions:

    * Exactly ONE ``workers`` row exists for our hostname (no duplicate
      register).
    * Rotation stats: ``inner_runs >= 2``, ``url_rotations >= 1``.
    * Released task's status is back to ``PENDING`` (peer can re-claim).
    * Control-plane events table contains a ``tasks.release`` event with
      ``reason='shutdown'`` for the in-flight task on URL_A.
    """
    worker_id, bearer = await _register_worker(control_plane)

    # The control-plane is bound to 127.0.0.1; expose URL_B as the same
    # endpoint via the ``localhost`` alias so the rotation file change
    # is textually distinct from URL_A but functionally still hits our
    # in-process control-plane (TCP connect to 127.0.0.1).
    url_a = control_plane
    port = control_plane.rsplit(":", 1)[1]
    url_b = f"http://localhost:{port}"
    assert url_a != url_b, "test setup error: URLs must differ to trigger rotation"

    # Seed plan + one PENDING task using the shared db_pool.
    task_id = "rot-task-001"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans(id, name) VALUES($1, $2) ON CONFLICT(id) DO UPDATE SET name = EXCLUDED.name",
            _PLAN_ID,
            "rotation plan",
        )
        await conn.execute(
            "INSERT INTO tasks(id, plan_id, status, priority, description, version)"
            " VALUES($1, $2, 'PENDING', 'medium', 'rotation task', 1)",
            task_id,
            _PLAN_ID,
        )

    # Funnel-URL file: starts at URL_A, rotates to URL_B mid-session.
    url_file = tmp_path / "funnel-url.txt"
    url_file.write_text(url_a + "\n", encoding="utf-8")
    source = FileUrlSource(url_file, poll_interval=0.05)

    runner_started = asyncio.Event()
    rotation_stop = asyncio.Event()

    async def runner(_task: Task, _prompt: str) -> AgentResult:
        runner_started.set()
        # Block until the rotation supervisor cancels us (URL change).
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    @contextlib.asynccontextmanager
    async def _client_factory(url: str) -> AsyncIterator[RemoteWorkerClient]:
        # Same bearer + worker_id reused across sessions — the whole
        # point of the feature. Different ``url`` per call.
        async with RemoteWorkerClient(url, bearer) as client:
            yield client

    plan = Plan(id=_PLAN_ID, name="rotation plan")

    async def _orchestrate() -> None:
        # Wait for the runner to start (= session 1 claimed the task)
        # then rotate the URL.
        await asyncio.wait_for(runner_started.wait(), timeout=20.0)
        await asyncio.sleep(0.2)
        url_file.write_text(url_b + "\n", encoding="utf-8")
        # Wait long enough for the watcher to observe + supervisor to
        # tear down session 1, release the task, open session 2, and
        # the second session to settle into its long-poll.
        await asyncio.sleep(2.5)
        rotation_stop.set()

    orchestrator = asyncio.create_task(_orchestrate(), name="rotation-orchestrator")
    try:
        rotation_stats: RotationStats = await asyncio.wait_for(
            run_remote_worker_with_url_rotation(
                _client_factory,
                runner,
                plan,
                worker_id,
                url_a,
                source,
                heartbeat_interval=0.5,
                install_signal_handlers=False,
                stop=rotation_stop,
            ),
            timeout=30.0,
        )
    finally:
        orchestrator.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await orchestrator
        await source.aclose()

    # ---- Assertions -------------------------------------------------- #
    assert rotation_stats.inner_runs >= 2, f"expected >=2 sessions across rotation, got {rotation_stats.inner_runs}"
    assert rotation_stats.url_rotations >= 1, f"expected >=1 rotation, got {rotation_stats.url_rotations}"

    async with db_pool.acquire() as conn:
        # Exactly one workers row was registered for this hostname —
        # the rotation must NOT have re-registered.
        worker_rows = await conn.fetch(
            "SELECT worker_id, hostname FROM workers WHERE hostname = $1",
            _HOSTNAME,
        )
        assert len(worker_rows) == 1, (
            f"expected exactly 1 workers row for {_HOSTNAME}, got {len(worker_rows)}: {[dict(r) for r in worker_rows]}"
        )
        assert worker_rows[0]["worker_id"] == worker_id

        # Released task is back to PENDING (peer can re-claim).
        task_row = await conn.fetchrow(
            "SELECT status, version FROM tasks WHERE id = $1",
            task_id,
        )
        assert task_row is not None, f"task {task_id} disappeared"
        assert task_row["status"] == "PENDING", f"expected released task back to PENDING, got {task_row['status']!r}"

        # The rotation must have emitted a RELEASE event with
        # reason='shutdown' (canonical reason for cooperative tear-down).
        release_events = await conn.fetch(
            "SELECT payload FROM events WHERE task_id = $1 AND event_type = 'RELEASE'",
            task_id,
        )
        assert release_events, "expected a RELEASE event for the in-flight task"
        # Whitebox: the worker's RELEASE call passes ``reason='shutdown'``;
        # any future renaming will trip this assertion.
        import json as _json

        reasons: list[object] = []
        for e in release_events:
            payload = e["payload"]
            if isinstance(payload, str):
                try:
                    payload = _json.loads(payload)
                except ValueError:
                    payload = {}
            if isinstance(payload, dict):
                reasons.append(payload.get("reason"))
        assert "shutdown" in reasons, f"expected 'shutdown' release reason, got {reasons}"
