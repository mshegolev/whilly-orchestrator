"""Phase 6 resilience gate (TASK-028, PRD Day 6 deliverable).

Closes Phase 6 by composing the SC-2 fault-tolerance machinery
(visibility-timeout sweep, optimistic locking, peer reclaim) with the
TASK-027 dashboard read view into a single end-to-end flow that pins the
PRD's "5 tasks, 2 workers, one killed mid-task, all reach DONE,
dashboard never lies" contract::

    seed plan + 5 PENDING tasks
        -> spawn worker A (subprocess, hung runner) -> A claims task #1
            -> SIGKILL worker A
                -> FastAPI lifespan brings up visibility-timeout sweep
                    -> sweep flips A's task back to PENDING
                        -> dashboard checkpoint: 4 PENDING + 1 PENDING (formerly CLAIMED)
                            -> in-process worker B drains all 5 tasks
                                -> dashboard checkpoint: 5 DONE

Each individual link has its own focused suite already
(``tests/integration/test_worker_kill.py`` exhausts the SIGKILL → sweep
→ peer-reclaim trio for the *single-task* SC-2 demonstration;
``tests/integration/test_dashboard.py`` proves the read view returns the
right projection against any seeded shape; ``tests/integration/test_phase4_e2e.py``
proves a 3-task plan drains end-to-end through the real CLI). The point
of *this* test is to prove the links *compose at scale*: a real kill mid-
plan plus a real peer plus the real dashboard SELECT all line up against
a 5-task plan without any link silently degrading. A regression where
the sweep correctly released the orphan but the dashboard's projection
still read stale ``claimed_by`` (or vice versa) would slip past every
other suite — only this gate would catch it.

Acceptance criteria mapping (TASK-028)
--------------------------------------
* "5 задач, 2 worker'а, один убит" →
  :func:`test_phase6_resilience_kills_one_worker_and_completes_all_tasks`
  seeds 5 tasks (mixed priorities, no inter-task dependencies — pure
  parallel-claim contention is the realistic shape of "5 ready tasks";
  threading dependencies through would mean "1 ready task + 4 waiting"
  which doesn't exercise the AC). Worker A is a real subprocess running
  :func:`whilly.worker.local.run_local_worker` with a hung stub runner
  (mirrors :mod:`tests.integration.test_worker_kill`). Worker B is in-
  process, runs :func:`run_local_worker` with a fast-complete stub, and
  drains the rest after the sweep releases A's claim.
* "Все задачи финально DONE" → after the sweep + worker B's drain, the
  test SELECTs every row in the plan and asserts ``status='DONE'`` for
  all 5. Pinning ``claimed_by`` per row also proves the worker
  identities show up correctly (one task ends up claimed by B even
  though A originally took it).
* "Dashboard корректно отображает статус в любой момент" →
  :func:`fetch_dashboard_rows` is sampled at three checkpoints: (1)
  before the kill — the row A claimed reads ``CLAIMED`` /
  ``IN_PROGRESS`` with ``claimed_by=A``; (2) after the sweep but before
  B starts — the released row reads ``PENDING`` with ``claimed_by=None``;
  (3) after B drains — every row reads ``DONE`` and ``claimed_by`` is
  preserved per worker. The three samples together span "any moment" —
  any drift between the dashboard's projection and the underlying
  ``tasks`` table at any of these points fails the test.

Why we do *not* run worker B as a second subprocess
---------------------------------------------------
Two real subprocesses competing for claims would exercise SC-1's
concurrent-claim path on top of the SC-2 recovery path — but
:mod:`tests.integration.test_concurrent_claims` already pins SC-1 with
100 concurrent claimers. Stacking SC-1 onto this test would add
non-determinism (which worker wins which claim race) without
strengthening the SC-2 + dashboard gate this file exists for. An in-
process worker B is fully observable (we can drive it to drain
deterministically via ``max_iterations``) and the AC explicitly names
"один убит" — it does not name "both real subprocesses".

Why we use a hand-written in-process runner stub instead of fake_claude.sh
-------------------------------------------------------------------------
:mod:`tests.integration.test_phase4_e2e` already proves the CLI →
runner → fake_claude.sh → parser pipeline drains a multi-task plan.
Re-running that pipeline here would couple this test to the CLI surface
(argparse, ``CLAUDE_BIN`` env propagation) without adding signal —
the AC is about *resilience* (SC-2 + dashboard), not about the runner
boundary. A direct ``async def fast_runner`` stub keeps the test
focused on the state machine + DB + projection seam.

Why we set ``heartbeat_timeout_seconds`` very high
--------------------------------------------------
TASK-025b's offline-worker sweep would also release A's claim once A's
``last_heartbeat`` ages past ``heartbeat_timeout`` — but the AC pin in
the audit log is on the visibility-timeout sweep (consistent with
TASK-026's per-task SC-2 demonstration). Parking the offline-worker
sweep at a very large threshold makes the visibility-timeout sweep
unambiguously the writer of the RELEASE row this test asserts on. The
offline-worker sweep gets its own coverage in
:mod:`tests.integration.test_worker_offline`.
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
from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.adapters.transport.server import create_app
from whilly.cli.dashboard import fetch_dashboard_rows
from whilly.core.models import Plan, Priority, Task, TaskStatus
from whilly.worker.local import run_local_worker

pytestmark = DOCKER_REQUIRED

# Tokens are required by ``create_app`` to construct the auth dependencies
# even though no HTTP route is exercised here. Per-test literals avoid env
# pollution under parallel pytest workers.
_BOOTSTRAP_TOKEN = "bootstrap-tok-phase6"
_WORKER_TOKEN = "worker-tok-phase6"

# Sweep timing knobs. ``visibility_timeout=10s`` matches the cadence used
# by ``test_worker_kill`` so the recovery wall-clock cost stays predictable
# across the SC-2 suite. ``heartbeat_timeout=600s`` parks the offline-worker
# sweep so the visibility-timeout sweep is unambiguously the writer of the
# release row (see module docstring).
_SWEEP_INTERVAL = 0.5
_VISIBILITY_TIMEOUT = 10
_OFFLINE_SWEEP_INTERVAL = 0.5
_HEARTBEAT_TIMEOUT_DISABLED = 600

# Wall-clock budgets. ``_RECOVERY_BUDGET`` covers the
# claimed_at-age-past-visibility_timeout window plus sweep-tick jitter.
# ``_CLAIM_BUDGET`` covers the cold-import + pool-open cost of the
# subprocess. ``_REAP_BUDGET`` covers the SIGKILL → kernel-reap latency
# (near-instant in practice; we just don't want a zombie surviving the
# test on a busy host). ``_DRAIN_BUDGET`` is the per-call cap on
# worker B's ``run_local_worker`` invocation (5 tasks × ~50ms idle
# wait per iteration plus runner stub time = sub-second comfortably).
_RECOVERY_BUDGET_SECONDS = 30.0
_CLAIM_BUDGET_SECONDS = 20.0
_REAP_BUDGET_SECONDS = 5.0
_DRAIN_BUDGET_SECONDS = 30.0

_PLAN_ID = "PLAN-PHASE6"
_VICTIM_WORKER_ID = "w-phase6-victim"
_PEER_WORKER_ID = "w-phase6-peer"

# Five tasks, mixed priorities so the claim ordering (priority_rank, id)
# is meaningful. We pick CRITICAL × 1 (the one A grabs first), HIGH × 2,
# MEDIUM × 1, LOW × 1 — variety enough that a regression in the
# priority-aware claim SQL would surface as "B drained tasks in the
# wrong order" before the all-DONE assertion masked it.
_TASKS: tuple[tuple[str, str], ...] = (
    ("T-PHASE6-1", Priority.CRITICAL.value),
    ("T-PHASE6-2", Priority.HIGH.value),
    ("T-PHASE6-3", Priority.HIGH.value),
    ("T-PHASE6-4", Priority.MEDIUM.value),
    ("T-PHASE6-5", Priority.LOW.value),
)
_TASK_IDS: tuple[str, ...] = tuple(tid for tid, _ in _TASKS)
# The CRITICAL task is the one the victim subprocess will grab first —
# ORDER BY priority_rank, id is the contract from
# :data:`whilly.adapters.db.repository._CLAIM_SQL`. Pinning the expected
# id explicitly (rather than asserting "any one of them got claimed")
# means a regression in priority ordering surfaces here, not silently
# downstream when the wrong task gets stranded.
_EXPECTED_VICTIM_TASK = _TASK_IDS[0]


# Inline subprocess script. Same shape as
# :mod:`tests.integration.test_worker_kill` — runs as ``python -c <this
# string>`` so any ImportError surfaces on the subprocess's stderr (which
# the test captures via ``Popen.stderr``) rather than as a silent zero-
# byte stdout. The runner stub blocks for an hour so SIGKILL is the only
# way out — a regression that returned early would surface as the task
# transitioning to DONE before the kill (caught by the post-claim status
# check below).
_VICTIM_WORKER_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os


    async def main() -> None:
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
                # Idempotent registration — mirrors whilly.cli.run's
                # _REGISTER_WORKER_SQL so a schema regression on the
                # workers table surfaces consistently across tests.
                await conn.execute(
                    "INSERT INTO workers (worker_id, hostname, token_hash) "
                    "VALUES ($1, 'phase6-victim', 'local') "
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
                print(f"VICTIM_CLAIMED:{task.id}", flush=True)
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


async def _seed_plan_with_five_tasks(pool: asyncpg.Pool, plan_id: str) -> None:
    """Insert one plan row plus five PENDING tasks ready to be claimed.

    Mirrors :func:`tests.integration.test_worker_kill._seed_plan_with_one_task`
    extended for five tasks. We use a single transaction so a partial
    seed (e.g. only 3 tasks committed) cannot leave the test in a flaky
    in-between state that would mask a real bug under "all 5 DONE".
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                plan_id,
                f"phase6-{plan_id}",
            )
            for task_id, priority in _TASKS:
                await conn.execute(
                    """
                    INSERT INTO tasks (
                        id, plan_id, status, dependencies, key_files,
                        priority, description, acceptance_criteria,
                        test_steps, prd_requirement, version
                    )
                    VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                            $3, $4, '[]'::jsonb, '[]'::jsonb, 'SC-2', 0)
                    """,
                    task_id,
                    plan_id,
                    priority,
                    f"phase6 task {task_id} — must reach DONE through resilient peer reclaim",
                )


async def _seed_peer_worker(pool: asyncpg.Pool, worker_id: str) -> None:
    """Insert the peer worker's row directly.

    The peer never goes through the FastAPI registration RPC — the test
    is exercising the recovery half of SC-2 (visibility-timeout sweep
    releases, peer drains), not registration. A direct INSERT keeps the
    test on the wire-level DB contract and avoids dragging the HTTP
    transport surface into the assertion shape.
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
    advances through both as a tight pair (claim → start) and the test
    might observe either state. We *don't* accept ``DONE`` — that would
    mean the runner returned, which the stuck runner is supposed to
    never do.
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


async def _fast_complete_runner(task: Task, prompt: str) -> AgentResult:  # noqa: ARG001 — prompt is unused; the worker still requires the signature
    """In-process runner stub: instantly returns a successful completion.

    ``is_complete=True`` + ``exit_code=0`` is the contract the worker's
    outcome routing reads to flip ``IN_PROGRESS → DONE`` via
    :meth:`TaskRepository.complete_task`. The output string is plain
    text so a future regression that started parsing the runner's
    output for tool-use directives would not silently swallow it.

    See :func:`tests.integration.test_local_worker._fake_runner_complete`
    — same shape, kept local here so this file is readable on its own.
    """
    return AgentResult(
        usage=AgentUsage(),
        exit_code=0,
        is_complete=True,
        output=f"<promise>COMPLETE</promise> for {task.id}",
    )


async def test_phase6_resilience_kills_one_worker_and_completes_all_tasks(
    db_pool: asyncpg.Pool,
    postgres_dsn: str,
) -> None:
    """SC-2 at scale + dashboard: 5 tasks, victim killed, peer drains, projection stays correct.

    End-to-end flow (see module docstring for the link-by-link picture):

    1. Seed plan + 5 PENDING tasks with mixed priorities.
    2. Spawn worker A subprocess that claims the CRITICAL task and hangs
       inside a stuck runner.
    3. Dashboard checkpoint #1: 4 PENDING + 1 CLAIMED/IN_PROGRESS row,
       the latter with ``claimed_by=A``.
    4. SIGKILL worker A.
    5. Stand up the FastAPI lifespan (visibility_timeout=10s, fast sweep
       cadence, offline-worker sweep parked) and wait up to 30s for the
       sweep to flip A's task back to PENDING.
    6. Dashboard checkpoint #2: 5 PENDING rows, none claimed.
    7. Run worker B in-process via :func:`run_local_worker` with the
       fast-complete stub until the queue drains.
    8. Verify all 5 rows are DONE; one is claimed_by=B (the released
       task), the others are also claimed_by=B (B drained them too).
    9. Dashboard checkpoint #3: 5 DONE rows, ``claimed_by`` populated.
    10. Audit log: A's CLAIM + visibility_timeout RELEASE + B's
        CLAIM/START/COMPLETE trio for every drained row.

    Each checkpoint's failure attribution is intentionally distinct: a
    sweep regression surfaces as a step-5 timeout naming the released
    task; a drain regression surfaces as the step-8 status assertion
    naming the un-DONE rows; a projection regression surfaces as a
    dashboard-checkpoint mismatch (the failing checkpoint in the
    assertion message tells the operator which seam drifted).
    """
    await _seed_plan_with_five_tasks(db_pool, _PLAN_ID)

    # Pre-register the peer worker now so step 7's ``claim_task`` calls
    # don't fail on the FK to ``workers``. The HTTP register flow is out
    # of scope here (it's covered in test_remote_worker_*); a direct
    # INSERT keeps this test on the wire-level DB contract.
    await _seed_peer_worker(db_pool, _PEER_WORKER_ID)

    # ─── Spawn victim worker (subprocess + stuck runner) ─────────────────
    env = os.environ.copy()
    env["WHILLY_DATABASE_URL"] = postgres_dsn
    env["WHILLY_PLAN_ID"] = _PLAN_ID
    env["WHILLY_WORKER_ID"] = _VICTIM_WORKER_ID
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-c", _VICTIM_WORKER_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        try:
            await _wait_for_claim(
                db_pool,
                _EXPECTED_VICTIM_TASK,
                _VICTIM_WORKER_ID,
                timeout=_CLAIM_BUDGET_SECONDS,
            )
        except AssertionError:
            # Drain whatever the subprocess wrote so the failure message
            # carries actionable diagnostics. Common failure modes:
            # ImportError (whilly not installed in subprocess env),
            # asyncpg connection refused (DSN drift), schema mismatch,
            # or a regression in priority-ordered claiming that handed
            # the victim a different task than _EXPECTED_VICTIM_TASK.
            proc.kill()
            stdout, stderr = proc.communicate(timeout=_REAP_BUDGET_SECONDS)
            raise AssertionError(
                "victim worker subprocess did not claim the CRITICAL task within "
                f"{_CLAIM_BUDGET_SECONDS}s.\n"
                f"--- subprocess stdout ---\n{stdout.decode(errors='replace')}\n"
                f"--- subprocess stderr ---\n{stderr.decode(errors='replace')}"
            ) from None

        # ─── Dashboard checkpoint #1: A holds one row, four are PENDING ──
        # We probe the dashboard *before* the kill so a regression that
        # made the projection lag behind UPDATE traffic surfaces here
        # (rather than masquerading as a "B is slow" timeout later).
        rows_pre_kill = await fetch_dashboard_rows(db_pool, _PLAN_ID)
        assert len(rows_pre_kill) == 5, (
            f"dashboard pre-kill should see all 5 seeded tasks; got {len(rows_pre_kill)}: "
            f"{[r.task_id for r in rows_pre_kill]}"
        )
        by_id_pre = {row.task_id: row for row in rows_pre_kill}
        victim_row = by_id_pre[_EXPECTED_VICTIM_TASK]
        assert victim_row.status in {TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS}, (
            f"dashboard pre-kill: victim's task should be CLAIMED/IN_PROGRESS, got {victim_row.status!r}"
        )
        assert victim_row.claimed_by == _VICTIM_WORKER_ID, (
            f"dashboard pre-kill: claimed_by drift on victim's task — "
            f"got {victim_row.claimed_by!r}, expected {_VICTIM_WORKER_ID!r}"
        )
        unclaimed_pre = {tid: row for tid, row in by_id_pre.items() if tid != _EXPECTED_VICTIM_TASK}
        assert all(row.status is TaskStatus.PENDING for row in unclaimed_pre.values()), (
            f"dashboard pre-kill: non-victim tasks should be PENDING; got "
            f"{[(tid, row.status.value) for tid, row in unclaimed_pre.items()]}"
        )
        assert all(row.claimed_by is None for row in unclaimed_pre.values()), (
            "dashboard pre-kill: non-victim tasks should have claimed_by=NULL"
        )

        # Sanity guard: the subprocess must still be alive at the moment
        # we send the kill. A regression that crashed the worker between
        # claim and kill would have been caught above by ``_wait_for_claim``,
        # but this guards the narrower "process exited cleanly between
        # claim and kill" race.
        assert proc.poll() is None, (
            f"victim worker subprocess unexpectedly exited before SIGKILL (returncode={proc.returncode!r})"
        )

        # ─── SIGKILL ────────────────────────────────────────────────────
        # Uncatchable; the worker's heartbeat / release / pool-close paths
        # cannot run. Any recovery from here on is the *server's*
        # responsibility (visibility-timeout sweep).
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

    # ─── Bring up the visibility-timeout sweep ──────────────────────────
    # Created *after* the kill so the sweep doesn't fire mid-claim and
    # produce a confounding RELEASE event before the kill — same timing
    # rationale as :mod:`tests.integration.test_worker_kill`.
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
        # Wait for the sweep to flip the victim's task back to PENDING.
        # The sweep needs ``claimed_at`` to age past visibility_timeout
        # (10s); with sweep_interval=0.5s, the next tick after the
        # threshold lands within ~10.5s of the original claim. The 30s
        # budget gives 3x headroom for slow CI hosts.
        released_row = await _wait_for_status(
            db_pool, _EXPECTED_VICTIM_TASK, "PENDING", timeout=_RECOVERY_BUDGET_SECONDS
        )
        assert released_row["claimed_by"] is None, (
            f"sweep flipped status but left claimed_by={released_row['claimed_by']!r}; "
            f"the ck_tasks_claim_pair_consistent CHECK should have rejected this"
        )
        assert released_row["claimed_at"] is None, "sweep flipped status but left claimed_at populated"

        # ─── Dashboard checkpoint #2: 5 PENDING, no claims ──────────────
        rows_post_kill = await fetch_dashboard_rows(db_pool, _PLAN_ID)
        assert len(rows_post_kill) == 5, f"dashboard post-kill should still see 5 tasks; got {len(rows_post_kill)}"
        non_pending_post_kill = {
            row.task_id: row.status for row in rows_post_kill if row.status is not TaskStatus.PENDING
        }
        assert not non_pending_post_kill, (
            f"dashboard post-kill: every task should be PENDING after sweep; non-PENDING rows: {non_pending_post_kill}"
        )
        claimed_post_kill = {row.task_id: row.claimed_by for row in rows_post_kill if row.claimed_by is not None}
        assert not claimed_post_kill, (
            f"dashboard post-kill: claimed_by should be NULL on every PENDING row; "
            f"still-claimed rows: {claimed_post_kill}"
        )

        # ─── Drain via in-process worker B ──────────────────────────────
        # ``max_iterations`` cap: 5 task-processing iterations + an idle-
        # poll cushion so the loop exits cleanly after the queue drains
        # rather than spinning until the test's wall-clock budget. With
        # idle_wait=0.05 the cushion costs ~0.25s in the worst case.
        repo = TaskRepository(db_pool)
        plan = Plan(id=_PLAN_ID, name=_PLAN_ID)
        try:
            stats = await asyncio.wait_for(
                run_local_worker(
                    repo,
                    _fast_complete_runner,
                    plan,
                    _PEER_WORKER_ID,
                    idle_wait=0.05,
                    max_iterations=10,
                ),
                timeout=_DRAIN_BUDGET_SECONDS,
            )
        except TimeoutError as exc:
            # A timeout here means worker B couldn't drain the queue
            # within the budget — most likely a regression in claim_task
            # SQL or the local-worker outcome routing. Surface enough
            # context to triage without re-running.
            async with db_pool.acquire() as conn:
                snapshot = await conn.fetch(
                    "SELECT id, status, claimed_by FROM tasks WHERE plan_id = $1 ORDER BY id",
                    _PLAN_ID,
                )
            raise AssertionError(
                f"worker B failed to drain 5 tasks within {_DRAIN_BUDGET_SECONDS}s. "
                f"current task states: {[(r['id'], r['status'], r['claimed_by']) for r in snapshot]}"
            ) from exc

        assert stats.completed == 5, f"worker B should have completed all 5 tasks; stats={stats}"
        assert stats.failed == 0, f"worker B should have zero failures; stats={stats}"

    # ─── Final assertions: 5 DONE, dashboard agrees, audit log intact ───
    async with db_pool.acquire() as conn:
        final_rows = await conn.fetch(
            "SELECT id, status, claimed_by, version FROM tasks WHERE plan_id = $1 ORDER BY id",
            _PLAN_ID,
        )

    by_id_final = {row["id"]: row for row in final_rows}
    assert set(by_id_final) == set(_TASK_IDS), (
        f"final task set mismatch: missing={set(_TASK_IDS) - set(by_id_final)}, "
        f"extra={set(by_id_final) - set(_TASK_IDS)}"
    )
    not_done = {tid: row["status"] for tid, row in by_id_final.items() if row["status"] != "DONE"}
    assert not not_done, f"phase6: every task must be DONE; non-terminal rows: {not_done}"
    bad_claim = {tid: row["claimed_by"] for tid, row in by_id_final.items() if row["claimed_by"] != _PEER_WORKER_ID}
    assert not bad_claim, (
        f"phase6: every DONE row should be claimed_by={_PEER_WORKER_ID!r} (the surviving peer); drift: {bad_claim}"
    )

    # ─── Dashboard checkpoint #3: 5 DONE, projection matches table ──────
    rows_final = await fetch_dashboard_rows(db_pool, _PLAN_ID)
    assert len(rows_final) == 5, f"dashboard final: expected 5 rows, got {len(rows_final)}"
    non_done_final = {row.task_id: row.status for row in rows_final if row.status is not TaskStatus.DONE}
    assert not non_done_final, f"dashboard final: every row should be DONE; non-DONE rows: {non_done_final}"
    bad_claim_final = {row.task_id: row.claimed_by for row in rows_final if row.claimed_by != _PEER_WORKER_ID}
    assert not bad_claim_final, (
        f"dashboard final: claimed_by should be {_PEER_WORKER_ID!r} on every DONE row; drift: {bad_claim_final}"
    )

    # ─── Audit log: visibility_timeout RELEASE for the victim's task ─────
    # The full SC-2 audit shape is pinned in detail by
    # :mod:`tests.integration.test_worker_kill`; here we only re-check the
    # specific row this test owns (the released CRITICAL task) so a
    # regression that broke the AC pin on ``reason='visibility_timeout'``
    # surfaces in this gate too without duplicating the full audit
    # assertions from the per-task suite.
    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id ASC",
            _EXPECTED_VICTIM_TASK,
        )
    release_events = [e for e in events if e["event_type"] == "RELEASE"]
    assert len(release_events) >= 1, (
        f"expected at least one RELEASE event on the victim's task after sweep; got {[e['event_type'] for e in events]}"
    )
    release_payload = json.loads(release_events[0]["payload"])
    assert release_payload["reason"] == "visibility_timeout", (
        f"expected reason='visibility_timeout' on victim's RELEASE; got payload={release_payload!r}. "
        f"If reason='worker_offline' shows up, the offline-worker sweep beat the visibility-timeout "
        f"sweep — check that heartbeat_timeout_seconds is parked at "
        f"{_HEARTBEAT_TIMEOUT_DISABLED}s for this test."
    )
