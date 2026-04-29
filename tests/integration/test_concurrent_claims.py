"""SC-1 verification: concurrent ``claim_task`` is safe under contention (TASK-011).

Acceptance criterion (PRD SC-1, NFR-1, NFR-2):

    100 параллельных claim_task в asyncio.gather, проверить что каждая
    задача взята ровно одним воркером (no duplicates, no losses).

This test is the proof that
:meth:`whilly.adapters.db.repository.TaskRepository.claim_task`'s
``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` + atomic UPDATE pattern
actually serialises N concurrent callers onto N distinct rows. It is
the single most load-bearing concurrency invariant in v4.0: every
remote worker, every local worker, every dashboard refresh that goes
through ``claim_task`` depends on this.

Why three sub-tests rather than one
-----------------------------------
SC-1 has two clauses ("each task taken exactly once" + "no losses, no
duplicates"). Splitting them into focused tests makes the failure
mode obvious if any of them regress:

* :func:`test_one_hundred_concurrent_claims_each_task_claimed_exactly_once`
  — equal-counts contention (100 tasks / 100 workers). Catches
  duplicate claims (two workers see the same row id) and lost claims
  (a worker gets ``None`` even though PENDING rows existed).

* :func:`test_excess_workers_get_none_no_duplicate_claims`
  — short-queue contention (50 tasks / 100 workers). Excess workers
  must get ``None``, not crash, and the 50 winners must still be
  unique.

* :func:`test_concurrent_claims_audit_log_consistent_with_task_state`
  — audit-integrity proof. The ``tasks`` UPDATE and the ``events``
  INSERT run in one transaction; this test catches the failure mode
  where the status flip commits but the audit row silently doesn't
  (FK-cascade race, transaction-abort path that retries the UPDATE
  without the INSERT, etc.).

Why the test does not assert anything about ``priority`` ordering
under contention. ``claim_task``'s ORDER BY is best-effort — the
precise picking order under SKIP LOCKED contention is not part of
its public contract (PRD FR-3.4 puts richer ordering in the pure
scheduler, TASK-013c). Asserting on it here would couple the test to
an implementation detail that may legitimately reshuffle.

Why the seeding helpers go through raw SQL on a borrowed pool
connection rather than through repository methods. The repository
deliberately does not expose "create a plan" or "create N tasks"
— those are CLI concerns (TASK-010b). For an integration test
focused on the *claim* path, a direct INSERT is the simplest
non-coupling fixture; otherwise we'd need TASK-010b finished before
TASK-011 could run, which the dependency graph in tasks.json
explicitly rejects.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.core.models import PlanId

# Module-level marker so pytest skips the whole file (rather than each
# test individually) when Docker is unavailable. ``postgres_dsn`` also
# calls ``pytest.skip`` from inside, but the marker gives a cleaner
# collection-time skip with a single line in the report.
pytestmark = DOCKER_REQUIRED


# Hardcoded counts. SC-1 names "100" explicitly; bumping this turns the
# test into a load benchmark, which is not what the AC asks for.
N_TASKS = 100
N_WORKERS = 100


async def _seed_plan_with_tasks(
    pool: asyncpg.Pool,
    plan_id: PlanId,
    n_tasks: int,
    *,
    priority: str = "medium",
) -> list[str]:
    """Insert one plan and ``n_tasks`` PENDING task rows; return the ids.

    Single transaction so a half-seeded DB cannot leak into the test if
    the seeding itself fails. ``executemany`` keeps the round-trip cost
    proportional to one INSERT regardless of ``n_tasks``.

    Task ids are deterministic (``T-0000`` … ``T-0099``) so assertions
    can compare against ``set(task_ids)`` rather than fetching the
    canonical id list back from Postgres.
    """
    task_ids = [f"T-{i:04d}" for i in range(n_tasks)]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2)",
                plan_id,
                f"plan for {plan_id}",
            )
            await conn.executemany(
                "INSERT INTO tasks (id, plan_id, status, priority) VALUES ($1, $2, 'PENDING', $3)",
                [(tid, plan_id, priority) for tid in task_ids],
            )
    return task_ids


async def _seed_workers(pool: asyncpg.Pool, n: int) -> list[str]:
    """Insert ``n`` worker rows and return their ids.

    The FK on ``tasks.claimed_by`` requires every ``claim_task`` call's
    ``worker_id`` to already exist in ``workers``; in production this
    happens via ``POST /workers/register`` (TASK-021b). For tests we
    insert directly so TASK-011 doesn't depend on TASK-021.
    """
    worker_ids = [f"w-{i:04d}" for i in range(n)]
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            [(wid, f"host-{wid}", f"sha256:{wid}") for wid in worker_ids],
        )
    return worker_ids


def _decode_event_payload(raw: object) -> dict[str, object]:
    """Decode an asyncpg JSONB ``payload`` column to a dict.

    Mirrors the helper in :mod:`whilly.adapters.db.repository`: asyncpg
    returns JSONB as ``str`` (raw JSON text) by default, but a future
    codec registration on the pool would surface a pre-decoded ``dict``
    instead. Accepts both so the test stays robust to that change.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        assert isinstance(decoded, dict), f"unexpected JSON shape: {decoded!r}"
        return decoded
    raise AssertionError(f"unexpected JSONB payload type {type(raw).__name__}: {raw!r}")


async def test_one_hundred_concurrent_claims_each_task_claimed_exactly_once(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """100 workers race for 100 tasks — every task is claimed exactly once.

    This is the canonical SC-1 case. Under perfect equal-count
    contention every worker must end up with a distinct task; if the
    SKIP LOCKED + atomic UPDATE pattern is correct, this test is
    deterministic-pass; if SKIP LOCKED is regressed (e.g. someone
    swaps it for a plain SELECT), at least one duplicate id surfaces
    in ~100% of runs.
    """
    plan_id = "PLAN-SC1-EQUAL"
    task_ids = await _seed_plan_with_tasks(db_pool, plan_id, N_TASKS)
    worker_ids = await _seed_workers(db_pool, N_WORKERS)

    # asyncio.gather schedules all 100 coroutines onto the same loop;
    # the asyncpg pool (max_size=20) lets up to 20 transactions run on
    # the wire concurrently, with the rest queueing. SKIP LOCKED has to
    # serialise the in-flight slice onto distinct rows on each batch —
    # if it doesn't, duplicates surface here.
    results = await asyncio.gather(*(task_repo.claim_task(wid, plan_id) for wid in worker_ids))

    # No losses: every worker got a task back.
    successes = [t for t in results if t is not None]
    assert len(successes) == N_WORKERS, (
        f"expected {N_WORKERS} successful claims, got {len(successes)}; None count = {results.count(None)}"
    )

    # No duplicates: every claimed id is unique.
    claimed_ids = [t.id for t in successes]
    duplicates = sorted({i for i in claimed_ids if claimed_ids.count(i) > 1})
    assert not duplicates, f"duplicate claims detected — same task id returned to multiple workers: {duplicates}"

    # Set equality: claimed set is exactly the seeded set.
    assert set(claimed_ids) == set(task_ids), (
        f"claimed/seeded set mismatch: missing={set(task_ids) - set(claimed_ids)}, "
        f"extra={set(claimed_ids) - set(task_ids)}"
    )

    # Optimistic-locking counter incremented exactly once per row
    # (PENDING:version=0 → CLAIMED:version=1).
    bad_versions = [(t.id, t.version) for t in successes if t.version != 1]
    assert not bad_versions, f"version increment broken: {bad_versions}"

    # All rows in the DB now have status=CLAIMED.
    async with db_pool.acquire() as conn:
        statuses = {
            row["status"]
            for row in await conn.fetch(
                "SELECT status FROM tasks WHERE plan_id = $1",
                plan_id,
            )
        }
        assert statuses == {"CLAIMED"}, f"expected all CLAIMED, got {statuses}"

        # Audit table holds exactly one CLAIM event per claimed row.
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE event_type = 'CLAIM' AND task_id = ANY($1::text[])",
            task_ids,
        )
        assert event_count == N_TASKS, f"expected {N_TASKS} CLAIM events, got {event_count}"


async def test_excess_workers_get_none_no_duplicate_claims(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """50 tasks / 100 workers — 50 winners + 50 ``None``, no overlap.

    Catches the "no losses" half of SC-1 in the asymmetric case: when
    the queue is shorter than the worker pool, the excess claimers
    must each receive ``None`` (the canonical "no work right now"
    response), and the 50 winners must still be unique.
    """
    plan_id = "PLAN-SC1-EXCESS"
    n_tasks = 50
    task_ids = await _seed_plan_with_tasks(db_pool, plan_id, n_tasks)
    worker_ids = await _seed_workers(db_pool, N_WORKERS)

    results = await asyncio.gather(*(task_repo.claim_task(wid, plan_id) for wid in worker_ids))

    successes = [t for t in results if t is not None]
    nones = [r for r in results if r is None]
    assert len(successes) == n_tasks, (
        f"expected {n_tasks} successful claims, got {len(successes)} (Nones: {len(nones)})"
    )
    assert len(nones) == N_WORKERS - n_tasks, (
        f"expected {N_WORKERS - n_tasks} None responses for excess workers, got {len(nones)}"
    )

    claimed_ids = [t.id for t in successes]
    assert len(set(claimed_ids)) == len(claimed_ids), (
        f"duplicate claims under excess-worker contention: "
        f"{sorted({i for i in claimed_ids if claimed_ids.count(i) > 1})}"
    )
    assert set(claimed_ids) == set(task_ids), "winning ids mismatch seeded ids"

    # The excess workers must not have left any orphan CLAIM events.
    async with db_pool.acquire() as conn:
        event_count = await conn.fetchval("SELECT COUNT(*) FROM events WHERE event_type = 'CLAIM'")
        assert event_count == n_tasks, (
            f"events table holds {event_count} CLAIM rows but only {n_tasks} claims succeeded — "
            "orphan events from excess workers leaked"
        )


async def test_concurrent_claims_audit_log_consistent_with_task_state(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """Every CLAIMED task has exactly one CLAIM event with matching ``worker_id``.

    Catches the audit-drift failure mode: the ``tasks`` UPDATE commits
    but the ``events`` INSERT silently doesn't (FK violation on a
    deleted task, transaction-abort retry that re-runs only the
    UPDATE, etc.). Both writes share one transaction in
    :meth:`TaskRepository.claim_task`, so observers must see *both*
    or *neither* — never just the status flip with no audit row, and
    never an event whose ``worker_id`` payload disagrees with
    ``tasks.claimed_by``.
    """
    plan_id = "PLAN-SC1-AUDIT"
    n = 100
    task_ids = await _seed_plan_with_tasks(db_pool, plan_id, n)
    worker_ids = await _seed_workers(db_pool, n)

    results = await asyncio.gather(*(task_repo.claim_task(wid, plan_id) for wid in worker_ids))
    successes = [t for t in results if t is not None]
    assert len(successes) == n, "precondition: equal-count contention should fully succeed"

    async with db_pool.acquire() as conn:
        owners = await conn.fetch(
            "SELECT id, claimed_by FROM tasks WHERE plan_id = $1 ORDER BY id",
            plan_id,
        )
        events = await conn.fetch(
            "SELECT task_id, payload FROM events "
            "WHERE event_type = 'CLAIM' AND task_id = ANY($1::text[]) "
            "ORDER BY task_id, created_at",
            task_ids,
        )

    # Every row has a non-NULL claimed_by — the CHECK constraint
    # ck_tasks_claim_pair_consistent guarantees claimed_at is also set.
    null_owners = [row["id"] for row in owners if row["claimed_by"] is None]
    assert not null_owners, f"NULL claimed_by detected after concurrent claim: {null_owners}"

    # And every claimed_by must be a worker we seeded — no phantom owners.
    seeded = set(worker_ids)
    rogue = [row["claimed_by"] for row in owners if row["claimed_by"] not in seeded]
    assert not rogue, f"unknown claimed_by values: {rogue}"

    # Each task has exactly one CLAIM event.
    by_task: dict[str, list[asyncpg.Record]] = {}
    for ev in events:
        by_task.setdefault(ev["task_id"], []).append(ev)
    for tid in task_ids:
        ev_count = len(by_task.get(tid, []))
        assert ev_count == 1, f"task {tid} should have exactly one CLAIM event, got {ev_count}"

    # Each event's payload.worker_id matches the tasks.claimed_by for that task.
    owner_by_id = {row["id"]: row["claimed_by"] for row in owners}
    drift: list[str] = []
    for ev in events:
        payload = _decode_event_payload(ev["payload"])
        if payload.get("worker_id") != owner_by_id[ev["task_id"]]:
            drift.append(
                f"{ev['task_id']}: event worker={payload.get('worker_id')!r} vs "
                f"tasks.claimed_by={owner_by_id[ev['task_id']]!r}"
            )
    assert not drift, "audit drift between events.payload.worker_id and tasks.claimed_by:\n  " + "\n  ".join(drift)


async def test_repeat_claims_after_exhaustion_return_none(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """Once the queue is drained, every subsequent ``claim_task`` returns ``None``.

    Belt-and-braces idempotence check: after the first wave drains the
    plan, a *second* wave of claim_task calls (different workers, same
    plan) must all see an empty PENDING set and return ``None``. Catches
    the failure mode where SKIP LOCKED somehow leaves rows visible to a
    later transaction (ghost-locking, MVCC visibility issues).
    """
    plan_id = "PLAN-SC1-DRAIN"
    n = 30  # smaller — this test cares about the second wave, not throughput
    await _seed_plan_with_tasks(db_pool, plan_id, n)
    workers_first = await _seed_workers(db_pool, n)

    first_wave = await asyncio.gather(*(task_repo.claim_task(wid, plan_id) for wid in workers_first))
    assert all(t is not None for t in first_wave), "first-wave should drain the queue completely"

    # Second wave of brand-new workers — no PENDING rows left, all must get None.
    async with db_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            # Each second-wave worker gets a unique token_hash because
            # migration 004 (TASK-101) added a partial UNIQUE index over
            # ``workers.token_hash`` (where token_hash IS NOT NULL); a
            # shared placeholder would now violate that constraint.
            [(f"w-second-{i:03d}", f"host-second-{i}", f"sha256:second-{i:03d}") for i in range(10)],
        )
    second_workers = [f"w-second-{i:03d}" for i in range(10)]
    second_wave = await asyncio.gather(*(task_repo.claim_task(wid, plan_id) for wid in second_workers))
    assert all(t is None for t in second_wave), (
        f"second-wave should return all None on drained queue, got: {[t.id for t in second_wave if t is not None]}"
    )
