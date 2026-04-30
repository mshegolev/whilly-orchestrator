"""Phase 2 e2e gate (TASK-012, PRD FR-2.4 / FR-2.5).

Closes Phase 2 by composing the four already-tested adapters into a
single end-to-end flow against a real Postgres:

    whilly plan import  →  TaskRepository.claim_task
                        →  TaskRepository.start_task
                        →  TaskRepository.complete_task
                        →  whilly plan export

Each individual link has its own focused suite already
(``test_plan_io.py`` for import/export round-trip, ``test_concurrent_claims.py``
for ``claim_task`` under contention, ``tests/unit/test_state_machine.py``
for the pure transition rules). The point of *this* test is to prove the
links *compose* — i.e. an operator who imports a plan, a worker that
runs through it, and an operator who exports it afterwards all see a
mutually consistent picture of the world.

Why a Phase Gate test exists at all
-----------------------------------
v4 is Hexagonal: each adapter is unit-tested in isolation against fakes,
which catches *internal* contract violations but not *boundary* drift
(an UPDATE that commits the row but writes the wrong event_type, an
export that selects ``status`` from the wrong column, a
``claim_task`` that mutates ``tasks.version`` but ``start_task`` reads
``v3_version``). This test is the cheapest way to surface that class of
bug before TASK-013 onwards builds on the same surface.

The acceptance criteria from TASK-012 map verbatim onto the assertions
below:

* "End-to-end через testcontainers"
  → ``pytestmark = DOCKER_REQUIRED`` + reuse of the session-scoped
    ``postgres_dsn`` fixture from ``tests/conftest.py``.
* "events таблица содержит CLAIM, START, COMPLETE для каждой задачи"
  → :func:`test_phase2_e2e_emits_claim_start_complete_per_task` reads
    ``events`` row-by-row and asserts the trio is present *and ordered*
    by ``created_at`` within each task. Out-of-order events are a
    concrete adapter bug (e.g. a START that overlaps a CLAIM
    transaction).
* "Финальный export показывает status=DONE"
  → :func:`test_phase2_e2e_export_after_completion_shows_done` parses
    the exported JSON and asserts every task's ``status`` is ``DONE``.

Why we go through the live ``whilly plan`` CLI handlers (and not raw
SQL) on both ends
    The AC names "import" and "export" — those are CLI surfaces,
    not repository methods. If we bypassed them with direct
    ``_insert_plan_and_tasks`` / ``_select_plan_with_tasks`` calls the
    test would not catch a regression in argument parsing, exit codes,
    or ``WHILLY_DATABASE_URL`` env-var reading. Calling
    :func:`run_plan_command` keeps the path identical to what an
    operator types in a shell.

Why the worker side of the loop uses the repository directly
    Phase 2 stops at the transport-less repository (TASK-019b is the
    local-worker wrapper, TASK-022 is the remote one — both later
    phases). So the test plays the worker manually: claim, start,
    complete. That mirrors the repository's public contract one method
    at a time and surfaces optimistic-locking drift (e.g. a START that
    misreads the version returned by CLAIM) the same way a real worker
    would hit it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_OK,
    run_plan_command,
)
from whilly.core.state_machine import Transition

pytestmark = DOCKER_REQUIRED


async def _run_cli(argv: Sequence[str]) -> int:
    """Execute :func:`run_plan_command` off the test event loop.

    The CLI handler internally calls :func:`asyncio.run`, which Python
    refuses to nest under another running loop. Our tests are async
    (``async def``) because they use the async ``db_pool`` fixture for
    seeding + assertions, so calling the handler directly here would
    raise ``RuntimeError: asyncio.run() cannot be called from a running
    event loop``. Routing through :func:`asyncio.to_thread` parks the
    handler on a worker thread that owns its own loop — the test loop
    stays unblocked and the handler's pool-lifecycle / ``json.dump``
    behaviour stays identical to a real ``whilly plan ...`` invocation.

    Returns the CLI exit code (the handler's own return value), so call
    sites can assert ``await _run_cli([...]) == EXIT_OK`` without
    sprinkling ``asyncio.to_thread`` everywhere.
    """
    return await asyncio.to_thread(run_plan_command, list(argv))


# Stable ids the assertions below can pin against. Three tasks is the
# smallest set that distinguishes "every task got the trio" from "the
# first task got three events and we assumed the rest did too" — a
# regression where only the leader-row writes events would silently
# pass on a single-task plan.
PLAN_ID = "plan-phase2-e2e"
PROJECT_NAME = "Phase 2 e2e gate"
TASK_IDS: tuple[str, ...] = ("T-001", "T-002", "T-003")


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    :func:`run_plan_command` reads the DSN from the environment because
    it's a one-shot CLI handler — the alternative would be threading the
    DSN through every call site. ``postgres_dsn`` already exports the
    var for its own bootstrap, but we restore the prior value on
    teardown so a leak from another test does not bleed into this one
    (this mirrors the local fixture in ``test_plan_io.py`` — kept
    duplicated rather than promoted to ``conftest.py`` so TASK-012 stays
    in its declared footprint; promotion is a TASK-029-era refactor).
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
def plan_payload() -> dict[str, Any]:
    """Return a v4 plan dict spanning three priorities and a dependency edge.

    The shape exercises the surfaces Phase 2 cares about without
    bloating into Phase 3 territory:

    * Three tasks → distinguishes per-row event emission from a
      "first task only" regression.
    * Mixed priorities (critical/high/medium) → ``_CLAIM_SQL``'s
      ``ORDER BY priority_rank, id`` will pick T-001 (critical) first;
      not asserted directly by this test, but documented so a future
      reader who sees the trio of priorities understands why.
    * One dependency edge (T-002 → T-001) → kept *only* so a future
      regression that drops dependencies in the round-trip surfaces;
      Phase 2 itself does not gate claims on dependencies (TASK-013c
      ``next_ready`` lives in Phase 3), so we deliberately bypass that
      ordering by claiming via ``claim_task`` directly which is
      dependency-blind.
    """
    return {
        "plan_id": PLAN_ID,
        "project": PROJECT_NAME,
        "tasks": [
            {
                "id": "T-001",
                "status": "PENDING",
                "priority": "critical",
                "description": "First — gets claimed first by ORDER BY priority.",
                "dependencies": [],
                "key_files": ["whilly/main.py"],
                "acceptance_criteria": ["entry point runs"],
                "test_steps": ["pytest -q"],
                "prd_requirement": "FR-2.4",
            },
            {
                "id": "T-002",
                "status": "PENDING",
                "priority": "high",
                "description": "Зависит от T-001 — UTF-8 round-trip canary.",
                "dependencies": ["T-001"],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
                "prd_requirement": "FR-2.5",
            },
            {
                "id": "T-003",
                "status": "PENDING",
                "priority": "medium",
                "description": "Trailing task — proves event trio fires for all rows.",
                "dependencies": [],
                "key_files": ["scripts/seed.sh"],
                "acceptance_criteria": ["seed.sh executable"],
                "test_steps": ["bash scripts/seed.sh --dry-run"],
                "prd_requirement": "",
            },
        ],
    }


@pytest.fixture
def plan_file(tmp_path: Path, plan_payload: dict[str, Any]) -> Path:
    """Write ``plan_payload`` to disk so ``import`` reads from a real file.

    Materialising the JSON (rather than driving ``parse_plan`` on the
    dict directly) keeps the test path identical to ``whilly plan
    import path/to/plan.json`` — i.e. an operator-realistic surface
    that exercises the file-read step in ``parse_plan`` too.
    """
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(plan_payload), encoding="utf-8")
    return target


# ─── helpers ─────────────────────────────────────────────────────────────


async def _seed_worker(pool: asyncpg.Pool, worker_id: str) -> None:
    """Insert one ``workers`` row so ``tasks.claimed_by`` FK is satisfied.

    ``claim_task`` requires the worker_id to already exist in
    ``workers`` (FK ``ON DELETE SET NULL``). In production
    ``POST /workers/register`` (TASK-021b) does this; for an
    integration test that doesn't go through transport we insert
    directly — same pattern as ``tests/integration/test_concurrent_claims.py``
    (see its ``_seed_workers`` for the rationale).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            f"host-{worker_id}",
            f"sha256:{worker_id}",
        )


def _decode_payload(raw: object) -> dict[str, object]:
    """Decode an asyncpg JSONB ``payload`` cell to a dict.

    asyncpg returns JSONB as raw ``str`` (JSON text) by default — the
    pool deliberately does not register a codec (TASK-009a). Mirrors
    the helper in ``test_concurrent_claims.py`` so this file stays
    self-contained without cross-file private imports.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        assert isinstance(decoded, dict), f"unexpected JSON shape: {decoded!r}"
        return decoded
    raise AssertionError(f"unexpected JSONB payload type {type(raw).__name__}: {raw!r}")


async def _drive_one_task_to_done(
    repo: TaskRepository,
    worker_id: str,
    plan_id: str,
) -> str:
    """Claim → start → complete one task; return its id.

    Simulates one iteration of the worker loop without going through
    transport (Phase 5). The version returned by ``claim_task`` feeds
    directly into ``start_task``; the version from ``start_task`` feeds
    into ``complete_task``. If any of those handovers drops or
    reuses the version counter, the optimistic-lock check in the next
    method raises :class:`VersionConflictError` and this helper bubbles
    it up — which is exactly the kind of boundary regression Phase 2
    needs to catch.
    """
    claimed = await repo.claim_task(worker_id, plan_id)
    assert claimed is not None, "claim_task returned None — plan unexpectedly drained mid-test"
    started = await repo.start_task(claimed.id, claimed.version)
    completed = await repo.complete_task(started.id, started.version)
    # Sanity: the same row threaded through all three methods.
    assert claimed.id == started.id == completed.id, (
        f"task id mutated mid-pipeline: claim={claimed.id} start={started.id} complete={completed.id}"
    )
    return completed.id


# ─── tests ───────────────────────────────────────────────────────────────


async def test_phase2_e2e_emits_claim_start_complete_per_task(
    db_pool: asyncpg.Pool,
    database_url: str,  # noqa: ARG001  — sets WHILLY_DATABASE_URL for run_plan_command
    plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SC for TASK-012: each task ends up with a CLAIM, START, COMPLETE event row.

    The trio must appear *in order* per task (CLAIM before START before
    COMPLETE) — the in-order guarantee comes from each transition
    happening inside its own ``async with conn.transaction()`` and
    being committed before the next is even attempted, but it's
    cheap to assert and would catch a future "fire-and-forget event
    insert" regression that violated the contract.

    Why fetch events ordered by (task_id, created_at) rather than
    grouped in Python: the index ``ix_events_task_id_created_at``
    promises this exact ordering at the SQL level, so leaning on it
    doubles as a coverage check that the index is still pulling its
    weight (a missing index would make this query plan a SEQSCAN +
    in-memory sort — same result, but a hint that the event table is
    growing without operational support).
    """
    # Step 1: import the plan — succeeds, populates plans+tasks tables.
    rc = await _run_cli(["import", str(plan_file)])
    assert rc == EXIT_OK, f"plan import returned {rc} (expected {EXIT_OK})"
    capsys.readouterr()  # Drain so unrelated success banners don't leak into later assertions.

    # Step 2: seed one worker and run the loop end-to-end. Single worker
    # (rather than three concurrent ones) keeps assertions trivial: a
    # multi-worker variant lives in ``test_concurrent_claims.py``.
    worker_id = "w-phase2"
    await _seed_worker(db_pool, worker_id)
    repo = TaskRepository(db_pool)

    completed_ids: list[str] = []
    for _ in TASK_IDS:
        completed_ids.append(await _drive_one_task_to_done(repo, worker_id, PLAN_ID))

    # Every seeded task got driven to DONE — no losses, no duplicates.
    assert sorted(completed_ids) == sorted(TASK_IDS), (
        f"loop did not cover every task: completed={sorted(completed_ids)} expected={sorted(TASK_IDS)}"
    )

    # Step 3: the events table reflects the trio for each task in order.
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
            f"task {tid!r} should have exactly 3 transition events (CLAIM, START, COMPLETE); got {len(events)}: "
            f"{[e['event_type'] for e in events]}"
        )
        actual_sequence = tuple(e["event_type"] for e in events)
        assert actual_sequence == expected_sequence, (
            f"task {tid!r} event order is {actual_sequence}; expected {expected_sequence}"
        )

        # Each transition's payload must include the post-update version
        # so the audit log is replayable. CLAIM also pins worker_id.
        claim_payload = _decode_payload(events[0]["payload"])
        assert claim_payload.get("worker_id") == worker_id, (
            f"CLAIM payload for {tid!r} missing/wrong worker_id: {claim_payload!r}"
        )
        assert claim_payload.get("version") == 1, f"CLAIM payload version drift: {claim_payload!r}"
        start_payload = _decode_payload(events[1]["payload"])
        assert start_payload.get("version") == 2, f"START payload version drift: {start_payload!r}"
        complete_payload = _decode_payload(events[2]["payload"])
        assert complete_payload.get("version") == 3, f"COMPLETE payload version drift: {complete_payload!r}"


async def test_phase2_e2e_export_after_completion_shows_done(
    db_pool: asyncpg.Pool,
    database_url: str,  # noqa: ARG001
    plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SC for TASK-012: ``whilly plan export`` after the loop shows every task DONE.

    Closes the e2e loop on the *export* side — proves the SELECT path
    in :mod:`whilly.cli.plan` reads the same status the repository
    wrote, and that ``serialize_plan`` does not silently rewrite
    terminal states. A regression in either would show up here as a
    task whose exported status is not ``DONE``, with the failing id
    named directly in the assertion message.

    We deliberately re-run the import → loop → export pipeline in this
    test rather than reusing state from the previous one. Tests are
    independent by design — the ``db_pool`` fixture TRUNCATEs at setup,
    so cross-test state bleeding is impossible — and a single
    test-failure should localise to a single assertion, not "tests
    after this one fail because state was not reset".
    """
    assert await _run_cli(["import", str(plan_file)]) == EXIT_OK
    capsys.readouterr()

    worker_id = "w-phase2-export"
    await _seed_worker(db_pool, worker_id)
    repo = TaskRepository(db_pool)
    for _ in TASK_IDS:
        await _drive_one_task_to_done(repo, worker_id, PLAN_ID)

    # Export to stdout, parse, and inspect every task.
    assert await _run_cli(["export", PLAN_ID]) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip(), "export must print non-empty JSON to stdout"

    payload = json.loads(captured.out)
    assert payload["plan_id"] == PLAN_ID, f"export plan_id drift: got {payload.get('plan_id')!r}, expected {PLAN_ID!r}"
    assert payload["project"] == PROJECT_NAME

    statuses_by_id = {task["id"]: task["status"] for task in payload["tasks"]}
    assert set(statuses_by_id) == set(TASK_IDS), (
        f"export task set mismatch: missing={set(TASK_IDS) - set(statuses_by_id)}, "
        f"extra={set(statuses_by_id) - set(TASK_IDS)}"
    )
    not_done = {tid: status for tid, status in statuses_by_id.items() if status != "DONE"}
    assert not not_done, f"export shows non-DONE status after full e2e completion: {not_done}"

    # ``version`` must have advanced by exactly 3 per task (CLAIM=1, START=2,
    # COMPLETE=3 starting from the ``parse_plan`` default of 0). Pinning
    # the counter — not just the terminal status — protects against a
    # future SQL refactor that flips status without bumping version,
    # which would silently break optimistic locking for any subsequent
    # transition (re-claim after release, FAIL backstop, etc.).
    bad_versions = {task["id"]: task["version"] for task in payload["tasks"] if task["version"] != 3}
    assert not bad_versions, (
        f"version counter not advanced by exactly 3 (CLAIM/START/COMPLETE) per task: {bad_versions}"
    )


async def test_phase2_e2e_tasks_table_consistent_with_export(
    db_pool: asyncpg.Pool,
    database_url: str,  # noqa: ARG001
    plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cross-check: the ``tasks`` table and the export agree on terminal state.

    Belt-and-braces. Two independent observers — the SQL row directly
    and the export-via-CLI — must report the same terminal status,
    cleared ``claimed_by`` (well, retained pointer is a non-issue
    because DONE rows can keep claimed_by for audit), and matching
    version. A divergence means the SELECT path in
    :func:`_select_plan_with_tasks` is reading from the wrong column or
    applying a stale projection — exactly the boundary bug a Phase Gate
    is for.

    The ``claimed_by`` assertion is intentionally weak — DONE rows
    *retain* their claim pointer so post-mortems can answer "who ran
    this?". A regression that nulls it on completion would not violate
    any AC; we only assert the row is non-null, not equal to a
    specific worker, so a future change that decides to clear
    ``claimed_by`` on DONE doesn't false-positive this test.
    """
    assert await _run_cli(["import", str(plan_file)]) == EXIT_OK
    capsys.readouterr()

    worker_id = "w-phase2-cross"
    await _seed_worker(db_pool, worker_id)
    repo = TaskRepository(db_pool)
    for _ in TASK_IDS:
        await _drive_one_task_to_done(repo, worker_id, PLAN_ID)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, status, version, claimed_by FROM tasks WHERE plan_id = $1 ORDER BY id",
            PLAN_ID,
        )

    db_view = {row["id"]: dict(row) for row in rows}

    assert await _run_cli(["export", PLAN_ID]) == EXIT_OK
    export_payload = json.loads(capsys.readouterr().out)
    export_view = {task["id"]: task for task in export_payload["tasks"]}

    assert set(db_view) == set(export_view) == set(TASK_IDS), (
        f"tasks-set divergence: db={set(db_view)} export={set(export_view)} expected={set(TASK_IDS)}"
    )
    for tid in TASK_IDS:
        assert db_view[tid]["status"] == "DONE" == export_view[tid]["status"], (
            f"status divergence on {tid}: db={db_view[tid]['status']!r} export={export_view[tid]['status']!r}"
        )
        assert db_view[tid]["version"] == 3 == export_view[tid]["version"], (
            f"version divergence on {tid}: db={db_view[tid]['version']} export={export_view[tid]['version']}"
        )
        # DONE row retains claimed_by for audit (see method docstring).
        assert db_view[tid]["claimed_by"] is not None, f"task {tid} lost its claimed_by on DONE — audit trail broken"
