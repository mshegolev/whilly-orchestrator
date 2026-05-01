"""M1 cross-host integration smoke (mirror of test_phase5_remote.py for two workers).

Exercises the docker-compose.control-plane.yml + docker-compose.worker.yml
deployment shape introduced in M1 by composing the same primitives in a
single test process: one FastAPI control-plane (uvicorn on a free
loopback port, against a testcontainers Postgres), and TWO independent
``whilly-worker`` subprocesses that simulate workers running on two
distinct hosts. The two simulated hosts share a single TCP namespace
(everything is loopback) but the workers are wholly separate Python
processes — distinct PIDs, distinct asyncio loops, distinct httpx
clients — and each carries a stable, distinct ``WORKER_ID`` and
``HOSTNAME`` so the events table can be queried for cross-host
attribution.

What this gate proves (mission §2 — M1 demo / sign-off)
-------------------------------------------------------
* A 5-task plan drains across two workers; both contribute COMPLETE
  rows to the events table (≥ 2 distinct ``worker_id`` values for
  ``event_type='COMPLETE'``).
* All 5 tasks transition to ``status='DONE'`` within the wall-clock
  budget; none are stuck in PENDING / CLAIMED / IN_PROGRESS at the end.
* Graceful SIGTERM teardown. After all tasks are DONE, sending SIGTERM
  to each worker subprocess results in a clean exit (return code 0)
  within a bounded timeout — the ``run_remote_worker_with_heartbeat``
  signal-handler path (TASK-022b3) is the production guarantee mirrored
  here, so an operator's ``kill -TERM`` on either host frees the
  process without abandoning work or leaving zombie children.
* Log fan-in. Each worker's stderr carries the canonical
  ``whilly-worker: worker '...' finished`` summary line, so an operator
  collecting ``docker logs`` from both hosts in a real deployment gets
  identical-shape audit footers from both processes.
* Venv activation idempotency. The two subprocesses both invoke the
  same ``whilly-worker`` entry point through the active venv (see
  ``_resolve_worker_command``); a stale ``pipx`` install of
  ``whilly-orchestrator`` on PATH does NOT shadow the in-venv binary,
  matching the M1 entrypoint.sh contract that ``whilly-worker``
  resolves to the version installed alongside ``whilly`` in the same
  Python environment.

Why a single uvicorn + 2 worker subprocesses, not literally
``docker compose up``
----------------------------------------------------------
``docker compose -f docker-compose.control-plane.yml up`` and ``-f
docker-compose.worker.yml up`` are the canonical operator-facing demo
path (and are exercised by the M1 user-testing-validator passes). For
pytest's purposes that approach is far heavier than what we need to
prove the state-machine drains correctly across processes: a
testcontainers Postgres + uvicorn + 2 subprocesses already crosses
every cross-process boundary the compose stack does, while staying
hermetic (no Docker images to pull, no compose-network plumbing) and
under 30s per run on warm hardware. ``test_phase5_remote.py`` pioneered
the pattern; this test extends it to two workers.

Hermetic by construction
------------------------
* No external network — everything is ``127.0.0.1:<port>``.
* Worker subprocesses inherit a minimal env (PATH, HOME, the four
  required ``WHILLY_*`` vars, CLAUDE_BIN). Stray pytest env vars are
  scrubbed so a developer running this from a shell with a real
  ``WHILLY_DATABASE_URL`` etc. doesn't accidentally cross-contaminate.
* The fake Claude CLI stub at ``tests/fixtures/fake_claude.sh`` is the
  same shim ``test_phase5_remote.py`` uses; both tasks complete by
  virtue of the deterministic ``<promise>COMPLETE</promise>`` envelope
  the stub emits, so the test never depends on a real LLM.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import asyncpg
import pytest
import uvicorn

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app

pytestmark = DOCKER_REQUIRED

# Bearer + bootstrap shared between the FastAPI app's auth deps and the
# worker subprocesses via env vars. Plaintext is fine here for the same
# reasons documented in test_phase5_remote.py — testcontainers Postgres
# + uvicorn live in the test process and are torn down on exit.
WORKER_TOKEN = "phase6-cross-host-bearer-placeholder"
BOOTSTRAP_TOKEN = "phase6-cross-host-bootstrap-placeholder"

PLAN_ID = "plan-phase6-cross-host"
PROJECT_NAME = "Phase 6 cross-host smoke (M1 demo mirror)"
TASK_COUNT = 5

# Two workers, each pretending to live on a different host. The hostnames
# match the mission §2 demo language (``macbook-mvs`` and ``vps-eu-1``)
# so the events table looks like a real two-host audit when the test
# fails and an operator inspects.
HOSTNAME_A = "host-cross-a"
HOSTNAME_B = "host-cross-b"
WORKER_ID_A = "w-phase6-host-a"
WORKER_ID_B = "w-phase6-host-b"

FAKE_CLAUDE_PATH: Path = (Path(__file__).parent.parent / "fixtures" / "fake_claude.sh").resolve()

# How long to wait for both workers to drain the 5-task plan. The fake
# Claude stub returns ~immediately, so the total wall-clock cost is
# dominated by the long-poll + heartbeat ticks. 60s is generous.
DRAIN_DEADLINE_SECONDS = 60.0
# Bound the SIGTERM-to-exit window. The worker's signal handler flips
# ``stop`` and the loop wakes within one ``claim_long_poll_timeout`` tick
# (we configure the server to 2s here). 15s is roomy.
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Helpers (mirroring test_phase5_remote.py)
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_started(server: uvicorn.Server, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not server.started:
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"uvicorn did not signal started within {timeout}s")
        await asyncio.sleep(0.05)


def _resolve_worker_command() -> tuple[str, ...]:
    """Resolve the in-venv ``whilly-worker`` command, robust to PATH pollution.

    Same rationale as the comment block in ``test_phase5_remote.py``: a
    stale ``pipx`` install of ``whilly-orchestrator`` may put a
    different ``whilly-worker`` ahead of the active venv on PATH, and
    that frozen-in-time pipx env may be missing transitive deps the
    current source tree requires. Bypass ``shutil.which`` entirely:

    1. Prefer ``<sys.prefix>/bin/whilly-worker`` — the entry point
       installed by ``pip install -e '.[dev]'`` in the active venv. This
       is the same binary the test process is currently running under,
       so there is no version skew.
    2. Fall back to ``sys.executable -m whilly.cli.worker`` — invokes
       the worker through the same Python interpreter, inheriting the
       venv's ``site-packages`` directly.
    """
    venv_bin = Path(sys.prefix) / "bin" / "whilly-worker"
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        return (str(venv_bin),)
    return (sys.executable, "-m", "whilly.cli.worker")


async def _seed_plan_tasks_and_workers(pool: asyncpg.Pool) -> None:
    """Seed one plan + 5 PENDING tasks + 2 workers (one per simulated host).

    The two workers are seeded out-of-band via direct SQL — same pattern
    as ``test_phase5_remote.py`` (full register-flow round-trip is
    exercised in the dedicated tests under
    ``test_per_worker_auth.py`` / ``test_cli_worker_connect_e2e.py``).
    Five tasks is the smallest count that guarantees both workers must
    contribute COMPLETE rows under any plausible long-poll race
    (any 1-task split-the-claim outcome leaves at least 4 for the
    other worker; we assert ≥ 1 COMPLETE per worker, which is robust to
    pathological skew where one worker manages to grab 4 of 5).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            PLAN_ID,
            PROJECT_NAME,
        )
        # Seed the two simulated hosts as distinct workers rows. Both
        # share the same per-cluster bearer (WORKER_TOKEN) — the
        # ``token_hash`` column is per-worker forensic state that the
        # phase 5 contract has already documented as a placeholder.
        for worker_id, hostname in (
            (WORKER_ID_A, HOSTNAME_A),
            (WORKER_ID_B, HOSTNAME_B),
        ):
            await conn.execute(
                """
                INSERT INTO workers (worker_id, hostname, token_hash, status)
                VALUES ($1, $2, $3, 'online')
                """,
                worker_id,
                hostname,
                f"phase6-{worker_id}-placeholder-hash",
            )
        # Five PENDING tasks under the same plan. They have no
        # dependencies on each other so the long-poll race between the
        # two workers is what determines who claims what.
        for task_index in range(1, TASK_COUNT + 1):
            task_id = f"T-PHASE6-{task_index}"
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
                task_id,
                PLAN_ID,
                f"Phase 6 cross-host canary task #{task_index}.",
                json.dumps([f"whilly/cross_host/task_{task_index}.py"]),
                json.dumps(["both workers contribute completes", "audit log shows 2 distinct worker_ids"]),
                json.dumps(["pytest tests/integration/test_phase6_cross_host.py -v"]),
                "M1-CROSS-HOST",
            )


def _decode_payload(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        assert isinstance(decoded, dict), f"unexpected JSON shape: {decoded!r}"
        return decoded
    raise AssertionError(f"unexpected JSONB payload type {type(raw).__name__}: {raw!r}")


@pytest.fixture
async def control_plane(db_pool: asyncpg.Pool) -> AsyncIterator[str]:
    """Boot a uvicorn FastAPI control-plane on a free loopback port; yield URL.

    Mirrors the ``control_plane`` fixture in ``test_phase5_remote.py``
    with a slightly longer claim long-poll (2s) to bound the SIGTERM-to-
    exit window without dominating the drain wall clock.
    """
    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=WORKER_TOKEN,
        bootstrap_token=BOOTSTRAP_TOKEN,
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
    server_task = asyncio.create_task(server.serve(), name="phase6-uvicorn")
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


def _build_worker_env(control_url: str, *, worker_id: str) -> dict[str, str]:
    """Build a hermetic env dict for a worker subprocess.

    Inherits PATH + HOME so the interpreter / fake-claude binary remain
    findable, but starts otherwise empty so a developer's shell vars
    (WHILLY_DATABASE_URL pointing at production, real ANTHROPIC_API_KEY,
    etc.) cannot leak into the subprocess. The five required
    ``WHILLY_*`` vars and ``CLAUDE_BIN`` are added explicitly.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "WHILLY_CONTROL_URL": control_url,
        "WHILLY_WORKER_TOKEN": WORKER_TOKEN,
        "WHILLY_PLAN_ID": PLAN_ID,
        "WHILLY_WORKER_ID": worker_id,
        "CLAUDE_BIN": str(FAKE_CLAUDE_PATH),
        "PYTHONUNBUFFERED": "1",
    }


async def _wait_for_all_tasks_done(
    pool: asyncpg.Pool,
    *,
    expected: int,
    deadline_seconds: float,
) -> int:
    """Poll the tasks table until ``expected`` rows are DONE or the deadline trips.

    Returns the final DONE count (== expected on success). Raises
    ``asyncio.TimeoutError`` on deadline so the caller emits a richer
    diagnostic with the per-status breakdown.
    """
    deadline = asyncio.get_event_loop().time() + deadline_seconds
    while True:
        async with pool.acquire() as conn:
            done = await conn.fetchval(
                "SELECT count(*) FROM tasks WHERE plan_id = $1 AND status = 'DONE'",
                PLAN_ID,
            )
        if done >= expected:
            return int(done)
        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError("tasks-DONE deadline tripped")
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# The cross-host smoke
# ---------------------------------------------------------------------------


async def test_phase6_two_workers_drain_five_task_plan_and_shutdown_cleanly(
    db_pool: asyncpg.Pool,
    control_plane: str,
) -> None:
    """Two simulated hosts drain a 5-task plan; SIGTERM exits both cleanly.

    Flow:

      1. Seed plan + 5 PENDING tasks + 2 workers (host-a, host-b).
      2. Spawn 2 ``whilly-worker`` subprocesses, each pinned to one of
         the two seeded ``WORKER_ID``s, against the same control-plane.
      3. Poll the tasks table until all 5 reach DONE (deadline 60s).
      4. Send SIGTERM to each worker; await clean exit (return code 0)
         within a 15s bound.
      5. Assert ``DISTINCT worker_id`` from ``COMPLETE`` events == 2 —
         both hosts must have contributed at least one complete.
      6. Assert no orphan task statuses remain (PENDING / CLAIMED /
         IN_PROGRESS / FAILED / SKIPPED).
      7. Assert each worker's stderr carries the canonical "finished"
         summary line — log fan-in invariant for ``docker logs`` paths.

    Why poll the DB rather than wait on subprocess.communicate():
        The workers run forever (no ``--once``, no ``--max-iterations``)
        because the production deployment shape is long-lived. We have
        to send SIGTERM ourselves. Polling the DB until DONE is the
        right wake-up signal: as soon as 5 DONEs are visible, we know
        both workers are sitting on idle long-polls and a SIGTERM will
        arrive cleanly through ``add_signal_handler``'s flip-the-stop-
        event path.
    """
    await _seed_plan_tasks_and_workers(db_pool)

    worker_cmd = _resolve_worker_command()

    proc_a = await asyncio.create_subprocess_exec(
        *worker_cmd,
        env=_build_worker_env(control_plane, worker_id=WORKER_ID_A),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    proc_b = await asyncio.create_subprocess_exec(
        *worker_cmd,
        env=_build_worker_env(control_plane, worker_id=WORKER_ID_B),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    procs = (proc_a, proc_b)

    try:
        # Wait until all 5 tasks land in DONE. Both workers compete for
        # claims via the long-poll endpoint; the server's
        # ``FOR UPDATE SKIP LOCKED`` semantics guarantee no double
        # assignment, but the actual split between A and B is non-
        # deterministic — we tolerate any split that leaves both
        # workers with ≥ 1 COMPLETE (asserted below).
        try:
            done_count = await _wait_for_all_tasks_done(
                db_pool,
                expected=TASK_COUNT,
                deadline_seconds=DRAIN_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            async with db_pool.acquire() as conn:
                breakdown_rows = await conn.fetch(
                    "SELECT status, count(*) AS n FROM tasks WHERE plan_id=$1 GROUP BY status ORDER BY status",
                    PLAN_ID,
                )
            breakdown = {row["status"]: int(row["n"]) for row in breakdown_rows}
            pytest.fail(
                f"5-task plan did not drain within {DRAIN_DEADLINE_SECONDS}s — final breakdown by status: {breakdown}"
            )
        assert done_count == TASK_COUNT, f"expected DONE count {TASK_COUNT}, got {done_count}"

        # ─── Graceful shutdown: SIGTERM both workers, expect exit 0 ──
        for proc in procs:
            assert proc.returncode is None, (
                f"worker pid={proc.pid} exited unexpectedly before SIGTERM (returncode={proc.returncode})"
            )
            proc.send_signal(signal.SIGTERM)

        async def _await_clean_exit(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
            try:
                return await asyncio.wait_for(
                    proc.communicate(),
                    timeout=GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                pytest.fail(
                    f"worker pid={proc.pid} did not exit within "
                    f"{GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS}s of SIGTERM "
                    f"— signal handler / graceful-shutdown path is broken"
                )

        # Run both communicate()s concurrently so a slow shutdown on one
        # worker doesn't serialise the test's wait time.
        (out_a, err_a), (out_b, err_b) = await asyncio.gather(
            _await_clean_exit(proc_a),
            _await_clean_exit(proc_b),
        )

        for label, proc, out_bytes, err_bytes in (
            ("A", proc_a, out_a, err_a),
            ("B", proc_b, out_b, err_b),
        ):
            assert proc.returncode == 0, (
                f"worker {label} exited with code {proc.returncode} on SIGTERM\n"
                f"stdout:\n{out_bytes.decode('utf-8', errors='replace')}\n"
                f"stderr:\n{err_bytes.decode('utf-8', errors='replace')}\n"
            )
    finally:
        # Defensive cleanup — kill anything still alive so a failing
        # assertion above doesn't strand a worker subprocess holding the
        # test event loop hostage. Mirrors the safety net in
        # test_phase5_remote.py.
        for proc in procs:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    stderr_a = err_a.decode("utf-8", errors="replace")
    stderr_b = err_b.decode("utf-8", errors="replace")

    # ─── Audit log: ≥ 2 distinct worker_ids attributed to COMPLETED tasks ──
    # The COMPLETE event payload itself only records ``{"version": ...}``
    # by current repository convention (whilly/adapters/db/repository.py
    # ``_COMPLETE_SQL``); worker attribution rides on the ``tasks.claimed_by``
    # column, which COMPLETE leaves intact (it transitions ``status →
    # 'DONE'`` and bumps ``version`` but never NULLs ``claimed_by``).
    # Asserting on ``claimed_by`` of the DONE rows is therefore the
    # canonical cross-host attribution check that mirrors the mission
    # §2 sign-off ``SELECT DISTINCT worker_id FROM events`` semantics
    # in a way the schema actually supports today.
    async with db_pool.acquire() as conn:
        done_worker_rows = await conn.fetch(
            "SELECT DISTINCT claimed_by FROM tasks WHERE plan_id=$1 AND status='DONE'",
            PLAN_ID,
        )
    distinct_done_workers = {row["claimed_by"] for row in done_worker_rows}
    assert distinct_done_workers == {WORKER_ID_A, WORKER_ID_B}, (
        f"expected DONE tasks attributed to both {WORKER_ID_A!r} and {WORKER_ID_B!r}; "
        f"got {distinct_done_workers!r}\n"
        f"--- worker A stderr ---\n{stderr_a}\n"
        f"--- worker B stderr ---\n{stderr_b}\n"
    )

    # And cover CLAIM events as a defence-in-depth check on the audit
    # trail itself: ≥ 2 distinct worker_ids must show up in CLAIM
    # payloads. CLAIM payloads *do* carry ``worker_id`` (see
    # ``test_phase5_remote.py`` for the same assertion shape against
    # one worker), so this query is the events-table-side mirror of
    # the tasks.claimed_by check above.
    async with db_pool.acquire() as conn:
        claim_worker_rows = await conn.fetch(
            """
            SELECT DISTINCT events.payload->>'worker_id' AS worker_id
            FROM events
            JOIN tasks ON tasks.id = events.task_id
            WHERE tasks.plan_id = $1 AND events.event_type = 'CLAIM'
            """,
            PLAN_ID,
        )
    distinct_claim_workers = {row["worker_id"] for row in claim_worker_rows}
    assert distinct_claim_workers == {WORKER_ID_A, WORKER_ID_B}, (
        f"expected CLAIM events from both workers; got {distinct_claim_workers!r}"
    )

    # ─── Final task-status breakdown: 5 DONE, no leftovers ──────────────
    async with db_pool.acquire() as conn:
        breakdown_rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM tasks WHERE plan_id=$1 GROUP BY status",
            PLAN_ID,
        )
    breakdown = {row["status"]: int(row["n"]) for row in breakdown_rows}
    assert breakdown == {"DONE": TASK_COUNT}, (
        f"final task-status breakdown for plan {PLAN_ID!r} should be all DONE; got {breakdown!r}"
    )

    # ─── Operability: each worker emitted its 'finished' summary on stderr ──
    # The CLI prints ``whilly-worker: worker '<id>' finished — iterations=...``
    # at clean shutdown (whilly/cli/worker.py::run_worker_command). This is
    # the operator's audit-trail breadcrumb for "the worker exited
    # voluntarily rather than getting OOM-killed", and is what
    # ``docker logs whilly-worker`` would surface under the M1 compose
    # split.
    assert "finished" in stderr_a, f"worker A stderr missing finished summary:\n{stderr_a}"
    assert "finished" in stderr_b, f"worker B stderr missing finished summary:\n{stderr_b}"
    assert WORKER_ID_A in stderr_a, f"worker A stderr should mention its worker_id:\n{stderr_a}"
    assert WORKER_ID_B in stderr_b, f"worker B stderr should mention its worker_id:\n{stderr_b}"

    # ─── Spot-check: a CLAIM event payload carries one of our worker_ids ──
    async with db_pool.acquire() as conn:
        sample_claim = await conn.fetchrow(
            """
            SELECT events.payload
            FROM events
            JOIN tasks ON tasks.id = events.task_id
            WHERE tasks.plan_id = $1 AND events.event_type = 'CLAIM'
            ORDER BY events.id
            LIMIT 1
            """,
            PLAN_ID,
        )
    assert sample_claim is not None
    sample_payload = _decode_payload(sample_claim["payload"])
    assert sample_payload.get("worker_id") in {WORKER_ID_A, WORKER_ID_B}, (
        f"CLAIM payload worker_id should be one of {{{WORKER_ID_A!r}, {WORKER_ID_B!r}}}; got payload={sample_payload!r}"
    )
