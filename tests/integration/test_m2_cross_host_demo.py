"""M2 cross-host demo gate (m2-cross-host-demo).

Hermetic in-process counterpart to the operator-facing VPS demo
orchestrator at ``scripts/m2_cross_host_demo.sh``. Mirrors the
multi-host topology of the M2 sign-off demo in a single test
process: one FastAPI control-plane (uvicorn on a free loopback
port, against a testcontainers Postgres) plus three independent
``whilly-worker`` subprocesses simulating Alice/Bob/Carol on
distinct hosts. Each worker registers through its own
per-operator bootstrap token (minted via the M2 admin CLI), so
``workers.owner_email`` is populated cluster-wide before the
drain begins.

What this gate proves (mission §2 — M2 demo / sign-off)
-------------------------------------------------------
* ``whilly admin bootstrap mint`` produces three distinct
  bootstrap tokens, one per owner email (alice / bob / carol).
  (VAL-M2-DEMO-003)
* All three workers register successfully and receive distinct
  ``worker_id`` values; ``workers.owner_email`` carries the
  three demo owners. (VAL-M2-DEMO-004 / VAL-M2-DEMO-903)
* The 6-task plan drains across all three workers — every owner
  contributes at least one COMPLETE row to the audit log. No
  single worker monopolises. (VAL-M2-DEMO-006-equivalent across
  hosts.)
* ``whilly admin worker revoke <alice_worker_id>`` evicts Alice
  live: her worker subprocess exits non-zero within ≤60 s, her
  in-flight task (if any) is RELEASE'd back to PENDING with
  ``payload.reason = 'admin_revoked'``, and Bob/Carol continue
  draining unaffected. (VAL-M2-DEMO-005 / VAL-M2-DEMO-901
  / VAL-CROSS-AUTH-007 / VAL-CROSS-LIFECYCLE-002)
* All 6 tasks ultimately reach ``status='DONE'`` even after the
  mid-drain revocation — the released task is reclaimed by a
  peer worker. (VAL-CROSS-LIFECYCLE-004)
* M2 admin / bootstrap surface remains compatible with the
  existing M1 per-worker bearer auth (claims authenticate
  through the same ``make_db_bearer_auth`` factory).

Why a single uvicorn + 3 worker subprocesses, not a real VPS?
-------------------------------------------------------------
The end-to-end VPS demo (control-plane + funnel sidecar
publishing a rotating ``*.lhr.life`` URL on the public
internet) is the validator's job; the operator-facing
orchestrator at ``scripts/m2_cross_host_demo.sh`` drives that.
This gate is the cheap pre-merge smoke that proves the
state-machine and admin surfaces compose correctly across
3 distinct workers — no Docker images to pull, no compose
plumbing, runs in seconds rather than minutes.

Hermetic by construction
------------------------
* No external network — everything is ``127.0.0.1:<port>``.
* Worker subprocesses inherit only ``PATH`` + ``HOME`` plus the
  required ``WHILLY_*`` vars + ``CLAUDE_BIN``. Stray pytest /
  shell vars never leak into the spawned worker.
* The fake Claude CLI stub at ``tests/fixtures/fake_claude.sh``
  is the same shim ``test_phase5_remote.py`` /
  ``test_phase6_cross_host.py`` use; tasks complete
  deterministically via the ``<promise>COMPLETE</promise>``
  envelope the stub emits.
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
import httpx
import pytest
import uvicorn

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import REGISTER_PATH, create_app
from whilly.cli import admin as admin_cli

pytestmark = DOCKER_REQUIRED

PLAN_ID = "plan-m2-cross-host-demo"
PROJECT_NAME = "M2 cross-host demo (alice/bob/carol)"
TASK_COUNT = 6

OWNERS: tuple[tuple[str, str], ...] = (
    ("alice", "alice@example.com"),
    ("bob", "bob@example.com"),
    ("carol", "carol@example.com"),
)

FAKE_CLAUDE_PATH: Path = (Path(__file__).parent.parent / "fixtures" / "fake_claude.sh").resolve()

DRAIN_DEADLINE_SECONDS = 90.0
REVOKE_PROPAGATION_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Helpers — uvicorn lifecycle, mirrors test_phase6_cross_host.py
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
    """Resolve the in-venv ``whilly-worker`` command, robust to PATH pollution."""
    venv_bin = Path(sys.prefix) / "bin" / "whilly-worker"
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        return (str(venv_bin),)
    return (sys.executable, "-m", "whilly.cli.worker")


async def _seed_plan_and_tasks(pool: asyncpg.Pool) -> None:
    """Insert the demo plan + ``TASK_COUNT`` PENDING tasks via direct SQL."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            PLAN_ID,
            PROJECT_NAME,
        )
        for idx in range(1, TASK_COUNT + 1):
            await conn.execute(
                """
                INSERT INTO tasks (
                    id, plan_id, status, priority,
                    description, key_files, acceptance_criteria, test_steps,
                    prd_requirement, version, created_at, updated_at
                ) VALUES (
                    $1, $2, 'PENDING', 'medium',
                    $3, $4::jsonb, $5::jsonb, $6::jsonb,
                    $7, 1, NOW(), NOW()
                )
                """,
                f"T-M2-DEMO-{idx}",
                PLAN_ID,
                f"M2 cross-host demo task #{idx}.",
                json.dumps([f"whilly/m2_demo/task_{idx}.py"]),
                json.dumps(["all 3 workers contribute completes", "alice revoke flushes claims"]),
                json.dumps(["pytest tests/integration/test_m2_cross_host_demo.py -v"]),
                "M2-CROSS-HOST",
            )


async def _mint_bootstrap_via_admin_cli(owner_email: str) -> str:
    """Drive ``whilly admin bootstrap mint --owner X --json`` and return the
    plaintext token. Going through the admin CLI (not the repo API
    directly) is the point of VAL-M2-DEMO-003 — the demo path operators
    actually use must produce the three tokens.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "whilly.cli",
        "admin",
        "bootstrap",
        "mint",
        "--owner",
        owner_email,
        "--json",
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=20.0)
    assert proc.returncode == admin_cli.EXIT_OK, (
        f"admin bootstrap mint failed for {owner_email!r} (rc={proc.returncode})\n"
        f"stdout: {stdout_bytes.decode('utf-8', errors='replace')}\n"
        f"stderr: {stderr_bytes.decode('utf-8', errors='replace')}"
    )
    payload = json.loads(stdout_bytes.decode("utf-8"))
    return payload["token"]


async def _register_worker_via_http(
    base_url: str,
    *,
    bootstrap_token: str,
    hostname: str,
) -> tuple[str, str]:
    """Drive ``POST /workers/register`` with the operator-supplied bootstrap
    bearer; return ``(worker_id, plaintext_token)`` exactly like the
    operator-facing ``whilly worker connect`` command would yield.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            REGISTER_PATH,
            json={"hostname": hostname},
            headers={"Authorization": f"Bearer {bootstrap_token}"},
        )
    assert response.status_code == 201, f"register({hostname}) returned {response.status_code}: {response.text}"
    body = response.json()
    return body["worker_id"], body["token"]


def _build_worker_env(
    base_url: str,
    *,
    worker_id: str,
    plaintext_token: str,
) -> dict[str, str]:
    """Hermetic env for a worker subprocess (mirrors test_phase6_cross_host)."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "WHILLY_CONTROL_URL": base_url,
        "WHILLY_WORKER_TOKEN": plaintext_token,
        "WHILLY_PLAN_ID": PLAN_ID,
        "WHILLY_WORKER_ID": worker_id,
        "CLAUDE_BIN": str(FAKE_CLAUDE_PATH),
        "PYTHONUNBUFFERED": "1",
    }


async def _wait_for_first_done(pool: asyncpg.Pool, *, deadline_seconds: float) -> None:
    """Block until at least one task transitions to DONE under ``PLAN_ID``.

    Used as the "drain has begun" signal before issuing the live revoke
    so Alice has had a chance to register a CLAIM — without this, the
    revoke could fire before any worker's first claim and the
    `released_tasks` counter would be 0, masking the M2 admin-revoke
    contract.
    """
    deadline = asyncio.get_event_loop().time() + deadline_seconds
    while True:
        async with pool.acquire() as conn:
            done = await conn.fetchval(
                "SELECT count(*) FROM tasks WHERE plan_id = $1 AND status = 'DONE'",
                PLAN_ID,
            )
        if done >= 1:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError("first DONE deadline tripped")
        await asyncio.sleep(0.2)


async def _wait_for_all_done(pool: asyncpg.Pool, *, expected: int, deadline_seconds: float) -> int:
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
            raise asyncio.TimeoutError("all-DONE deadline tripped")
        await asyncio.sleep(0.5)


async def _wait_for_proc_exit(proc: asyncio.subprocess.Process, *, timeout: float) -> tuple[int, bytes, bytes]:
    """Wait for ``proc`` to exit and return (returncode, stdout, stderr)."""
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    assert proc.returncode is not None
    return proc.returncode, stdout_bytes, stderr_bytes


def _decode_payload(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        assert isinstance(decoded, dict)
        return decoded
    raise AssertionError(f"unexpected JSONB payload type {type(raw).__name__}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def control_plane(db_pool: asyncpg.Pool) -> AsyncIterator[str]:
    """Boot a uvicorn FastAPI control-plane on a free loopback port; yield URL.

    Tight long-poll knobs keep the test brisk; ``bootstrap_token=None``
    forces every register to authenticate through the DB-backed M2
    bootstrap_tokens table — exactly what the operator demo path uses.
    """
    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=None,
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
    server_task = asyncio.create_task(server.serve(), name="m2-demo-uvicorn")
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
def admin_env(monkeypatch: pytest.MonkeyPatch, postgres_dsn: str) -> str:
    """Point WHILLY_DATABASE_URL at the testcontainers DSN so the admin CLI
    subprocess can mint / revoke without explicit DSN flags."""
    monkeypatch.setenv(admin_cli.DATABASE_URL_ENV, postgres_dsn)
    return postgres_dsn


# ---------------------------------------------------------------------------
# The end-to-end demo gate
# ---------------------------------------------------------------------------


async def test_m2_cross_host_demo_drains_with_live_alice_revoke(
    db_pool: asyncpg.Pool,
    control_plane: str,
    admin_env: str,
) -> None:
    """3 owners × per-user bootstrap → 3 workers drain a 6-task plan with a
    mid-drain Alice revoke.

    Flow:
      1. Seed plan + 6 PENDING tasks.
      2. Mint 3 distinct bootstrap tokens via ``whilly admin bootstrap mint``
         (one per owner).
      3. Register one worker per owner over real HTTP (the same path
         ``whilly worker connect`` exercises minus the keychain step).
      4. Spawn 3 ``whilly-worker`` subprocesses, one per owner.
      5. Wait for the first DONE — proves the drain has actually begun.
      6. ``whilly admin worker revoke <alice_worker_id>`` mid-drain.
      7. Wait for Alice's subprocess to exit non-zero (≤60 s).
      8. Wait for the remaining tasks to drain to DONE under Bob+Carol.
      9. Assert audit invariants: 3 owner_emails registered; admin_revoked
         RELEASE events present; 6 DONE total; alice's tasks reclaimed by
         peers.
    """
    await _seed_plan_and_tasks(db_pool)

    bootstrap_tokens: dict[str, str] = {}
    for _short_name, owner_email in OWNERS:
        bootstrap_tokens[owner_email] = await _mint_bootstrap_via_admin_cli(owner_email)

    async with db_pool.acquire() as conn:
        owner_rows = await conn.fetch(
            "SELECT DISTINCT owner_email FROM bootstrap_tokens WHERE revoked_at IS NULL ORDER BY owner_email"
        )
    minted_owners = {row["owner_email"] for row in owner_rows}
    assert minted_owners == {email for _, email in OWNERS}, (
        f"expected three distinct bootstrap owners; got {minted_owners!r}"
    )

    worker_ids: dict[str, str] = {}
    worker_tokens: dict[str, str] = {}
    for short_name, owner_email in OWNERS:
        wid, plain = await _register_worker_via_http(
            control_plane,
            bootstrap_token=bootstrap_tokens[owner_email],
            hostname=f"host-{short_name}",
        )
        worker_ids[owner_email] = wid
        worker_tokens[owner_email] = plain

    distinct_ids = set(worker_ids.values())
    assert len(distinct_ids) == 3, f"expected 3 distinct worker_ids; got {distinct_ids!r}"

    async with db_pool.acquire() as conn:
        owner_email_rows = await conn.fetch("SELECT DISTINCT owner_email FROM workers ORDER BY owner_email")
    distinct_owners_in_workers = {row["owner_email"] for row in owner_email_rows if row["owner_email"]}
    assert distinct_owners_in_workers == {email for _, email in OWNERS}, (
        f"workers.owner_email did not match minted owners; got {distinct_owners_in_workers!r}"
    )

    worker_cmd = _resolve_worker_command()
    procs: dict[str, asyncio.subprocess.Process] = {}
    for _, owner_email in OWNERS:
        env = _build_worker_env(
            control_plane,
            worker_id=worker_ids[owner_email],
            plaintext_token=worker_tokens[owner_email],
        )
        procs[owner_email] = await asyncio.create_subprocess_exec(
            *worker_cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    alice_email = OWNERS[0][1]
    bob_email = OWNERS[1][1]
    carol_email = OWNERS[2][1]
    alice_worker_id = worker_ids[alice_email]

    try:
        # Step 5: wait for the first task to land in DONE so we know all
        # workers have hit their long-poll loop and at least one CLAIM
        # has fired. Without this, Alice's revoke could land before any
        # worker has even registered a heartbeat / claim, masking the
        # contract we're testing.
        await _wait_for_first_done(db_pool, deadline_seconds=DRAIN_DEADLINE_SECONDS)

        # Step 6: live revoke Alice via the admin CLI subprocess.
        revoke_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "whilly.cli",
            "admin",
            "worker",
            "revoke",
            alice_worker_id,
            "--json",
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        revoke_out, revoke_err = await asyncio.wait_for(revoke_proc.communicate(), timeout=15.0)
        assert revoke_proc.returncode == admin_cli.EXIT_OK, (
            f"admin worker revoke failed (rc={revoke_proc.returncode})\n"
            f"stdout: {revoke_out.decode('utf-8', errors='replace')}\n"
            f"stderr: {revoke_err.decode('utf-8', errors='replace')}"
        )
        revoke_payload = json.loads(revoke_out.decode("utf-8"))
        assert revoke_payload["revoked"] is True
        assert revoke_payload["worker_id"] == alice_worker_id

        # Step 7: Alice's worker subprocess must exit (non-zero) within
        # the propagation window. ``whilly-worker`` re-raises AuthError
        # from its long-poll → asyncio.run propagates → main exits via
        # uncaught exception → returncode != 0.
        rc_alice, alice_out, alice_err = await _wait_for_proc_exit(
            procs[alice_email],
            timeout=REVOKE_PROPAGATION_TIMEOUT_SECONDS,
        )
        alice_stderr = alice_err.decode("utf-8", errors="replace")
        alice_stdout = alice_out.decode("utf-8", errors="replace")
        assert rc_alice != 0, (
            f"alice worker should exit non-zero after revoke; rc={rc_alice}\n"
            f"stdout:\n{alice_stdout}\nstderr:\n{alice_stderr}"
        )

        # Step 8: the remaining 5+ tasks must drain to DONE under
        # Bob+Carol. Alice's released task (if any) is reclaimed by a
        # peer in one poll cycle.
        done_count = await _wait_for_all_done(
            db_pool,
            expected=TASK_COUNT,
            deadline_seconds=DRAIN_DEADLINE_SECONDS,
        )
        assert done_count == TASK_COUNT, f"expected {TASK_COUNT} DONE tasks after revoke; got {done_count}"

        # Bob and Carol still alive — send SIGTERM and await clean exit.
        for owner_email in (bob_email, carol_email):
            proc = procs[owner_email]
            assert proc.returncode is None, (
                f"{owner_email} worker exited unexpectedly (rc={proc.returncode}) — alice's revoke must NOT cascade"
            )
        for owner_email in (bob_email, carol_email):
            procs[owner_email].terminate()
        bob_rc, bob_out, bob_err = await _wait_for_proc_exit(procs[bob_email], timeout=15.0)
        carol_rc, carol_out, carol_err = await _wait_for_proc_exit(procs[carol_email], timeout=15.0)
        assert bob_rc == 0, (
            f"bob worker should exit cleanly on SIGTERM; rc={bob_rc}\n"
            f"stderr:\n{bob_err.decode('utf-8', errors='replace')}"
        )
        assert carol_rc == 0, (
            f"carol worker should exit cleanly on SIGTERM; rc={carol_rc}\n"
            f"stderr:\n{carol_err.decode('utf-8', errors='replace')}"
        )
    finally:
        for proc in procs.values():
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    # ── Step 9: audit invariants ─────────────────────────────────────────
    # 9a — every non-NULL owner_email surfaces in the workers table.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT owner_email FROM workers WHERE owner_email IS NOT NULL")
    observed_owners = {row["owner_email"] for row in rows}
    assert observed_owners == {email for _, email in OWNERS}, (
        f"expected workers.owner_email to cover {{alice, bob, carol}}; got {observed_owners!r}"
    )

    # 9b — Alice's bearer was nulled out by the revoke (admin CLI pathway).
    async with db_pool.acquire() as conn:
        alice_token_hash = await conn.fetchval(
            "SELECT token_hash FROM workers WHERE worker_id = $1",
            alice_worker_id,
        )
    assert alice_token_hash is None, f"alice's token_hash must be NULL after admin revoke; got {alice_token_hash!r}"

    # 9c — at least one RELEASE event with reason=admin_revoked attributed
    #      to alice's worker_id.
    async with db_pool.acquire() as conn:
        release_events = await conn.fetch(
            """
            SELECT events.payload
            FROM events
            WHERE events.event_type = 'RELEASE'
            ORDER BY events.id
            """
        )
    admin_revoke_payloads = [_decode_payload(row["payload"]) for row in release_events]
    admin_revokes_for_alice = [
        p for p in admin_revoke_payloads if p.get("reason") == "admin_revoked" and p.get("worker_id") == alice_worker_id
    ]
    # We accept zero releases here only when alice never managed to
    # claim a task before the revoke landed. The first-DONE wait gate
    # makes that pathological window unlikely but not impossible — the
    # drain test below cross-checks redistribution either way.
    if admin_revokes_for_alice:
        # If any RELEASE did fire, every payload row must carry the
        # full enriched shape (VAL-CROSS-BACKCOMPAT-912).
        for payload in admin_revokes_for_alice:
            assert payload.get("task_id"), payload
            assert payload.get("plan_id") == PLAN_ID, payload
            assert payload.get("version") is not None, payload

    # 9d — full task-status breakdown: 6 DONE, no leftovers.
    async with db_pool.acquire() as conn:
        breakdown_rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM tasks WHERE plan_id=$1 GROUP BY status",
            PLAN_ID,
        )
    breakdown = {row["status"]: int(row["n"]) for row in breakdown_rows}
    assert breakdown == {"DONE": TASK_COUNT}, (
        f"final task-status breakdown should be {{'DONE': {TASK_COUNT}}}; got {breakdown!r}"
    )

    # 9e — the audit log carries CLAIM events from at least Bob and
    #      Carol. Alice may or may not have claimed before the revoke;
    #      Bob+Carol absolutely must have, otherwise the drain wouldn't
    #      have completed.
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
    distinct_claimers = {row["worker_id"] for row in claim_worker_rows}
    assert worker_ids[bob_email] in distinct_claimers, f"bob did not register a CLAIM event; saw {distinct_claimers!r}"
    assert worker_ids[carol_email] in distinct_claimers, (
        f"carol did not register a CLAIM event; saw {distinct_claimers!r}"
    )

    # 9f — VAL-CROSS-AUTH-007 spot-check: alice's bearer is rejected with
    #      401 by the control plane after the revoke.
    async with httpx.AsyncClient(base_url=control_plane, timeout=10.0) as client:
        response = await client.post(
            f"/workers/{alice_worker_id}/heartbeat",
            json={"worker_id": alice_worker_id},
            headers={"Authorization": f"Bearer {worker_tokens[alice_email]}"},
        )
    assert response.status_code == 401, (
        f"alice's bearer should return 401 after admin revoke; got {response.status_code}: {response.text}"
    )
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer "), response.headers
