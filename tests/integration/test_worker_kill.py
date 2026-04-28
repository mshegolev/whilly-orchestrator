"""SIGKILL recovery integration test (TASK-026, PRD SC-2 / NFR-1 / FR-1.4).

This is the canonical "kill ``-9`` a worker, peer takes over" demonstration
the v4.0 fault-tolerance story rests on. Existing tests cover the building
blocks in isolation — :mod:`tests.integration.test_visibility_timeout`
exercises the sweep against a hand-seeded stale-claim row,
:mod:`tests.integration.test_worker_signals` proves cooperative SIGTERM
shutdown via :func:`os.kill` against the test process — but neither pins
the *combined* scenario the PRD's SC-2 names: a real worker process holds a
real claim, the OS kills it without notice, and a peer process is supposed
to pick up the orphaned task within seconds. Without this test, an SC-2
regression that broke the seam between a hard-killed worker and the
visibility-timeout sweep (the only mechanism that *can* recover such
claims) would slip through every other suite.

Acceptance criteria mapping (TASK-026)
--------------------------------------
* "Тест убивает subprocess воркера через ``os.kill(SIGKILL)``" →
  :func:`test_sigkill_worker_releases_claim_to_peer_within_visibility_timeout`
  spawns a worker subprocess via :class:`subprocess.Popen`, waits until the
  worker has actually claimed the seeded task (status flips to
  ``CLAIMED`` / ``IN_PROGRESS`` with the matching ``claimed_by``), then
  sends ``SIGKILL``. SIGKILL is uncatchable — the worker can't run its
  release / heartbeat-cleanup paths, which is the whole point: this test
  pins the *server-side* recovery, not a cooperative shutdown.
* "Задача автоматически re-claimed другим воркером в течение 30с (с
  ``visibility_timeout=10s`` в тесте)" → after the kill, the test brings
  up a FastAPI app with the visibility-timeout sweep tuned to the AC's
  ``visibility_timeout=10s`` (and a fast 0.5s sweep cadence so the test
  doesn't burn its full budget waiting on the production cadence). A peer
  worker (in-process, via :class:`TaskRepository`) reclaims the task
  inside the lifespan — proving the released row is actually consumable,
  not just sitting in a half-state.
* "``events`` содержит запись release с ``reason='visibility_timeout'``"
  → the post-recovery audit-log assertions read the ``events`` table and
  pin a ``RELEASE`` row whose ``payload['reason'] == 'visibility_timeout'``.
* "SC-2 demonstrated" → the test composes all three above in one
  end-to-end flow against a real Postgres + a real worker subprocess,
  which is exactly what SC-2 names.

Why a real subprocess rather than an in-process worker
------------------------------------------------------
The sibling :mod:`tests.integration.test_worker_signals` already covers
the in-process / cooperative shutdown path with ``os.kill(os.getpid(),
SIGTERM)`` and ``stop.set()``. SIGKILL is a categorically different
contract: the kernel reaps the process *immediately*, with no signal
handler dispatch and no coroutine cleanup. A test that flipped a
``stop`` event in-process or sent SIGTERM (which asyncio's loop would
intercept) would not exercise the same code path — it would prove the
cooperative path works, which we already know. Spawning ``python -c
<inline-script>`` and SIGKILL-ing its PID is the only way to drive the
"worker process literally vanishes" half of SC-2 from a Python test
runner.

Why we disable the offline-worker sweep instead of letting it race
------------------------------------------------------------------
TASK-025b's offline-worker sweep is faster than the visibility-timeout
sweep on heartbeats it can see — it would also release this task once
the seeded worker's ``last_heartbeat`` ages past ``heartbeat_timeout``.
Two sweeps competing for the same row is fine in production (whichever
fires first wins; the other's UPDATE finds no CLAIMED rows and no-ops),
but it would make this test's assertion about ``reason`` non-
deterministic — the AC pins ``visibility_timeout`` specifically.
Setting ``heartbeat_timeout_seconds=600`` parks the offline-worker
sweep for the test window (the worker's heartbeat is at most ~30s old
by the time we observe the release) so the visibility-timeout sweep is
unambiguously the writer. The offline-worker sweep gets its own
end-to-end coverage in :mod:`tests.integration.test_worker_offline`.

Why we run the worker via an inline ``python -c`` script
--------------------------------------------------------
``whilly run`` (the production composition root in :mod:`whilly.cli.run`)
is the natural choice, but it hard-codes :func:`whilly.adapters.runner.run_task`
as the agent runner — that wraps the real ``claude`` binary. Substituting
a stub via :func:`run_run_command`'s ``runner`` kwarg works in-process,
but doesn't help once we're across a process boundary. The cleanest
substitute is a small inline asyncio script that imports
:func:`whilly.worker.local.run_local_worker` directly and passes a stub
runner that simply ``await asyncio.sleep`` for an hour — the worker
will reach ``run_local_worker``'s ``runner`` call, claim the seeded
task, hang inside the stub, and stay there until SIGKILL. The script
is short enough that the Popen ``-c`` argument is more readable than a
sibling helper module would be, and the test's coupling to the
worker's public API (rather than to ``whilly run``'s argparse surface)
matches what TASK-025a / TASK-025b already do for the sweep tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap

import asyncpg
from fastapi import FastAPI

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.adapters.transport.server import create_app

pytestmark = DOCKER_REQUIRED

# Tokens are required by ``create_app`` to construct the auth dependencies
# even though no HTTP route is exercised here. Per-test literals avoid env
# pollution under parallel pytest workers.
_BOOTSTRAP_TOKEN = "bootstrap-tok-kill"
_WORKER_TOKEN = "worker-tok-kill"

# Sweep timing knobs. ``visibility_timeout=10s`` is the AC pin; the
# 0.5s sweep cadence keeps the test budget tight without making the
# loop pathologically hot. ``heartbeat_timeout=600s`` parks the
# offline-worker sweep so the visibility-timeout sweep is unambiguously
# the writer (see module docstring).
_SWEEP_INTERVAL = 0.5
_VISIBILITY_TIMEOUT = 10
_OFFLINE_SWEEP_INTERVAL = 0.5
_HEARTBEAT_TIMEOUT_DISABLED = 600

# AC budget: "в течение 30с". The release fires once ``claimed_at`` has
# aged past ``visibility_timeout``; we add headroom for sweep-tick jitter
# and the lifespan-startup latency.
_RECOVERY_BUDGET_SECONDS = 30.0
# Time we give the subprocess to come up, register, and claim the seeded
# task. The cold-import + pool-open cost dominates here; 15s is roomy.
_CLAIM_BUDGET_SECONDS = 20.0
# How long to wait for the SIGKILL'd subprocess to actually reap. Should
# be near-instant (kernel does the work) but we don't want a zombie
# process to outlive the test on a busy CI host.
_REAP_BUDGET_SECONDS = 5.0

_PLAN_ID = "PLAN-KILL-1"
_TASK_ID = "T-KILL-1"
_VICTIM_WORKER_ID = "w-victim"
_PEER_WORKER_ID = "w-peer"


# Inline subprocess script. Runs as ``python -c <this string>``.
# Reads the connection knobs from env vars (set by the test) so the
# subprocess and test agree on which Postgres + plan + worker_id to
# operate against. The runner stub blocks for an hour so SIGKILL is the
# only way out — a regression that returned early would surface as the
# task transitioning to DONE before the kill (caught by the post-claim
# status check below). ``PYTHONUNBUFFERED=1`` (set by the test env) makes
# the ``WORKER_CLAIMED:`` marker visible to the parent if it ever needs
# to debug a hang via stdout.
_WORKER_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os


    async def main() -> None:
        # Imports inside main() so that any ImportError surfaces on the
        # subprocess's stderr (which the test captures via Popen.stderr)
        # rather than as a silent zero-byte stdout.
        from whilly.adapters.db import TaskRepository, close_pool, create_pool
        from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
        from whilly.core.models import Plan
        from whilly.worker.local import run_local_worker

        dsn = os.environ["WHILLY_DATABASE_URL"]
        plan_id = os.environ["WHILLY_PLAN_ID"]
        worker_id = os.environ["WHILLY_WORKER_ID"]

        pool = await create_pool(dsn)
        try:
            async with pool.acquire() as conn:
                # Idempotent registration mirroring whilly.cli.run's
                # _REGISTER_WORKER_SQL — a re-run with the same id refreshes
                # last_heartbeat, which doesn't matter here (the test seeds
                # a single victim) but keeps the SQL aligned with prod.
                await conn.execute(
                    "INSERT INTO workers (worker_id, hostname, token_hash) "
                    "VALUES ($1, 'kill-test', 'local') "
                    "ON CONFLICT (worker_id) DO UPDATE SET last_heartbeat = NOW()",
                    worker_id,
                )

            repo = TaskRepository(pool)
            plan = Plan(id=plan_id, name=plan_id)

            async def stuck_runner(task, prompt):
                # Marker that lets the test prove (via stdout polling) the
                # runner actually entered. The test currently uses the
                # tasks-table state instead, but the marker is cheap and
                # invaluable for diagnosing flakes locally.
                print(f"WORKER_CLAIMED:{task.id}", flush=True)
                # 3600s far exceeds any reasonable test budget — the
                # only way out is SIGKILL.
                await asyncio.sleep(3600)
                # Defensive: a regression that broke asyncio.sleep (or
                # that ran cleanup before the kill) would fall through.
                # AgentResult here would route to complete_task and
                # invalidate the test; an unreachable raise is louder.
                return AgentResult(
                    usage=AgentUsage(),
                    exit_code=0,
                    is_complete=True,
                    output="unreachable",
                )

            # idle_wait kept tight so the worker doesn't sit idle if the
            # claim happens to lose a race (it shouldn't — the test is
            # the only claimer in this DB — but being deterministic is
            # cheap).
            await run_local_worker(
                repo, stuck_runner, plan, worker_id, idle_wait=0.05,
            )
        finally:
            # SIGKILL skips the finally entirely; this branch matters only
            # for clean exits (e.g. the test aborting via SIGTERM after a
            # claim-budget timeout).
            await close_pool(pool)


    asyncio.run(main())
    """
)


async def _seed_plan_with_one_task(pool: asyncpg.Pool, plan_id: str, task_id: str) -> None:
    """Insert a plan with exactly one PENDING task ready to claim.

    Mirrors :mod:`tests.integration.test_local_worker`'s seed helper —
    one canonical seed shape across the integration suite means a schema
    regression (e.g. a new NOT NULL column) surfaces consistently.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            plan_id,
            f"plan-{plan_id}",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'high', $3, '[]'::jsonb, '[]'::jsonb, 'SC-2', 0)
            """,
            task_id,
            plan_id,
            "kill-test task — must be reclaimable after victim SIGKILL",
        )


async def _seed_peer_worker(pool: asyncpg.Pool, worker_id: str) -> None:
    """Insert the peer's workers row directly.

    The peer never goes through the FastAPI registration RPC — the test
    is exercising the recovery half of SC-2 (visibility-timeout sweep
    releases, peer claims), not registration. A direct INSERT keeps the
    test on the wire-level DB contract and avoids dragging the HTTP
    transport into the assertion surface.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) "
            "VALUES ($1, $2, $3) ON CONFLICT (worker_id) DO NOTHING",
            worker_id,
            f"host-{worker_id}",
            "peer-token-placeholder",
        )


async def _wait_for_claim(pool: asyncpg.Pool, task_id: str, worker_id: str, timeout: float) -> None:
    """Poll the tasks row until ``claimed_by == worker_id``.

    Accepts both ``CLAIMED`` and ``IN_PROGRESS`` because the local worker
    advances through both states as a tight pair (claim → start) and the
    test happens to look at any moment in that window. We *don't* accept
    ``DONE`` — that would mean the runner returned, which the stuck
    runner is supposed to never do.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_status: str | None = None
    last_claimed_by: str | None = None
    while loop.time() < deadline:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, claimed_by FROM tasks WHERE id = $1",
                task_id,
            )
        if row is not None:
            last_status = row["status"]
            last_claimed_by = row["claimed_by"]
            if row["claimed_by"] == worker_id and row["status"] in {"CLAIMED", "IN_PROGRESS"}:
                return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"task {task_id} not claimed by {worker_id} within {timeout}s "
        f"(last seen status={last_status!r}, claimed_by={last_claimed_by!r})"
    )


async def _wait_for_status(pool: asyncpg.Pool, task_id: str, expected_status: str, timeout: float) -> asyncpg.Record:
    """Poll until the task reaches ``expected_status`` or the budget elapses.

    Returns the matching row so the caller can assert on the post-update
    columns (``claimed_by``, ``version``) without a second SELECT.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_status: str | None = None
    while loop.time() < deadline:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, claimed_by, claimed_at, version FROM tasks WHERE id = $1",
                task_id,
            )
        if row is not None:
            last_status = row["status"]
            if row["status"] == expected_status:
                return row
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"task {task_id} did not reach {expected_status} within {timeout}s (last seen status={last_status!r})"
    )


async def test_sigkill_worker_releases_claim_to_peer_within_visibility_timeout(
    db_pool: asyncpg.Pool,
    postgres_dsn: str,
) -> None:
    """SIGKILL'd worker → visibility-timeout sweep → peer reclaims (SC-2).

    End-to-end flow:

    1. Seed plan + one PENDING task.
    2. Spawn a worker subprocess that claims the task and hangs in a
       stuck runner.
    3. SIGKILL the subprocess once we observe the claim in the DB.
    4. Stand up the FastAPI lifespan (visibility_timeout=10s, fast
       sweep cadence, offline-worker sweep parked).
    5. Wait up to 30s for the task to flip back to PENDING.
    6. Have a peer worker claim it via :class:`TaskRepository.claim_task`
       — the SC-2 "peer takes over" outcome.
    7. Assert the audit log carries a RELEASE event with
       ``reason='visibility_timeout'``.

    Each step's failure attribution is intentionally distinct: a hang in
    step 2 surfaces as a ``_wait_for_claim`` AssertionError naming the
    last-seen status; a regression that broke the sweep surfaces as a
    ``_wait_for_status('PENDING')`` timeout; a regression in the peer-
    claim path surfaces on the ``peer_claim is None`` assertion. We
    deliberately do *not* fold these into a single composite check —
    individual failure points are the entire reason this test exists
    over a black-box smoke.
    """
    await _seed_plan_with_one_task(db_pool, _PLAN_ID, _TASK_ID)

    # Pass the DSN, plan id, and worker id to the subprocess via env.
    # Argparse would also work, but env-var passing matches how
    # ``whilly run`` is configured in production (WHILLY_DATABASE_URL,
    # WHILLY_WORKER_ID — see whilly.cli.run) so the subprocess script
    # stays close to the prod composition root's contract.
    env = os.environ.copy()
    env["WHILLY_DATABASE_URL"] = postgres_dsn
    env["WHILLY_PLAN_ID"] = _PLAN_ID
    env["WHILLY_WORKER_ID"] = _VICTIM_WORKER_ID
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-c", _WORKER_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Wait for the subprocess to actually claim the task. ``to_thread``
        # isn't needed — the wait is async-poll over the pool the test
        # already owns.
        try:
            await _wait_for_claim(
                db_pool,
                _TASK_ID,
                _VICTIM_WORKER_ID,
                timeout=_CLAIM_BUDGET_SECONDS,
            )
        except AssertionError:
            # Drain whatever the subprocess wrote so the failure message
            # carries actionable diagnostics. Common failure modes:
            # ImportError (whilly not installed in the subprocess's env),
            # asyncpg connection refused (DSN drift), schema mismatch.
            proc.kill()
            stdout, stderr = proc.communicate(timeout=_REAP_BUDGET_SECONDS)
            raise AssertionError(
                "worker subprocess did not claim the task within "
                f"{_CLAIM_BUDGET_SECONDS}s.\n"
                f"--- subprocess stdout ---\n{stdout.decode(errors='replace')}\n"
                f"--- subprocess stderr ---\n{stderr.decode(errors='replace')}"
            ) from None

        # Sanity: subprocess is still alive at the moment we send the
        # kill. A regression that crashed the worker before claim would
        # have been caught above; this guards the narrower "process
        # exited cleanly between claim and kill" race.
        assert proc.poll() is None, (
            f"worker subprocess unexpectedly exited before SIGKILL (returncode={proc.returncode!r})"
        )

        # SIGKILL — the canonical TASK-026 "kill -9" gesture. Uncatchable;
        # the worker's heartbeat / release / pool-close paths cannot run.
        # That's the entire point: any recovery from here on is the
        # *server's* responsibility (visibility-timeout sweep).
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=_REAP_BUDGET_SECONDS)
        # SIGKILL exits with -9 on POSIX. Any other code would mean we
        # raced with a clean exit (which the stuck runner forbids).
        assert proc.returncode == -signal.SIGKILL, (
            f"expected SIGKILL exit (-{int(signal.SIGKILL)}), got returncode={proc.returncode!r}"
        )
    finally:
        # Defensive cleanup — covers the path where an assertion above
        # raised before we reached ``proc.wait``. ``poll()`` is None iff
        # the process is still running.
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=_REAP_BUDGET_SECONDS)
            except subprocess.TimeoutExpired:
                pass

    # Now bring up the visibility-timeout sweep. The lifespan is created
    # *after* the kill so the sweep doesn't fire mid-claim and produce
    # a confounding RELEASE event before the kill — see module docstring
    # for the timing rationale.
    app: FastAPI = create_app(
        db_pool,
        worker_token=_WORKER_TOKEN,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        sweep_interval_seconds=_SWEEP_INTERVAL,
        visibility_timeout_seconds=_VISIBILITY_TIMEOUT,
        offline_worker_sweep_interval_seconds=_OFFLINE_SWEEP_INTERVAL,
        heartbeat_timeout_seconds=_HEARTBEAT_TIMEOUT_DISABLED,
    )

    async with app.router.lifespan_context(app):
        # The sweep needs ``claimed_at`` to age past visibility_timeout
        # (10s). With sweep_interval=0.5s, the next tick after the
        # threshold lands within ~10.5s of the original claim. The 30s
        # budget gives 3x headroom for slow CI hosts.
        released_row = await _wait_for_status(db_pool, _TASK_ID, "PENDING", timeout=_RECOVERY_BUDGET_SECONDS)
        assert released_row["claimed_by"] is None, (
            f"sweep flipped status but left claimed_by={released_row['claimed_by']!r}"
        )
        assert released_row["claimed_at"] is None, (
            "sweep flipped status but left claimed_at populated; tasks.ck_claim_pair_consistent should have caught this"
        )
        # version 0 → 1 (claim) → 2 (sweep release). start_task may or
        # may not have landed depending on how far the worker got
        # before SIGKILL; either way the sweep advances the version,
        # so the post-release version is strictly > the pre-claim
        # version of 0. Pinning ``> 0`` (rather than ``== 2``) keeps
        # the test stable against the start_task-landed / didn't-land
        # race.
        assert released_row["version"] > 0, f"version did not advance through release: {released_row['version']}"

        # Peer claim — the SC-2 outcome the AC names. We seed the peer's
        # workers row first because TaskRepository.claim_task assumes
        # the FK target exists (HTTP register flow is out of scope here).
        await _seed_peer_worker(db_pool, _PEER_WORKER_ID)
        repo = TaskRepository(db_pool)
        peer_claim = await repo.claim_task(_PEER_WORKER_ID, _PLAN_ID)

    assert peer_claim is not None, (
        "peer worker could not claim the released task — visibility-timeout "
        "sweep flipped the row but left it un-claimable; check the SQL in "
        "TaskRepository.release_stale_claims for a bad UPDATE shape"
    )
    assert peer_claim.id == _TASK_ID, f"peer claimed an unexpected task: {peer_claim.id!r} (expected {_TASK_ID!r})"

    # Audit log: a RELEASE event with reason='visibility_timeout' must
    # exist. We allow ``>= 1`` (rather than ``== 1``) because in theory
    # nothing forbids a future sweep tick from generating a redundant
    # release if the row's already-PENDING; today the SQL filters that
    # out (and the idempotency property is pinned in
    # :mod:`tests.integration.test_visibility_timeout`), but pinning
    # exact equality here would couple this test to that filter rather
    # than to the SC-2 contract this test owns.
    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id ASC",
            _TASK_ID,
        )

    release_events = [e for e in events if e["event_type"] == "RELEASE"]
    assert len(release_events) >= 1, (
        f"expected at least one RELEASE event after sweep; got {[e['event_type'] for e in events]}"
    )
    payload = json.loads(release_events[0]["payload"])
    assert payload["reason"] == "visibility_timeout", (
        f"expected reason='visibility_timeout' (the AC pin), got "
        f"payload={payload!r}. If reason='worker_offline' shows up here, "
        f"the offline-worker sweep beat the visibility-timeout sweep — "
        f"check that heartbeat_timeout_seconds is parked at "
        f"{_HEARTBEAT_TIMEOUT_DISABLED}s for this test."
    )

    # The full audit shape should also include the original CLAIM (and
    # possibly START) from the victim worker before the kill. We don't
    # assert on START specifically — start_task may have landed before
    # SIGKILL or not, and the test-stability rationale on ``version``
    # above applies here too.
    event_types = [e["event_type"] for e in events]
    assert "CLAIM" in event_types, (
        f"victim worker's CLAIM event missing — was the subprocess seed actually run? events={event_types!r}"
    )
