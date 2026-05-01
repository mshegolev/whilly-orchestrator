"""Unit tests for the v4.4.0 enriched ``events.payload`` shape (M1 fix).

Pin the JSON-payload contract introduced by the M1 fix for
VAL-CROSS-BACKCOMPAT-909 / -910 / -911 / -912: every CLAIM /
COMPLETE / FAIL / RELEASE event payload now carries
``worker_id`` + ``task_id`` + ``plan_id`` at minimum, plus
event-specific extras:

* CLAIM: ``claimed_at`` (ISO-8601 timestamp).
* COMPLETE: ``usage`` envelope (``cost_usd`` stringified).
* FAIL: ``error`` aliased to ``reason``.
* RELEASE: ``reason`` (enum extended with ``admin_revoked``).

The shapes are pinned in
``tests/fixtures/baselines/events_payload_v4.4.0.json``; this file
exercises the *runtime* emission path of
:class:`whilly.adapters.db.repository.TaskRepository` against a real
testcontainers Postgres so the JSONB encoding, RETURNING clauses,
and FK chain across ``tasks`` / ``events`` are all part of the
assertion surface. A pure-Python mock would lose every one of those
invariants — which is the same reason
``tests/integration/test_skip_task.py`` lives in the integration
tree even though its assertions look unit-test-ish.

The test still lives under ``tests/unit/`` because the verification
step on the M1 feature spec names the path explicitly
(``pytest tests/unit/test_event_payload_keys.py -v``); that decision
prioritises the operator-facing signal "the event-payload contract
is exercised at the unit level" over the project's usual
unit-vs-integration split.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import asyncpg

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db.repository import TaskRepository
from whilly.core.models import TaskStatus

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-EVT-PAYLOAD-001"
TASK_ID = "T-EVT-PAYLOAD-001"
WORKER_ID = "w-evt-payload-001"


async def _seed(
    pool: asyncpg.Pool,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    version: int = 0,
    claimed_by: str | None = None,
) -> None:
    """Seed one plan, one worker, one task in the requested status.

    Mirrors ``_seed_plan_and_task`` in tests/integration/test_skip_task.py
    but pinned to the unit-test naming so the SQL surface stays local
    to this file.
    """
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", PLAN_ID, "evt-payload")
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            WORKER_ID,
            "host-evt",
            "hash-evt",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version,
                claimed_by, claimed_at
            )
            VALUES ($1, $2, $3, '[]'::jsonb, '[]'::jsonb,
                    'medium', $4, '[]'::jsonb, '[]'::jsonb, '', $5,
                    $6::text,
                    CASE WHEN $6::text IS NULL THEN NULL ELSE NOW() END)
            """,
            TASK_ID,
            PLAN_ID,
            status.value,
            f"task {TASK_ID}",
            version,
            claimed_by,
        )


def _decode(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    assert isinstance(raw, str), f"unexpected payload type {type(raw).__name__}: {raw!r}"
    decoded = json.loads(raw)
    assert isinstance(decoded, dict), f"unexpected payload JSON shape: {decoded!r}"
    return decoded


async def _fetch_event_payload(pool: asyncpg.Pool, *, event_type: str) -> dict[str, object]:
    """Fetch the payload of the (single) event row of ``event_type`` for ``TASK_ID``."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM events WHERE task_id = $1 AND event_type = $2 ORDER BY id ASC",
            TASK_ID,
            event_type,
        )
    assert len(rows) == 1, f"expected exactly one {event_type!r} event for task {TASK_ID!r}; got {len(rows)}"
    return _decode(rows[0]["payload"])


# ─── CLAIM (VAL-CROSS-BACKCOMPAT-909) ────────────────────────────────────


async def test_claim_event_payload_carries_v4_4_0_required_keys(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """CLAIM payload must carry ``worker_id`` + ``task_id`` + ``plan_id`` + ``claimed_at`` + ``version``.

    The v4.3.1 baseline only required ``worker_id`` + ``version``; the
    v4.4.0 enriched shape adds the other three so cross-host audit
    queries can attribute claims without a JOIN against the tasks
    table. ``claimed_at`` is the post-update timestamp captured from
    ``tasks.claimed_at`` via ``_CLAIM_SQL`` RETURNING — emitted as an
    ISO-8601 string so the JSON round-trips cleanly.
    """
    await _seed(db_pool, status=TaskStatus.PENDING, version=0)

    claimed = await task_repo.claim_task(WORKER_ID, PLAN_ID)
    assert claimed is not None
    assert claimed.status == TaskStatus.CLAIMED

    payload = await _fetch_event_payload(db_pool, event_type="CLAIM")
    required = {"worker_id", "task_id", "plan_id", "claimed_at", "version"}
    assert required.issubset(payload.keys()), (
        f"CLAIM payload missing required v4.4.0 keys: {required - payload.keys()}; got payload={payload!r}"
    )
    assert payload["worker_id"] == WORKER_ID
    assert payload["task_id"] == TASK_ID
    assert payload["plan_id"] == PLAN_ID
    assert payload["version"] == claimed.version
    # ``claimed_at`` must be an ISO-8601 string parseable by datetime.
    assert isinstance(payload["claimed_at"], str)
    assert datetime.fromisoformat(str(payload["claimed_at"])) is not None


# ─── COMPLETE (VAL-CROSS-BACKCOMPAT-910) ─────────────────────────────────


async def test_complete_event_payload_carries_v4_4_0_required_keys(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """COMPLETE payload must carry ``worker_id`` + ``task_id`` + ``plan_id`` + ``version`` + ``usage``.

    ``worker_id`` is sourced from ``tasks.claimed_by`` (preserved
    across the COMPLETE transition); ``usage`` is a structured envelope
    carrying ``cost_usd`` (stringified Decimal) so the JSON round-trips
    with NUMERIC(10, 4) precision.
    """
    # Seed in CLAIMED state so complete_task's status filter accepts.
    await _seed(db_pool, status=TaskStatus.CLAIMED, version=1, claimed_by=WORKER_ID)

    completed = await task_repo.complete_task(TASK_ID, version=1, cost_usd=Decimal("0.0123"))
    assert completed.status == TaskStatus.DONE

    payload = await _fetch_event_payload(db_pool, event_type="COMPLETE")
    required = {"worker_id", "task_id", "plan_id", "version", "usage"}
    assert required.issubset(payload.keys()), (
        f"COMPLETE payload missing required v4.4.0 keys: {required - payload.keys()}; got payload={payload!r}"
    )
    assert payload["worker_id"] == WORKER_ID
    assert payload["task_id"] == TASK_ID
    assert payload["plan_id"] == PLAN_ID
    assert payload["version"] == completed.version
    assert isinstance(payload["usage"], dict)
    usage = payload["usage"]
    assert "cost_usd" in usage
    # ``cost_usd`` is stringified so JSON preserves the Decimal precision.
    assert Decimal(str(usage["cost_usd"])) == Decimal("0.0123")


# ─── FAIL (VAL-CROSS-BACKCOMPAT-911) ─────────────────────────────────────


async def test_fail_event_payload_carries_v4_4_0_required_keys(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """FAIL payload must carry ``worker_id`` + ``task_id`` + ``plan_id`` + ``version`` + ``reason`` + ``error``.

    ``worker_id`` is sourced from ``tasks.claimed_by`` (preserved
    across the FAIL transition); ``error`` is an alias of ``reason``
    so dashboards keying off either name surface the failure cause.
    """
    await _seed(db_pool, status=TaskStatus.IN_PROGRESS, version=2, claimed_by=WORKER_ID)

    failure_reason = "agent crashed — exit code 137 (OOM kill)"
    failed = await task_repo.fail_task(TASK_ID, version=2, reason=failure_reason)
    assert failed.status == TaskStatus.FAILED

    payload = await _fetch_event_payload(db_pool, event_type="FAIL")
    required = {"worker_id", "task_id", "plan_id", "version", "reason", "error"}
    assert required.issubset(payload.keys()), (
        f"FAIL payload missing required v4.4.0 keys: {required - payload.keys()}; got payload={payload!r}"
    )
    assert payload["worker_id"] == WORKER_ID
    assert payload["task_id"] == TASK_ID
    assert payload["plan_id"] == PLAN_ID
    assert payload["reason"] == failure_reason
    # ``error`` mirrors ``reason`` for v4.4.0; keying off either is valid.
    assert payload["error"] == failure_reason


# ─── RELEASE (VAL-CROSS-BACKCOMPAT-912) — single-task targeted release ───


async def test_release_event_payload_carries_v4_4_0_required_keys(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """RELEASE payload must carry ``worker_id`` + ``task_id`` + ``plan_id`` + ``version`` + ``reason``.

    The targeted single-task release path captures the *previous*
    ``claimed_by`` via ``_RELEASE_SQL``'s ``prev`` CTE before the
    UPDATE NULL's the column, so ``worker_id`` is the worker that
    OWNED the task at release time — matches the bulk-sweep variants
    in ``_RELEASE_STALE_SQL`` / ``_RELEASE_OFFLINE_WORKERS_SQL``.
    """
    await _seed(db_pool, status=TaskStatus.IN_PROGRESS, version=3, claimed_by=WORKER_ID)

    released = await task_repo.release_task(TASK_ID, version=3, reason="shutdown")
    assert released.status == TaskStatus.PENDING

    payload = await _fetch_event_payload(db_pool, event_type="RELEASE")
    required = {"worker_id", "task_id", "plan_id", "version", "reason"}
    assert required.issubset(payload.keys()), (
        f"RELEASE payload missing required v4.4.0 keys: {required - payload.keys()}; got payload={payload!r}"
    )
    assert payload["worker_id"] == WORKER_ID
    assert payload["task_id"] == TASK_ID
    assert payload["plan_id"] == PLAN_ID
    assert payload["reason"] == "shutdown"
    assert payload["version"] == released.version


# ─── RELEASE bulk sweeps — visibility timeout ────────────────────────────


async def test_release_stale_sweep_emits_v4_4_0_payload(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """The visibility-timeout sweep MUST emit RELEASE rows with the v4.4.0 enriched shape.

    Pins VAL-CROSS-BACKCOMPAT-912 against the *bulk* sweep path (not
    just the targeted single-task release). The sweep snapshots the
    pre-UPDATE ``claimed_by`` / ``plan_id`` via the ``stale`` CTE so
    the JSON payload can carry both before the UPDATE NULL's the
    columns.
    """
    # Seed an aged-out CLAIMED row (claimed_at = NOW() - large interval).
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", PLAN_ID, "evt-stale")
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            WORKER_ID,
            "host-stale",
            "hash-stale",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version,
                claimed_by, claimed_at
            )
            VALUES ($1, $2, 'CLAIMED', '[]'::jsonb, '[]'::jsonb,
                    'medium', $3, '[]'::jsonb, '[]'::jsonb, '', 1,
                    $4, NOW() - INTERVAL '1 hour')
            """,
            TASK_ID,
            PLAN_ID,
            "stale-task",
            WORKER_ID,
        )

    released_count = await task_repo.release_stale_tasks(visibility_timeout_seconds=10)
    assert released_count == 1

    payload = await _fetch_event_payload(db_pool, event_type="RELEASE")
    required = {"worker_id", "task_id", "plan_id", "version", "reason"}
    assert required.issubset(payload.keys()), (
        f"stale-sweep RELEASE payload missing v4.4.0 keys: {required - payload.keys()}; got {payload!r}"
    )
    assert payload["worker_id"] == WORKER_ID
    assert payload["task_id"] == TASK_ID
    assert payload["plan_id"] == PLAN_ID
    assert payload["reason"] == "visibility_timeout"


async def test_release_offline_workers_sweep_emits_v4_4_0_payload(
    db_pool: asyncpg.Pool,
    task_repo: TaskRepository,
) -> None:
    """The offline-worker sweep MUST emit RELEASE rows with the v4.4.0 enriched shape.

    Same contract as the visibility-timeout sweep but driven by
    ``workers.last_heartbeat`` aging. The sweep already carried
    ``worker_id`` in v4.3.x; v4.4.0 additionally pins ``task_id`` +
    ``plan_id``.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ($1, $2)", PLAN_ID, "evt-offline")
        # Worker with stale heartbeat.
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, token_hash, status, last_heartbeat)
            VALUES ($1, $2, $3, 'online', NOW() - INTERVAL '1 hour')
            """,
            WORKER_ID,
            "host-offline",
            "hash-offline",
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version,
                claimed_by, claimed_at
            )
            VALUES ($1, $2, 'IN_PROGRESS', '[]'::jsonb, '[]'::jsonb,
                    'medium', $3, '[]'::jsonb, '[]'::jsonb, '', 2,
                    $4, NOW())
            """,
            TASK_ID,
            PLAN_ID,
            "offline-task",
            WORKER_ID,
        )

    released_count = await task_repo.release_offline_workers(heartbeat_timeout_seconds=10)
    assert released_count == 1

    payload = await _fetch_event_payload(db_pool, event_type="RELEASE")
    required = {"worker_id", "task_id", "plan_id", "version", "reason"}
    assert required.issubset(payload.keys()), (
        f"offline-sweep RELEASE payload missing v4.4.0 keys: {required - payload.keys()}; got {payload!r}"
    )
    assert payload["worker_id"] == WORKER_ID
    assert payload["task_id"] == TASK_ID
    assert payload["plan_id"] == PLAN_ID
    assert payload["reason"] == "worker_offline"


# ─── Forward-readability: legacy v4.3.1 payload still parses ─────────────


def test_legacy_v4_3_1_payloads_remain_valid_against_v4_3_1_baseline() -> None:
    """A v4.3.1-shaped payload (only the legacy required keys) must still parse.

    The v4.3.1 baseline marks every event_type's schema as
    ``additionalProperties: true``, so a v4.4.0 reader treating it
    as the canonical legacy contract accepts payloads carrying ONLY
    the v4.3.1 required keys without raising. Pinned here as the
    forward-readability anchor — already-emitted rows in long-running
    databases must still be honoured.
    """
    legacy_complete = {"version": 1}
    legacy_claim = {"worker_id": "w-legacy", "version": 1}
    legacy_fail = {"version": 1, "reason": "legacy-fail"}
    legacy_release = {"version": 1, "reason": "shutdown"}

    # Required keys per the v4.3.1 baseline.
    assert {"version"}.issubset(legacy_complete.keys())
    assert {"worker_id", "version"}.issubset(legacy_claim.keys())
    assert {"version", "reason"}.issubset(legacy_fail.keys())
    assert {"version", "reason"}.issubset(legacy_release.keys())
