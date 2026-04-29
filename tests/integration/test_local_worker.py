"""Integration test for ``whilly run`` (TASK-019c, PRD FR-1.6).

Acceptance criteria covered
---------------------------
- ``whilly run --plan <id>`` запускает local worker с heartbeat: a synthetic
  plan with a single PENDING task is imported into Postgres, then the CLI
  is invoked with ``--max-iterations`` so the loop terminates deterministically
  after the task is exhausted. The task transitions to DONE end-to-end.
- При отсутствии плана — exit code 2 с подсказкой: a missing ``plan_id``
  is exercised end-to-end against the real DB and asserted to surface
  ``EXIT_ENVIRONMENT_ERROR`` with a stderr diagnostic that names the id.
- Все задачи плана доводятся до DONE/FAILED при отсутствии падений: the
  fake runner returns a ``COMPLETE`` :class:`AgentResult` so the worker's
  outcome routing exercises the ``complete_task`` path; the test then
  asserts ``status=DONE`` and the audit-log shape (CLAIM, START, COMPLETE).

Why this lives in ``tests/integration/`` and not ``tests/unit/``
----------------------------------------------------------------
The unit suite (``tests/unit/test_cli_run.py``) already exhausts the
sync exit-code mapping with patched ``_async_run``. The point of *this*
file is to pin the full composition: pool open → plan SELECT → worker
register INSERT → ``run_local_worker`` claim/start/complete loop → pool
close. A regression in the SQL shape of any of those (the placeholder
``token_hash``, the ``ON CONFLICT`` clause for re-registration, the
``_select_plan_with_tasks`` reuse from :mod:`whilly.cli.plan`) would
slip past the unit suite but fail this test loudly.

Why ``--max-iterations 5`` and not "let the worker decide"
----------------------------------------------------------
``run_local_worker`` polls forever by default — that's the production
contract (FR-1.6). Tests need a deterministic exit, and the cleanest
way is the same ``--max-iterations`` flag operators get for one-shot CI
runs. Five iterations is enough headroom for the claim → start → run →
complete sequence (one iteration each for the work + a couple of idle
polls afterwards) without making the test wall-clock slow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import cast

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.cli import run as cli_run
from whilly.cli.run import EXIT_ENVIRONMENT_ERROR, EXIT_OK, run_run_command
from whilly.core.models import Task

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-RUN-CLI-1"
TASK_ID = "T-RUN-CLI-1"


@pytest.fixture
def db_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Surface the testcontainers DSN through ``WHILLY_DATABASE_URL``.

    ``run_run_command`` reads the env var, not a function argument, so
    every test in this file needs the override in place. Scoped per-test
    so the post-test cleanup matches the per-test ``db_pool`` truncation
    — no leak across tests.
    """
    monkeypatch.setenv("WHILLY_DATABASE_URL", postgres_dsn)
    yield postgres_dsn


async def _seed_plan_with_one_task(pool: asyncpg.Pool, plan_id: str, task_id: str) -> None:
    """Insert a plan + one PENDING task ready to be claimed.

    Mirrors the seeding pattern from
    :mod:`tests.integration.test_worker_signals` so a schema regression
    surfaces consistently across the integration suite. We deliberately
    don't pre-insert a workers row — that's exactly what the CLI's own
    registration INSERT must handle, and skipping it here keeps the test
    honest about the CLI's responsibilities.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2)",
            plan_id,
            f"plan {plan_id}",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'high', $3, '[]'::jsonb, '[]'::jsonb, 'FR-1.6', 0)
            """,
            task_id,
            plan_id,
            f"run-cli test task {task_id}",
        )


async def _fake_runner_complete(task: Task, prompt: str) -> AgentResult:
    """A fake agent runner that always reports a successful completion.

    ``is_complete=True`` + ``exit_code=0`` is the contract for
    :meth:`TaskRepository.complete_task` — see
    :func:`whilly.worker.local.run_local_worker`'s outcome routing. We
    return a minimal :class:`AgentUsage` (zeros) because the worker
    doesn't read it; the metrics surface (TASK-024 territory) will.
    """
    return AgentResult(
        usage=AgentUsage(),
        exit_code=0,
        is_complete=True,
        output=f"<promise>COMPLETE</promise> for {task.id}",
    )


async def test_run_command_processes_plan_to_done(
    db_pool: asyncpg.Pool,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: imported plan → ``whilly run`` → task DONE + full audit log.

    Exercises every adapter the CLI composition root touches. A regression
    in the worker registration INSERT (e.g. dropping the ``ON CONFLICT``
    clause), the ``_select_plan_with_tasks`` reuse, or the worker loop's
    completion routing would all surface here as a hung test, a wrong
    final status, or a missing audit event.
    """
    await _seed_plan_with_one_task(db_pool, PLAN_ID, TASK_ID)

    # Patch the runner factory so the worker doesn't try to invoke the
    # real ``claude`` binary. ``run_run_command``'s ``runner`` kwarg is
    # the documented injection seam for exactly this case.
    exit_code = await asyncio.to_thread(
        run_run_command,
        ["--plan", PLAN_ID, "--max-iterations", "5", "--idle-wait", "0.01", "--heartbeat-interval", "60.0"],
        runner=_fake_runner_complete,
        install_signal_handlers=False,
    )
    assert exit_code == EXIT_OK, f"whilly run exited with {exit_code}, expected {EXIT_OK}"

    # Database post-conditions. The task must have advanced through the
    # full state-machine: PENDING → CLAIMED → IN_PROGRESS → DONE.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, claimed_by, claimed_at, version FROM tasks WHERE id = $1",
            TASK_ID,
        )
    assert row is not None, f"task {TASK_ID} disappeared from the database"
    assert row["status"] == "DONE", f"final status was {row['status']!r}, expected DONE"
    # claim → start → complete each bump version, so version >= 3.
    assert row["version"] >= 3, f"version did not advance through completion: {row['version']}"

    # Audit log shape: at least one CLAIM, START, COMPLETE event each. The
    # presence-rather-than-equality check is deliberate — future tasks
    # (TASK-022) may add additional events to this row, and asserting
    # exact event counts would create false-positive failures during the
    # rollout. The ordering is fixed by the state-machine.
    async with db_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type FROM events WHERE task_id = $1 ORDER BY created_at, id",
            TASK_ID,
        )
    event_types = [e["event_type"] for e in events]
    assert "CLAIM" in event_types, f"no CLAIM event recorded: {event_types!r}"
    assert "START" in event_types, f"no START event recorded: {event_types!r}"
    assert "COMPLETE" in event_types, f"no COMPLETE event recorded: {event_types!r}"

    # The CLI must also have registered a workers row — the FK on
    # tasks.claimed_by would have rejected ``claim_task`` otherwise, so
    # the row's existence is implicit, but we assert directly to pin
    # the placeholder ``token_hash`` value documented in cli/run.py.
    async with db_pool.acquire() as conn:
        worker_rows = await conn.fetch("SELECT worker_id, hostname, token_hash FROM workers ORDER BY worker_id")
    assert worker_rows, "whilly run did not register a workers row"
    assert worker_rows[0]["token_hash"] == "local", (
        f"workers.token_hash was {worker_rows[0]['token_hash']!r}, "
        "expected the documented 'local' placeholder for local-worker registration"
    )


async def test_run_command_exits_2_when_plan_not_in_db(
    db_pool: asyncpg.Pool,
    db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A plan id absent from Postgres → exit 2 + diagnostic naming the id.

    This is the AC's "При отсутствии плана — exit code 2" path exercised
    against a real DB rather than via patching. We deliberately do not
    seed *any* plan so the SELECT returns ``None`` cleanly; the CLI must
    map that to ``EXIT_ENVIRONMENT_ERROR`` with a stderr message the
    operator can act on.
    """
    missing_id = "PLAN-DOES-NOT-EXIST"
    exit_code = await asyncio.to_thread(
        run_run_command,
        ["--plan", missing_id, "--max-iterations", "1"],
        runner=_fake_runner_complete,
    )
    assert exit_code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert missing_id in captured.err, f"diagnostic did not mention the missing id: {captured.err!r}"
    assert "not found" in captured.err.lower()


async def test_run_command_idempotent_worker_registration(
    db_pool: asyncpg.Pool,
    db_url: str,
) -> None:
    """Running the CLI twice with the same ``--worker-id`` does not blow up.

    The schema's ``workers.worker_id`` is a primary key; without the
    documented ``ON CONFLICT (worker_id) DO UPDATE`` clause in
    :data:`whilly.cli.run._REGISTER_WORKER_SQL`, the second invocation
    would crash with a unique-constraint violation. Operators restart
    workers freely (kubectl rollouts, manual re-runs); a registration
    that wasn't idempotent would force a manual cleanup before each
    restart — exactly the foot-gun the AC's "повторный запуск" wording
    forbids.

    We seed two PENDING tasks so the second invocation has work to claim
    (otherwise the worker would idle for ``max_iterations`` and the
    test would prove nothing about the second registration).
    """
    await _seed_plan_with_one_task(db_pool, PLAN_ID, TASK_ID)
    second_task_id = TASK_ID + "-2"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'high', $3, '[]'::jsonb, '[]'::jsonb, 'FR-1.6', 0)
            """,
            second_task_id,
            PLAN_ID,
            "second task",
        )

    base_argv = ["--plan", PLAN_ID, "--worker-id", "stable-id-1", "--max-iterations", "5", "--idle-wait", "0.01"]
    first = await asyncio.to_thread(
        run_run_command, base_argv, runner=_fake_runner_complete, install_signal_handlers=False
    )
    second = await asyncio.to_thread(
        run_run_command, base_argv, runner=_fake_runner_complete, install_signal_handlers=False
    )
    assert first == EXIT_OK
    assert second == EXIT_OK

    async with db_pool.acquire() as conn:
        worker_rows = await conn.fetch("SELECT worker_id FROM workers")
        done_count = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE plan_id = $1 AND status = 'DONE'",
            PLAN_ID,
        )

    # Exactly one workers row despite two registrations — the ON CONFLICT
    # branch did its job.
    worker_ids = [r["worker_id"] for r in worker_rows]
    assert worker_ids == ["stable-id-1"], f"expected one workers row, got {worker_ids!r}"
    # Both tasks reached DONE across the two CLI invocations.
    assert done_count == 2, f"expected both tasks DONE, got {done_count}"


async def test_run_command_uses_dispatcher_via_main(
    db_pool: asyncpg.Pool,
    db_url: str,
) -> None:
    """``whilly.cli.main(["run", ...])`` round-trips the run subcommand end-to-end.

    The unit suite already pins the dispatcher routing with a stubbed
    handler. This integration test pins the *real* call: a regression
    in :func:`whilly.cli.main` that broke the ``run`` branch but still
    let the lazy import succeed would slip past unit coverage. Driving
    the same plan through ``main`` here closes that gap.
    """
    await _seed_plan_with_one_task(db_pool, PLAN_ID, TASK_ID)

    # ``main`` doesn't expose the ``runner`` or ``install_signal_handlers``
    # kwargs — they're injection seams for direct callers. We compensate
    # with two module-level patches:
    #
    # * ``run_task`` → ``_fake_runner_complete``: patched at its import
    #   site in :mod:`whilly.cli.run` so the CLI's ``runner = ... or
    #   run_task`` fallback picks up the stub.
    # * ``_install_signal_handlers`` / ``_remove_signal_handlers`` → no-ops:
    #   the integration test runs the CLI inside :func:`asyncio.to_thread`,
    #   and :meth:`asyncio.AbstractEventLoop.add_signal_handler` raises
    #   ``RuntimeError`` from a worker thread. Bypassing handler installation
    #   removes the only thread-affinity dependency without changing the
    #   production code path.
    from whilly.worker import main as worker_main

    fake_runner = _fake_runner_complete
    with pytest.MonkeyPatch().context() as m:
        m.setattr(cli_run, "run_task", cast(object, fake_runner))
        m.setattr(worker_main, "_install_signal_handlers", lambda _stop: [])
        m.setattr(worker_main, "_remove_signal_handlers", lambda _installed: None)
        from whilly.cli import main as dispatch_main

        exit_code = await asyncio.to_thread(
            dispatch_main,
            ["run", "--plan", PLAN_ID, "--max-iterations", "5", "--idle-wait", "0.01"],
        )

    assert exit_code == EXIT_OK
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", TASK_ID)
    assert status == "DONE", f"task did not reach DONE via main(): status={status!r}"
