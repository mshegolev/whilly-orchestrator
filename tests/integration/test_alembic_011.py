"""Integration tests for migration 011_events_notify_trigger (M3 mission).

Pins the data-layer half of the M3 ``m3-migration-011`` feature: the
alembic migration that creates the ``whilly_notify_event()`` plpgsql
function and the ``tr_events_notify`` AFTER INSERT trigger on
``events``. Mirrors the structure of
:mod:`tests.integration.test_alembic_009` /
:mod:`tests.integration.test_migration_010_funnel_url`:

* ``upgrade head`` from base-010 creates the function + trigger.
* ``downgrade -1`` reverts cleanly back to revision 010_funnel_url
  (function + trigger gone, ``alembic_version`` rolled back).
* ``upgrade head → downgrade base → upgrade head`` round-trip works.
* Re-running ``upgrade head`` is a no-op (CREATE OR REPLACE FUNCTION
  + DROP TRIGGER IF EXISTS pattern is idempotent).
* INSERT into ``events`` fires exactly one ``NOTIFY whilly_events``
  carrying valid JSON with the contract-required keys.
* UPDATE / DELETE / ROLLBACK do NOT fire NOTIFYs (AFTER INSERT-only,
  transactional).
* Multi-row INSERT fires one NOTIFY per row (FOR EACH ROW).
* Channel name is exactly ``whilly_events``.
* Downgrade does not require ``CASCADE`` and leaves no orphans.
* ``schema.sql`` is in sync with the migration.

Why sync test functions instead of ``async``?
    ``alembic.command.upgrade`` ultimately calls :func:`asyncio.run`
    (see ``whilly/adapters/db/migrations/env.py``) which raises if
    invoked from inside an already-running event loop. Per-test
    asyncio bookkeeping uses :func:`asyncio.run` for the LISTEN /
    NOTIFY scenarios so each test owns its own loop lifetime.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.conftest import (
    DOCKER_REQUIRED,
    HAS_TESTCONTAINERS,
    _build_alembic_config,
    _retry_colima_flake,
    docker_available,
    resolve_docker_host,
)
from whilly.adapters.db import MIGRATIONS_DIR

pytestmark = DOCKER_REQUIRED


_MIGRATION_011_PATH: Path = MIGRATIONS_DIR / "versions" / "011_events_notify_trigger.py"
_NOTIFY_FUNCTION_NAME: str = "whilly_notify_event"
_NOTIFY_TRIGGER_NAME: str = "tr_events_notify"
_NOTIFY_CHANNEL_NAME: str = "whilly_events"


def test_migration_011_file_exists_on_disk() -> None:
    """The 011 migration ships at the canonical path."""
    assert _MIGRATION_011_PATH.is_file(), (
        f"Migration script missing at {_MIGRATION_011_PATH}; alembic upgrade head won't apply 011."
    )


@pytest.fixture
def base_010_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at revision ``010_funnel_url``."""
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    if "DOCKER_HOST" not in os.environ:
        resolved = resolve_docker_host()
        if resolved is not None:
            monkeypatch.setenv("DOCKER_HOST", resolved)
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")

    pg = PostgresContainer("postgres:15-alpine")
    started = False
    try:
        _retry_colima_flake(
            pg.start,
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_011)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        cfg = _build_alembic_config(dsn)
        _retry_colima_flake(
            lambda: command.upgrade(cfg, "010_funnel_url"),
            op="alembic.command.upgrade(010_funnel_url) (test_alembic_011)",
        )
        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
                pass


def _build_cfg(dsn: str) -> Config:
    return _build_alembic_config(dsn)


def _to_asyncpg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def _fetchval(dsn: str, sql: str, *args: Any) -> Any:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _fetchrow(dsn: str, sql: str, *args: Any) -> asyncpg.Record | None:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchrow(sql, *args)
    finally:
        await conn.close()


async def _fetch(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(dsn: str, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Script-directory: 011 is the head revision after this migration ships
# ---------------------------------------------------------------------------


def test_011_is_head_revision() -> None:
    """The alembic script directory reports ``011_events_notify_trigger`` as head."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "011_events_notify_trigger"


def test_011_depends_on_010() -> None:
    """Migration 011's ``down_revision`` is 010 (VAL-M3-MIGRATE-010-001)."""
    cfg = _build_cfg("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("011_events_notify_trigger")
    assert revision is not None
    assert revision.down_revision == "010_funnel_url"


# ---------------------------------------------------------------------------
# Upgrade creates the function + trigger
# (VAL-M3-MIGRATE-010-002 / -003 / -004 / -015)
# ---------------------------------------------------------------------------


def test_upgrade_creates_notify_function(base_010_dsn: str) -> None:
    """``pg_proc`` lists ``whilly_notify_event`` returning trigger after upgrade."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (010→head)")

    row = asyncio.run(
        _fetchrow(
            base_010_dsn,
            """
            SELECT p.proname,
                   pg_get_function_result(p.oid) AS result_type,
                   l.lanname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname = 'public' AND p.proname = $1
            """,
            _NOTIFY_FUNCTION_NAME,
        )
    )
    assert row is not None, f"function {_NOTIFY_FUNCTION_NAME!r} missing after upgrade head"
    assert row["proname"] == _NOTIFY_FUNCTION_NAME
    assert row["result_type"] == "trigger"
    assert row["lanname"] == "plpgsql"


def test_upgrade_creates_after_insert_trigger(base_010_dsn: str) -> None:
    """``information_schema.triggers`` shows the trigger bound AFTER INSERT."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    rows = asyncio.run(
        _fetch(
            base_010_dsn,
            """
            SELECT trigger_name, action_timing, event_manipulation,
                   action_orientation, action_statement
            FROM information_schema.triggers
            WHERE event_object_table = 'events'
              AND trigger_name = $1
            """,
            _NOTIFY_TRIGGER_NAME,
        )
    )
    assert rows, f"trigger {_NOTIFY_TRIGGER_NAME!r} missing after upgrade head"
    by_event = {r["event_manipulation"]: r for r in rows}
    assert "INSERT" in by_event, "tr_events_notify must fire on INSERT"
    insert_row = by_event["INSERT"]
    assert insert_row["action_timing"] == "AFTER"
    assert insert_row["action_orientation"] == "ROW"
    assert _NOTIFY_FUNCTION_NAME in insert_row["action_statement"]
    # AFTER INSERT only — the trigger MUST NOT also fire on UPDATE or DELETE.
    assert "UPDATE" not in by_event
    assert "DELETE" not in by_event


def test_function_source_uses_whilly_events_channel(base_010_dsn: str) -> None:
    """``pg_get_functiondef`` shows the function calls ``pg_notify('whilly_events', …)``."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    src = asyncio.run(
        _fetchval(
            base_010_dsn,
            f"SELECT pg_get_functiondef('{_NOTIFY_FUNCTION_NAME}'::regproc)",
        )
    )
    assert src is not None
    assert f"pg_notify('{_NOTIFY_CHANNEL_NAME}'" in src, (
        f"function source must call pg_notify('{_NOTIFY_CHANNEL_NAME}', …); got: {src}"
    )


# ---------------------------------------------------------------------------
# NOTIFY fan-out behaviour
# (VAL-M3-MIGRATE-010-006 / -007 / -009 / -013 / -014)
# ---------------------------------------------------------------------------


async def _seed_minimal_plan_and_task(dsn: str) -> tuple[str, str]:
    plan_id = "plan-notify-001"
    task_id = "task-notify-001"
    await _execute(
        dsn,
        "INSERT INTO plans (id, name) VALUES ($1, 'notify-test')",
        plan_id,
    )
    await _execute(
        dsn,
        "INSERT INTO tasks (id, plan_id) VALUES ($1, $2)",
        task_id,
        plan_id,
    )
    return plan_id, task_id


def test_insert_into_events_fires_single_notify(base_010_dsn: str) -> None:
    """One INSERT into ``events`` triggers exactly one NOTIFY on ``whilly_events``."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    asyncpg_dsn = _to_asyncpg_dsn(base_010_dsn)
    received: list[tuple[str, str]] = []

    async def _scenario() -> None:
        plan_id, task_id = await _seed_minimal_plan_and_task(base_010_dsn)

        listener = await asyncpg.connect(asyncpg_dsn)
        try:

            def _on_notify(_conn: object, _pid: int, channel: str, payload: str) -> None:
                received.append((channel, payload))

            await listener.add_listener(_NOTIFY_CHANNEL_NAME, _on_notify)

            inserter = await asyncpg.connect(asyncpg_dsn)
            try:
                event_id = await inserter.fetchval(
                    """
                    INSERT INTO events (task_id, plan_id, event_type, payload)
                    VALUES ($1, $2, 'task.claimed', '{"v": 1}'::jsonb)
                    RETURNING id
                    """,
                    task_id,
                    plan_id,
                )
            finally:
                await inserter.close()

            for _ in range(20):
                await asyncio.sleep(0.05)
                if received:
                    break

            assert len(received) == 1, f"expected exactly 1 NOTIFY, got {len(received)}: {received}"
            channel, payload_str = received[0]
            assert channel == _NOTIFY_CHANNEL_NAME
            decoded = json.loads(payload_str)
            assert decoded["event_id"] == event_id
            assert decoded["event_type"] == "task.claimed"
            assert decoded["task_id"] == task_id
            assert decoded["plan_id"] == plan_id
            assert decoded["payload"] == {"v": 1}
        finally:
            await listener.close()

    asyncio.run(_scenario())


def test_multi_row_insert_fires_one_notify_per_row(base_010_dsn: str) -> None:
    """A multi-row INSERT of N rows emits exactly N NOTIFYs (FOR EACH ROW)."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    asyncpg_dsn = _to_asyncpg_dsn(base_010_dsn)
    received: list[str] = []
    n_rows = 25

    async def _scenario() -> None:
        plan_id, task_id = await _seed_minimal_plan_and_task(base_010_dsn)

        listener = await asyncpg.connect(asyncpg_dsn)
        try:

            def _on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
                received.append(payload)

            await listener.add_listener(_NOTIFY_CHANNEL_NAME, _on_notify)

            inserter = await asyncpg.connect(asyncpg_dsn)
            try:
                values = ",".join(
                    f"('{task_id}', '{plan_id}', 'task.heartbeat', '{{\"i\": {i}}}'::jsonb)" for i in range(n_rows)
                )
                await inserter.execute("INSERT INTO events (task_id, plan_id, event_type, payload) VALUES " + values)
            finally:
                await inserter.close()

            for _ in range(40):
                await asyncio.sleep(0.05)
                if len(received) >= n_rows:
                    break

            assert len(received) == n_rows, f"expected {n_rows} NOTIFYs, got {len(received)}"
            ids = [json.loads(p)["event_id"] for p in received]
            assert ids == sorted(ids), "event_id values must be monotonically increasing"
            assert len(set(ids)) == n_rows, "event_id values must be unique per row"
        finally:
            await listener.close()

    asyncio.run(_scenario())


def test_update_does_not_fire_notify(base_010_dsn: str) -> None:
    """UPDATE on ``events`` emits zero NOTIFYs (AFTER INSERT-only)."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    asyncpg_dsn = _to_asyncpg_dsn(base_010_dsn)
    received: list[str] = []

    async def _scenario() -> None:
        plan_id, task_id = await _seed_minimal_plan_and_task(base_010_dsn)
        seed = await asyncpg.connect(asyncpg_dsn)
        try:
            event_id = await seed.fetchval(
                """
                INSERT INTO events (task_id, plan_id, event_type, payload)
                VALUES ($1, $2, 'task.created', '{}'::jsonb)
                RETURNING id
                """,
                task_id,
                plan_id,
            )
        finally:
            await seed.close()

        listener = await asyncpg.connect(asyncpg_dsn)
        try:

            def _on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
                received.append(payload)

            await listener.add_listener(_NOTIFY_CHANNEL_NAME, _on_notify)

            mutator = await asyncpg.connect(asyncpg_dsn)
            try:
                await mutator.execute(
                    "UPDATE events SET payload = '{\"updated\": true}'::jsonb WHERE id = $1",
                    event_id,
                )
                await mutator.execute("DELETE FROM events WHERE id = $1", event_id)
            finally:
                await mutator.close()

            for _ in range(10):
                await asyncio.sleep(0.05)

            assert received == [], (
                f"expected zero NOTIFYs from UPDATE/DELETE (AFTER INSERT trigger only); got {received}"
            )
        finally:
            await listener.close()

    asyncio.run(_scenario())


def test_rollback_does_not_fire_notify(base_010_dsn: str) -> None:
    """INSERT inside a rolled-back transaction emits zero NOTIFYs."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    asyncpg_dsn = _to_asyncpg_dsn(base_010_dsn)
    received: list[str] = []

    async def _scenario() -> None:
        plan_id, task_id = await _seed_minimal_plan_and_task(base_010_dsn)

        listener = await asyncpg.connect(asyncpg_dsn)
        try:

            def _on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
                received.append(payload)

            await listener.add_listener(_NOTIFY_CHANNEL_NAME, _on_notify)

            mutator = await asyncpg.connect(asyncpg_dsn)
            try:
                tx = mutator.transaction()
                await tx.start()
                await mutator.execute(
                    """
                    INSERT INTO events (task_id, plan_id, event_type, payload)
                    VALUES ($1, $2, 'task.rollback', '{}'::jsonb)
                    """,
                    task_id,
                    plan_id,
                )
                await tx.rollback()
            finally:
                await mutator.close()

            for _ in range(10):
                await asyncio.sleep(0.05)

            assert received == [], f"rolled-back INSERT must emit zero NOTIFYs; got {received}"
        finally:
            await listener.close()

    asyncio.run(_scenario())


def test_oversize_payload_is_truncated(base_010_dsn: str) -> None:
    """Inserting a >7900-byte payload triggers a notification with ``truncated:true``.

    Pins VAL-M3-MIGRATE-010-008: the trigger keeps the NOTIFY payload
    under the Postgres 8000-byte cap by dropping ``payload`` and
    setting a ``truncated`` marker when the assembled JSON would
    exceed ``NOTIFY_PAYLOAD_BUDGET_BYTES``.
    """
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    asyncpg_dsn = _to_asyncpg_dsn(base_010_dsn)
    received: list[str] = []

    async def _scenario() -> None:
        plan_id, task_id = await _seed_minimal_plan_and_task(base_010_dsn)

        listener = await asyncpg.connect(asyncpg_dsn)
        try:

            def _on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
                received.append(payload)

            await listener.add_listener(_NOTIFY_CHANNEL_NAME, _on_notify)

            big_payload = json.dumps({"blob": "x" * 8000})
            inserter = await asyncpg.connect(asyncpg_dsn)
            try:
                event_id = await inserter.fetchval(
                    """
                    INSERT INTO events (task_id, plan_id, event_type, payload)
                    VALUES ($1, $2, 'task.huge', $3::jsonb)
                    RETURNING id
                    """,
                    task_id,
                    plan_id,
                    big_payload,
                )
            finally:
                await inserter.close()

            for _ in range(20):
                await asyncio.sleep(0.05)
                if received:
                    break

            assert len(received) == 1
            decoded = json.loads(received[0])
            assert decoded["event_id"] == event_id
            assert decoded["event_type"] == "task.huge"
            assert decoded.get("truncated") is True
            assert "payload" not in decoded
            assert len(received[0].encode("utf-8")) < 8000
        finally:
            await listener.close()

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Idempotency / re-applicability (VAL-M3-MIGRATE-010-011 / -901)
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(base_010_dsn: str) -> None:
    """Two consecutive ``upgrade head`` calls succeed; function + trigger remain present."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (1)")
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head (2)")

    fn_count = asyncio.run(
        _fetchval(
            base_010_dsn,
            """
            SELECT count(*)::int FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = $1
            """,
            _NOTIFY_FUNCTION_NAME,
        )
    )
    trigger_count = asyncio.run(
        _fetchval(
            base_010_dsn,
            """
            SELECT count(*)::int FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = 'events' AND t.tgname = $1 AND NOT t.tgisinternal
            """,
            _NOTIFY_TRIGGER_NAME,
        )
    )
    assert int(fn_count) == 1
    assert int(trigger_count) == 1


def _load_migration_011_module() -> Any:
    """Import the 011 migration as a module despite its leading-digit filename."""
    import importlib.util  # noqa: PLC0415 — local helper

    spec = importlib.util.spec_from_file_location(
        "whilly._test_migration_011",
        _MIGRATION_011_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_raw_upgrade_sql_is_idempotent_on_double_apply(base_010_dsn: str) -> None:
    """Applying the rendered upgrade DDL twice via raw SQL succeeds (VAL-M3-MIGRATE-010-901).

    The plpgsql DDL inside the migration MUST be re-applicable
    against a database that already has the function/trigger
    present. Mechanism: ``CREATE OR REPLACE FUNCTION`` for the
    function (no DROP needed) and ``DROP TRIGGER IF EXISTS`` BEFORE
    the ``CREATE TRIGGER`` for the trigger.
    """
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")

    mig = _load_migration_011_module()

    async def _double_apply() -> None:
        conn = await asyncpg.connect(_to_asyncpg_dsn(base_010_dsn))
        try:
            for _ in range(2):
                await conn.execute(mig.CREATE_NOTIFY_FUNCTION_SQL)
                await conn.execute(mig.DROP_NOTIFY_TRIGGER_SQL)
                await conn.execute(mig.CREATE_NOTIFY_TRIGGER_SQL)
        finally:
            await conn.close()

    asyncio.run(_double_apply())


# ---------------------------------------------------------------------------
# Downgrade leaves no orphans, no CASCADE (VAL-M3-MIGRATE-010-012 / -902)
# ---------------------------------------------------------------------------


def test_downgrade_removes_function_and_trigger(base_010_dsn: str) -> None:
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1")

    async def _inspect() -> tuple[int, int, str | None]:
        fn_count = await _fetchval(
            base_010_dsn,
            """
            SELECT count(*)::int FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = $1
            """,
            _NOTIFY_FUNCTION_NAME,
        )
        trigger_count = await _fetchval(
            base_010_dsn,
            """
            SELECT count(*)::int FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = 'events' AND t.tgname = $1 AND NOT t.tgisinternal
            """,
            _NOTIFY_TRIGGER_NAME,
        )
        version = await _fetchval(base_010_dsn, "SELECT version_num FROM alembic_version")
        return int(fn_count), int(trigger_count), version

    fn_count, trigger_count, version = asyncio.run(_inspect())
    assert fn_count == 0
    assert trigger_count == 0
    assert version == "010_funnel_url"


def test_downgrade_does_not_use_cascade() -> None:
    """The migration's downgrade DROP FUNCTION must NOT use CASCADE.

    Pins VAL-M3-MIGRATE-010-902: the downgrade MUST tolerate the
    absence of either object (DROP IF EXISTS) but MUST NOT silently
    drop unrelated dependents through CASCADE. Verified by reading
    the migration source — the DROP FUNCTION line cannot include
    ``CASCADE``.
    """
    text = _MIGRATION_011_PATH.read_text(encoding="utf-8")
    drop_function_lines = [line for line in text.splitlines() if "DROP FUNCTION" in line.upper()]
    assert drop_function_lines, "migration must DROP FUNCTION on downgrade"
    for line in drop_function_lines:
        assert "CASCADE" not in line.upper(), (
            f"DROP FUNCTION must NOT use CASCADE in 011_events_notify_trigger.py; got: {line!r}"
        )


def test_round_trip_upgrade_downgrade_upgrade(base_010_dsn: str) -> None:
    """``upgrade 011`` → ``downgrade -1`` → ``upgrade 011`` succeeds at every step."""
    cfg = _build_cfg(base_010_dsn)
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "011_events_notify_trigger"),
        op="upgrade 011_events_notify_trigger (rt-1)",
    )
    _retry_colima_flake(lambda: command.downgrade(cfg, "-1"), op="downgrade -1 (rt)")
    _retry_colima_flake(
        lambda: command.upgrade(cfg, "011_events_notify_trigger"),
        op="upgrade 011_events_notify_trigger (rt-2)",
    )

    async def _inspect() -> tuple[int, int]:
        fn_count = await _fetchval(
            base_010_dsn,
            """
            SELECT count(*)::int FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = $1
            """,
            _NOTIFY_FUNCTION_NAME,
        )
        trigger_count = await _fetchval(
            base_010_dsn,
            """
            SELECT count(*)::int FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = 'events' AND t.tgname = $1 AND NOT t.tgisinternal
            """,
            _NOTIFY_TRIGGER_NAME,
        )
        return int(fn_count), int(trigger_count)

    fn_count, trigger_count = asyncio.run(_inspect())
    assert fn_count == 1
    assert trigger_count == 1


# ---------------------------------------------------------------------------
# schema.sql parity check (VAL-M3-MIGRATE-010-005)
# ---------------------------------------------------------------------------


def test_schema_sql_mentions_notify_function_and_trigger() -> None:
    """The hand-maintained ``schema.sql`` reference declares the new function + trigger.

    AGENTS.md → "Migration discipline" requires every alembic
    migration in M2/M3 to hand-update ``schema.sql`` in the SAME
    commit as the migration. This test pins that invariant for
    migration 011 — if any of the function name, trigger binding,
    or channel literal is missing from ``schema.sql`` the test
    fails loudly, before drift propagates.
    """
    schema_sql_path = Path(__file__).resolve().parents[2] / "whilly" / "adapters" / "db" / "schema.sql"
    text = schema_sql_path.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION whilly_notify_event" in text, (
        "schema.sql must declare the whilly_notify_event() function after migration 011 ships"
    )
    assert "RETURNS trigger" in text
    assert "LANGUAGE plpgsql" in text
    assert "pg_notify('whilly_events'" in text, "schema.sql must call pg_notify on the 'whilly_events' channel"
    assert "CREATE TRIGGER tr_events_notify" in text
    assert "AFTER INSERT ON events" in text
    assert "FOR EACH ROW" in text
    assert "EXECUTE FUNCTION whilly_notify_event" in text
    assert "DROP TRIGGER IF EXISTS tr_events_notify" in text, (
        "schema.sql must include the DROP TRIGGER IF EXISTS guard before CREATE TRIGGER"
    )
