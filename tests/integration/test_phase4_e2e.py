"""Phase 4 e2e gate (TASK-020, PRD Day 4 deliverable).

Closes Phase 4 by composing the local-worker stack into a single end-to-end
flow against a real Postgres and a real subprocess agent::

    whilly plan import
        -> whilly run --plan <id>           (CLI handler in whilly.cli.run)
            -> asyncpg pool + TaskRepository
            -> run_worker (heartbeat composition root)
                -> run_local_worker (claim -> start -> run agent -> complete/fail)
                    -> run_task (subprocess wrapper, whilly.adapters.runner.claude_cli)
                        -> asyncio.create_subprocess_exec(CLAUDE_BIN, ...)
                            -> tests/fixtures/fake_claude.sh
                                -> JSON envelope with <promise>COMPLETE</promise>
                            <- exit 0
                        <- AgentResult(is_complete=True, exit_code=0)
                    <- repo.complete_task(task_id, version)

Each individual link has its own focused suite already (the unit test for
``parse_output`` covers JSON edge cases, ``run_local_worker`` is unit-tested
against a stub coroutine, ``test_phase2_e2e`` already proves repo + import
+ export agree on terminal state). The point of *this* test is to prove the
links *compose* — i.e. an operator who runs ``CLAUDE_BIN=... whilly run``
against an imported plan ends up with every task ``DONE`` and the audit
trail intact, and the worker's outcome routing actually fires the right
state-machine transitions on a real subprocess result.

Why a Phase Gate test exists at all
-----------------------------------
v4 is Hexagonal: each adapter is unit-tested in isolation against fakes,
which catches *internal* contract violations but not *boundary* drift (an
``--output-format json`` flag dropped from the argv, a ``CLAUDE_BIN`` env
read that bypasses :func:`os.environ.get`, a worker that ignores the
``is_complete`` flag and marks every task DONE). This test is the cheapest
way to surface that class of bug before TASK-021 onwards builds the
HTTP/remote variant of the same plumbing.

The acceptance criteria from TASK-020 map verbatim onto the assertions
below:

* "fake_claude.sh — детерминированный stub возвращающий ``<promise>COMPLETE</promise>``"
  -> :func:`test_fake_claude_stub_invocation_emits_completion_marker` invokes
    the script standalone via :func:`asyncio.create_subprocess_exec` (no
    DB, no worker) and asserts ``parse_output`` sees ``is_complete=True``
    and ``exit_code=0``. Cheap canary that fires if the fixture loses its
    executable bit, the JSON shape changes, or the parser drifts. We use
    the *same* spawn primitive the production runner uses
    (whilly.adapters.runner.claude_cli._spawn_and_collect) so the canary
    fails for exactly the same reason the e2e flow would.
* "3 задачи переходят PENDING -> DONE"
  -> :func:`test_phase4_e2e_three_tasks_reach_done_via_local_worker` imports
    a 3-task plan, runs the CLI worker with ``CLAUDE_BIN`` pointing at the
    stub and a bounded ``--max-iterations`` cap, then asserts every row's
    ``status='DONE'`` directly from the ``tasks`` table.
* "events содержит по 3 записи на задачу"
  -> :func:`test_phase4_e2e_events_table_has_claim_start_complete_per_task`
    runs the same loop and reads ``events`` row-by-row, asserting the
    CLAIM / START / COMPLETE trio appears *in order* per task. Out-of-order
    or missing events are concrete adapter bugs (e.g. a runner that bypasses
    ``start_task``, or a complete that fires before the start row commits).

Why we go through the live ``whilly run`` CLI handler (and not the worker
directly)
    The AC names "Day 4 deliverable" — that's the CLI surface, not the
    worker function. Calling :func:`run_run_command` keeps the path
    identical to what an operator types in a shell: argparse -> DSN env ->
    pool open -> worker registration -> loop -> pool close. A future
    regression in any of those would not surface if we shimmed the worker
    function directly; it would surface here.

Why we point ``CLAUDE_BIN`` at a shell stub (not a Python mock)
    The runner spawns ``claude`` via :func:`asyncio.create_subprocess_exec`
    and pipes its stdout through :func:`parse_output`. Mocking on the
    Python side would bypass the subprocess seam, the JSON wire-shape, and
    the exit-code thread-through — i.e. the *actual* boundary the test is
    supposed to certify. A POSIX shell stub stays in the same lane the
    production binary uses.

Why ``install_signal_handlers=False``
    pytest-asyncio's loop owns the test thread, so the CLI handler's
    internal :func:`asyncio.run` must execute on a worker thread (via
    :func:`asyncio.to_thread`). :meth:`asyncio.AbstractEventLoop.add_signal_handler`
    raises ``RuntimeError`` from non-main threads — the CLI exposes this
    kwarg specifically to let integration tests bypass the SIGTERM/SIGINT
    installation. Production CLI invocations always run in the main thread
    of the main interpreter, so the default ``True`` is correct.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.runner.result_parser import parse_output
from whilly.cli.plan import DATABASE_URL_ENV
from whilly.cli.plan import EXIT_OK as PLAN_EXIT_OK
from whilly.cli.plan import run_plan_command
from whilly.cli.run import EXIT_OK as RUN_EXIT_OK
from whilly.cli.run import run_run_command
from whilly.core.state_machine import Transition

pytestmark = DOCKER_REQUIRED


# Stable ids the assertions below pin against. Three tasks is the smallest
# set that distinguishes "every task got the trio" from "the first task
# got three events and we assumed the rest did too" — a regression where
# only the leader-row writes events would silently pass on a single-task
# plan.
PLAN_ID = "plan-phase4-e2e"
PROJECT_NAME = "Phase 4 e2e gate"
TASK_IDS: tuple[str, ...] = ("T-001", "T-002", "T-003")

# Resolved at module load so the test fails fast at collection time if the
# fixture has been moved or deleted, rather than silently passing the
# import step and then crashing inside the subprocess spawn with
# ``EXIT_BINARY_NOT_FOUND``.
FAKE_CLAUDE_PATH: Path = (Path(__file__).parent.parent / "fixtures" / "fake_claude.sh").resolve()


# ─── helpers ─────────────────────────────────────────────────────────────


async def _run_plan(argv: Sequence[str]) -> int:
    """Execute ``whilly plan ...`` off the test event loop.

    Mirrors :func:`tests.integration.test_phase2_e2e._run_cli`. The CLI
    handler internally calls :func:`asyncio.run`, which Python refuses to
    nest under another running loop. Routing through
    :func:`asyncio.to_thread` parks the handler on a worker thread that
    owns its own loop — the test loop stays unblocked and the handler's
    pool-lifecycle / json.dump behaviour stays identical to a real
    ``whilly plan ...`` invocation.
    """
    return await asyncio.to_thread(run_plan_command, list(argv))


async def _run_run(argv: Sequence[str]) -> int:
    """Execute ``whilly run ...`` off the test event loop.

    Two differences from :func:`_run_plan` matter:

    * ``run_run_command`` accepts an ``install_signal_handlers`` kwarg.
      Pytest-asyncio runs the test on the loop it owns, so the CLI handler
      ends up on a worker thread via :func:`asyncio.to_thread` — and
      :meth:`asyncio.AbstractEventLoop.add_signal_handler` raises
      ``RuntimeError`` from worker threads. We pin the flag to ``False``
      via :func:`functools.partial` because :func:`asyncio.to_thread`'s
      signature is positional-only for the wrapped callable's args.
    * The handler also blocks on the worker loop until ``max_iterations``
      is reached or the plan drains, so a missing cap would hang the test
      indefinitely. Callers always pass ``--max-iterations N`` and a small
      ``--idle-wait`` to keep wall-clock time bounded.
    """
    handler = partial(run_run_command, list(argv), install_signal_handlers=False)
    return await asyncio.to_thread(handler)


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    Both ``whilly plan import`` and ``whilly run`` read the DSN from the
    environment because they're one-shot CLI handlers — the alternative
    would be threading the DSN through every call site. ``postgres_dsn``
    already exports the var for its own bootstrap, but we restore the
    prior value on teardown so a leak from another test does not bleed
    into this one (mirrors the local fixture in ``test_phase2_e2e.py``).
    """
    prior = os.environ.get(DATABASE_URL_ENV)
    os.environ[DATABASE_URL_ENV] = postgres_dsn
    try:
        yield postgres_dsn
    finally:
        if prior is None:
            os.environ.pop(DATABASE_URL_ENV, None)
        else:
            os.environ[DATABASE_URL_ENV] = prior


@pytest.fixture
def claude_bin(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``CLAUDE_BIN`` at the fixture stub for the duration of one test.

    The runner reads ``CLAUDE_BIN`` at every subprocess spawn (see
    :func:`whilly.adapters.runner.claude_cli._claude_bin`), so a
    monkeypatch in the main thread is visible to the worker thread that
    eventually runs the CLI handler — :class:`os.environ` is a singleton
    backed by a process-wide C-level table, not thread-local. We
    deliberately set the *resolved absolute* path so the worker's cwd
    (which differs from the test cwd in subprocess-driven scenarios) does
    not affect resolution.
    """
    monkeypatch.setenv("CLAUDE_BIN", str(FAKE_CLAUDE_PATH))
    return FAKE_CLAUDE_PATH


@pytest.fixture
def plan_payload() -> dict[str, Any]:
    """Return a v4 plan dict with three independent tasks.

    Independence (no dependencies) matters here because the local worker
    relies on :meth:`TaskRepository.claim_task` for ordering, and that
    method is dependency-blind by design (TASK-013c's ``next_ready`` is
    Phase 3 territory and not exercised by ``whilly run``). If we threaded
    a dependency edge through, the ``ORDER BY priority_rank, id`` claim
    would still work, but a future regression that wires dependency
    enforcement into the runtime would silently pin tasks to PENDING and
    flake this test instead of failing it deterministically.
    """
    return {
        "plan_id": PLAN_ID,
        "project": PROJECT_NAME,
        "tasks": [
            {
                "id": "T-001",
                "status": "PENDING",
                "priority": "critical",
                "description": "Synthetic task — picked first by ORDER BY priority.",
                "dependencies": [],
                "key_files": ["whilly/main.py"],
                "acceptance_criteria": ["entry point runs"],
                "test_steps": ["pytest -q"],
                "prd_requirement": "Day 4 deliverable",
            },
            {
                "id": "T-002",
                "status": "PENDING",
                "priority": "high",
                "description": "Synthetic task — UTF-8 round-trip canary: «привет», 你好.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
                "prd_requirement": "Day 4 deliverable",
            },
            {
                "id": "T-003",
                "status": "PENDING",
                "priority": "medium",
                "description": "Synthetic trailing task — proves event trio fires for all rows.",
                "dependencies": [],
                "key_files": ["scripts/seed.sh"],
                "acceptance_criteria": ["seed.sh executable"],
                "test_steps": ["bash scripts/seed.sh --dry-run"],
                "prd_requirement": "Day 4 deliverable",
            },
        ],
    }


@pytest.fixture
def plan_file(tmp_path: Path, plan_payload: dict[str, Any]) -> Path:
    """Write ``plan_payload`` to disk so ``import`` reads from a real file.

    Materialising the JSON (rather than driving ``parse_plan`` on the dict
    directly) keeps the test path identical to ``whilly plan import
    path/to/plan.json`` — i.e. an operator-realistic surface that
    exercises the file-read step in :func:`parse_plan` too.
    """
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(plan_payload), encoding="utf-8")
    return target


def _decode_payload(raw: object) -> dict[str, object]:
    """Decode an asyncpg JSONB ``payload`` cell to a dict.

    asyncpg returns JSONB as raw ``str`` (JSON text) by default — the pool
    deliberately does not register a codec. Mirrors the helper in
    ``test_phase2_e2e.py`` so this file stays self-contained without
    cross-file private imports.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        assert isinstance(decoded, dict), f"unexpected JSON shape: {decoded!r}"
        return decoded
    raise AssertionError(f"unexpected JSONB payload type {type(raw).__name__}: {raw!r}")


# ─── tests ───────────────────────────────────────────────────────────────


async def test_fake_claude_stub_invocation_emits_completion_marker() -> None:
    """AC #1: the stub binary itself produces a parseable, complete result.

    Cheap canary that fails fast if the fixture loses its executable bit
    or the JSON shape drifts away from what :func:`parse_output` accepts —
    no Postgres, no worker. If this test fails the e2e tests below will
    fail too, but with a far less obvious diagnostic
    (``EXIT_BINARY_NOT_FOUND``, or every task in FAILED state, or the
    runner's API-error retry path triggering on a parse fallback).

    We invoke the script via :func:`asyncio.create_subprocess_exec` —
    *the same primitive* the production runner uses
    (whilly.adapters.runner.claude_cli._spawn_and_collect). That keeps
    the canary failing for the exact same reason the e2e flow would,
    not for some adjacent reason a different spawn primitive might
    surface.
    """
    assert FAKE_CLAUDE_PATH.exists(), f"fixture missing: {FAKE_CLAUDE_PATH}"
    assert os.access(FAKE_CLAUDE_PATH, os.X_OK), (
        f"fixture lost its executable bit: {FAKE_CLAUDE_PATH}; run `chmod +x` to restore"
    )

    proc = await asyncio.create_subprocess_exec(
        str(FAKE_CLAUDE_PATH),
        "-p",
        "irrelevant prompt",
        "--output-format",
        "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8")

    assert proc.returncode == 0, f"stub exited {proc.returncode}; stderr={stderr_bytes!r}"
    assert stdout, "stub produced empty stdout — parser would fall back to AgentResult()"

    result = parse_output(stdout, exit_code=proc.returncode or 0)
    assert result.is_complete, f"stub stdout did not surface completion marker: {result.output!r}"
    assert result.exit_code == 0, f"stub exit_code threaded through as {result.exit_code} (expected 0)"


async def test_phase4_e2e_three_tasks_reach_done_via_local_worker(
    db_pool: asyncpg.Pool,
    database_url: str,  # noqa: ARG001  — sets WHILLY_DATABASE_URL for the CLI handlers
    claude_bin: Path,  # noqa: ARG001  — sets CLAUDE_BIN for the runner
    plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC #2: every task in a 3-task plan transitions PENDING -> DONE via ``whilly run``.

    Asserts directly against the ``tasks`` table rather than against the
    export CLI: ``test_phase2_e2e`` already proves the export reads what
    the repository wrote, so duplicating that round-trip here would only
    bloat the test without strengthening the gate. The Phase 4 gate is
    specifically about the worker stack producing the right *terminal
    state* — the table row is the source of truth for that.

    Why ``--max-iterations 6``
        The worker loop counts both task-processing iterations and
        idle-poll iterations. With three tasks the first three iterations
        consume the queue; iteration 4 returns ``None`` from ``claim_task``
        (queue drained) and increments ``idle_polls``. A cap of 6 leaves
        a buffer of three idle polls before clean exit, which keeps the
        wall-clock time bounded (~0.15s of idle wait at ``idle_wait=0.05``)
        without flaking under transient subprocess-spawn slowness.

    Why ``--heartbeat-interval`` is set to a value larger than
    ``max_iterations × idle_wait``
        ``run_worker`` (whilly.worker.main) composes the loop with a
        heartbeat coroutine that ticks immediately on entry and then on
        ``heartbeat_interval``. The first tick is unavoidable; subsequent
        ticks during the test are pointless DB round-trips that just slow
        the suite. Pinning the interval at 999s effectively disables them.
    """
    assert await _run_plan(["import", str(plan_file)]) == PLAN_EXIT_OK
    capsys.readouterr()  # Drain the import banner so it doesn't pollute later assertions.

    rc = await _run_run(
        [
            "--plan",
            PLAN_ID,
            "--max-iterations",
            "6",
            "--idle-wait",
            "0.05",
            "--heartbeat-interval",
            "999",
            "--worker-id",
            "w-phase4",
        ]
    )
    assert rc == RUN_EXIT_OK, f"whilly run returned {rc} (expected {RUN_EXIT_OK})"
    capsys.readouterr()  # Drain the worker stats banner; written to stderr but cleared for hygiene.

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, status, version, claimed_by FROM tasks WHERE plan_id = $1 ORDER BY id",
            PLAN_ID,
        )

    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == set(TASK_IDS), (
        f"task set mismatch: missing={set(TASK_IDS) - set(by_id)}, extra={set(by_id) - set(TASK_IDS)}"
    )

    not_done = {tid: row["status"] for tid, row in by_id.items() if row["status"] != "DONE"}
    assert not not_done, f"phase 4 e2e: expected every task DONE after `whilly run`, got non-terminal: {not_done}"

    # ``version`` must have advanced by exactly 3 per task (CLAIM=1, START=2,
    # COMPLETE=3 starting from the parse_plan default of 0). Pinning the
    # counter — not just the terminal status — protects against a future
    # SQL refactor that flips status without bumping version, which would
    # silently break optimistic locking for any subsequent transition
    # (re-claim after release, FAIL backstop, etc.).
    bad_versions = {tid: row["version"] for tid, row in by_id.items() if row["version"] != 3}
    assert not bad_versions, f"phase 4 e2e: version counter not advanced by exactly 3 per task: {bad_versions}"

    # claimed_by retained on DONE rows so post-mortems can answer "who ran
    # this?". We assert non-null + correct worker id (the CLI's
    # ``--worker-id`` flag, threaded through to ``claim_task``).
    bad_claim = {tid: row["claimed_by"] for tid, row in by_id.items() if row["claimed_by"] != "w-phase4"}
    assert not bad_claim, f"phase 4 e2e: claimed_by drift on DONE rows — expected 'w-phase4' for all, got: {bad_claim}"


async def test_phase4_e2e_events_table_has_claim_start_complete_per_task(
    db_pool: asyncpg.Pool,
    database_url: str,  # noqa: ARG001
    claude_bin: Path,  # noqa: ARG001
    plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC #3: ``events`` holds CLAIM, START, COMPLETE per task, in order.

    The trio must appear *in order* per task (CLAIM before START before
    COMPLETE) — the in-order guarantee comes from each transition
    happening inside its own ``async with conn.transaction()`` and being
    committed before the next is even attempted, but it's cheap to assert
    and would catch a future "fire-and-forget event insert" regression
    that violated the contract. Mirrors the equivalent assertion in
    ``test_phase2_e2e.py`` but driven by the actual CLI worker stack
    (subprocess-backed runner) rather than by direct repository calls.

    Why fetch events ordered by ``(task_id, created_at)`` rather than
    grouped in Python: the index ``ix_events_task_id_created_at`` promises
    this exact ordering at the SQL level, so leaning on it doubles as a
    coverage check that the index is still pulling its weight (a missing
    index would make this query plan a SEQSCAN + in-memory sort — same
    result, but a hint that the event table is growing without
    operational support).
    """
    assert await _run_plan(["import", str(plan_file)]) == PLAN_EXIT_OK
    capsys.readouterr()

    rc = await _run_run(
        [
            "--plan",
            PLAN_ID,
            "--max-iterations",
            "6",
            "--idle-wait",
            "0.05",
            "--heartbeat-interval",
            "999",
            "--worker-id",
            "w-phase4-events",
        ]
    )
    assert rc == RUN_EXIT_OK, f"whilly run returned {rc}"
    capsys.readouterr()

    async with db_pool.acquire() as conn:
        # Filter out the import-time ``task.created`` audit events
        # written by the plan-import path (M3 fix-feature) — this test
        # pins the worker-side state-machine transitions only.
        rows = await conn.fetch(
            "SELECT task_id, event_type, created_at, payload "
            "FROM events "
            "WHERE task_id = ANY($1::text[]) AND event_type != 'task.created' "
            "ORDER BY task_id, created_at, id",
            list(TASK_IDS),
        )

    by_task: dict[str, list[asyncpg.Record]] = {tid: [] for tid in TASK_IDS}
    for row in rows:
        by_task[row["task_id"]].append(row)

    expected_sequence = (Transition.CLAIM.value, Transition.START.value, Transition.COMPLETE.value)
    for tid in TASK_IDS:
        events = by_task[tid]
        assert len(events) == 3, (
            f"task {tid!r} should have exactly 3 transition events (CLAIM, START, COMPLETE); "
            f"got {len(events)}: {[e['event_type'] for e in events]}"
        )
        actual_sequence = tuple(e["event_type"] for e in events)
        assert actual_sequence == expected_sequence, (
            f"task {tid!r} event order is {actual_sequence}; expected {expected_sequence}"
        )

        # Each transition's payload must include the post-update version
        # so the audit log is replayable. CLAIM also pins worker_id —
        # asserting that wires the CLI's ``--worker-id`` flag all the
        # way through ``run_worker`` -> ``run_local_worker`` ->
        # ``claim_task``.
        claim_payload = _decode_payload(events[0]["payload"])
        assert claim_payload.get("worker_id") == "w-phase4-events", (
            f"CLAIM payload for {tid!r} missing/wrong worker_id: {claim_payload!r}"
        )
        assert claim_payload.get("version") == 1, f"CLAIM payload version drift: {claim_payload!r}"
        start_payload = _decode_payload(events[1]["payload"])
        assert start_payload.get("version") == 2, f"START payload version drift: {start_payload!r}"
        complete_payload = _decode_payload(events[2]["payload"])
        assert complete_payload.get("version") == 3, f"COMPLETE payload version drift: {complete_payload!r}"
