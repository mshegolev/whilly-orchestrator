"""Phase 1 integration smoke (TASK-008, PRD SC-6).

Verifies the three Day-1 deliverables are wired end-to-end:

1. ``whilly.core`` is importable and free of forbidden I/O / transport modules
   (Hexagonal architecture, PRD TC-8 / SC-6). This duplicates the
   ``import-linter`` contract programmatically so a CI run that skips
   ``lint-imports`` still catches the regression.
2. Domain dataclasses (Task, Plan, Event, WorkerHandle) round-trip cleanly —
   they're frozen, hashable, and survive ``dataclasses.asdict`` serialisation
   without raising on the immutable tuple defaults.
3. Alembic migrations apply against a fresh Postgres 15 launched via
   testcontainers, producing the four tables (plans, tasks, events, workers)
   plus the indexes the access-pattern AC names. Round-trip
   ``upgrade head`` → ``downgrade base`` → ``upgrade head`` is clean.

These tests intentionally do **not** exercise ``TaskRepository`` (TASK-009b/c/d)
or any HTTP transport — that's Phase 2/5 work. The point is "everything from
Day 1 still composes together".
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from whilly.adapters.db import MIGRATIONS_DIR, SCHEMA_SQL_PATH
from whilly.core import (
    Event,
    Plan,
    Priority,
    Task,
    TaskStatus,
    WorkerHandle,
)

# ─── pytest skip plumbing ─────────────────────────────────────────────────
# The migration round-trip test boots a Postgres container via testcontainers,
# which needs a working Docker daemon. Skip cleanly on machines where Docker
# is not available rather than failing the whole integration suite.

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TESTCONTAINERS = True
except ImportError:  # pragma: no cover — testcontainers is in [dev]; guard anyway
    _HAS_TESTCONTAINERS = False


def _docker_available() -> bool:
    """Return True iff a Docker daemon is reachable.

    Skips the migration test on hosts without Docker (CI runners that don't
    expose the socket, fresh laptops without Desktop running, etc.). Stdlib
    ``shutil.which`` checks the binary; ``docker info`` is the authoritative
    daemon-reachable check.
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _resolve_docker_host() -> str | None:
    """Resolve the active Docker context's socket path for the Python SDK.

    The Python ``docker`` library defaults to ``/var/run/docker.sock`` and
    ignores Docker CLI contexts (colima, Rancher Desktop, etc.). On macOS
    multi-context setups, ``/var/run/docker.sock`` is often a stale symlink
    to whichever flavour of Docker installed itself last, while the CLI
    routes to a different socket via ``docker context use``.

    Returning the active context's endpoint lets the fixture set
    ``DOCKER_HOST`` so testcontainers (which uses the Python SDK) finds the
    same daemon the CLI does. Returns ``None`` if context detection fails —
    caller falls back to whatever ``docker.from_env`` picks.
    """
    if shutil.which("docker") is None:
        return None
    try:
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    host = result.stdout.strip()
    return host or None


_DOCKER_REQUIRED = pytest.mark.skipif(
    not (_HAS_TESTCONTAINERS and _docker_available()),
    reason="Docker daemon not reachable; testcontainers cannot boot Postgres",
)


# ─── 1. Core import purity (programmatic SC-6) ────────────────────────────


_FORBIDDEN_IN_CORE = ("asyncpg", "httpx", "subprocess", "fastapi", "uvicorn", "alembic")


def test_whilly_core_is_importable_without_io_dependencies() -> None:
    """``whilly.core`` and its submodules import without pulling in I/O deps.

    Mirrors ``.importlinter``'s ``core-purity`` contract: walks every loaded
    submodule and asserts none of them ended up with a forbidden top-level
    module attribute. Catches the case where someone adds
    ``import asyncpg`` to ``whilly.core.foo`` and the import-linter step
    happens to be skipped.
    """
    # Force-import every submodule so the asserts below see them in sys.modules.
    import whilly.core  # noqa: F401 — re-import for explicitness
    import whilly.core.models  # noqa: F401
    import whilly.core.prompts  # noqa: F401
    import whilly.core.scheduler  # noqa: F401
    import whilly.core.state_machine  # noqa: F401

    core_modules = {name for name in sys.modules if name == "whilly.core" or name.startswith("whilly.core.")}
    assert core_modules, "expected at least whilly.core itself in sys.modules"

    for mod_name in sorted(core_modules):
        module = sys.modules[mod_name]
        for forbidden in _FORBIDDEN_IN_CORE:
            assert not hasattr(module, forbidden), (
                f"{mod_name} appears to import forbidden top-level module {forbidden!r}; "
                "whilly.core must remain pure (PRD SC-6 / TC-8)."
            )


def test_whilly_core_subprocess_and_chdir_grep_clean() -> None:
    """Belt-and-braces grep: no ``os.chdir`` / ``subprocess`` call sites in core.

    The ``.importlinter`` ``ignore_imports`` block notes that stdlib ``os`` is
    permitted in ``whilly.core`` (for ``os.path`` utilities), but the v3-style
    ``os.chdir`` / ``subprocess.run`` patterns are explicitly forbidden by the
    PRD (SC-6 / Module structure). Static-source grep catches them even if the
    import-graph check happens to miss the indirection.
    """
    core_dir = Path(__file__).resolve().parents[2] / "whilly" / "core"
    assert core_dir.is_dir(), f"expected whilly/core/ at {core_dir}"

    offenders: list[str] = []
    for py_file in sorted(core_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        # Strip simple ``# ...`` comments before scanning so docstring
        # discussions of forbidden symbols don't trip the test.
        live = "\n".join(line.partition("#")[0] for line in text.splitlines())
        for needle in ("os.chdir(", "os.getcwd(", "subprocess.", "import subprocess"):
            if needle in live:
                offenders.append(f"{py_file.relative_to(core_dir.parent.parent)}: {needle}")
    assert not offenders, "Forbidden call sites in whilly.core:\n  " + "\n  ".join(offenders)


# ─── 2. Domain-model serialisation round-trip ─────────────────────────────


def test_task_dataclass_is_frozen_and_serialisable() -> None:
    """Frozen dataclass invariant + ``asdict`` round-trip on Task.

    Confirms that the value-object semantics from TASK-004 survive the
    integration boundary: tuples stay tuples (not lists), enums stay enums,
    and the dataclass is hashable so the scheduler (TASK-013c) can drop it
    into ``set[Task]``.
    """
    task = Task(
        id="TASK-008",
        status=TaskStatus.PENDING,
        dependencies=("TASK-006", "TASK-007"),
        key_files=("tests/integration/test_phase1_smoke.py",),
        priority=Priority.HIGH,
        description="phase 1 smoke",
        acceptance_criteria=("import-linter passes", "migrations apply"),
        test_steps=("lint-imports", "pytest tests/integration/test_phase1_smoke.py -v"),
        prd_requirement="SC-6",
    )

    # frozen=True — attribute assignment must raise.
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.status = TaskStatus.DONE  # type: ignore[misc]

    # hashable — required by the scheduler in_progress: set[TaskId] paths.
    assert hash(task) == hash(task)

    # asdict round-trip: tuples stay tuples on the way in/out.
    payload = dataclasses.asdict(task)
    assert payload["id"] == "TASK-008"
    assert payload["status"] == TaskStatus.PENDING
    assert payload["dependencies"] == ("TASK-006", "TASK-007")
    assert payload["priority"] == Priority.HIGH
    assert payload["version"] == 0


def test_plan_event_worker_handle_serialise_cleanly() -> None:
    """Plan, Event, WorkerHandle all round-trip through ``dataclasses.asdict``.

    Smoke-tests the rest of the domain surface — important because the
    Postgres adapter (TASK-009) and the FastAPI transport (TASK-021) will
    serialise these via the same protocol.
    """
    task = Task(id="t1", status=TaskStatus.PENDING)
    plan = Plan(id="plan-001", name="phase 1 smoke plan", tasks=(task,))
    plan_dict = dataclasses.asdict(plan)
    assert plan_dict["id"] == "plan-001"
    assert plan_dict["tasks"][0]["id"] == "t1"

    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
    event = Event(
        id=42,
        task_id="t1",
        event_type="CLAIM",
        payload={"worker_id": "w-1", "version": 1},
        created_at=now,
    )
    event_dict = dataclasses.asdict(event)
    assert event_dict["task_id"] == "t1"
    assert event_dict["payload"] == {"worker_id": "w-1", "version": 1}
    assert event_dict["created_at"] == now

    worker = WorkerHandle(
        worker_id="w-1",
        hostname="ci-runner-1",
        last_heartbeat=now,
        token_hash="sha256:deadbeef",
    )
    worker_dict = dataclasses.asdict(worker)
    assert worker_dict["worker_id"] == "w-1"
    assert worker_dict["token_hash"] == "sha256:deadbeef"


# ─── 3. Alembic migrations apply against testcontainers Postgres ──────────


def _project_root() -> Path:
    """Return the workspace root (where alembic.ini lives)."""
    return Path(__file__).resolve().parents[2]


def _alembic_config(dsn: str) -> Config:
    """Build an Alembic :class:`Config` pointing at the project's migrations.

    We override ``script_location`` to an absolute path so the test does not
    depend on the current working directory, and pass the DSN via
    ``WHILLY_DATABASE_URL`` (which env.py honours per the v4.0 contract).
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("version_path_separator", "os")
    # env.py reads WHILLY_DATABASE_URL first; setting sqlalchemy.url is a
    # belt-and-braces fallback for the offline path.
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    """Boot a fresh ``postgres:15-alpine`` and yield its DSN.

    Module-scoped so the migration test pays the container-boot cost once and
    the migration round-trip can re-use the same instance. The container is
    torn down even if the test raises (testcontainers' ``__exit__``).
    """
    if not (_HAS_TESTCONTAINERS and _docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")

    # Bridge CLI context → Python SDK so multi-context macOS setups (colima,
    # Rancher Desktop, plain Docker Desktop) all work without DOCKER_HOST
    # being pre-exported in the developer's shell. Stays a no-op if the env
    # var is already set or context detection fails.
    prior_docker_host = os.environ.get("DOCKER_HOST")
    if prior_docker_host is None:
        resolved = _resolve_docker_host()
        if resolved is not None:
            os.environ["DOCKER_HOST"] = resolved

    # testcontainers normally launches a ``ryuk`` reaper container that
    # cleans up orphaned containers on test crash. The reaper bind-mounts
    # the host's docker socket, which colima rejects ("operation not
    # supported" on the colima-managed path) and which is fragile on
    # rootless / Lima setups generally. We don't need ryuk in this suite —
    # the ``with PostgresContainer(...)`` context manager tears down its own
    # container deterministically. Opt out so the test runs on colima.
    prior_ryuk = os.environ.get("TESTCONTAINERS_RYUK_DISABLED")
    if prior_ryuk is None:
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"

    # Match the docker-compose image (TASK-003) so behaviour parity with
    # local dev is guaranteed.
    try:
        with PostgresContainer("postgres:15-alpine") as pg:
            # testcontainers' default DSN uses the psycopg2 driver suffix. We
            # rip it back to plain ``postgresql://`` so env.py does the
            # asyncpg coercion itself — exercises the same code path
            # operators hit.
            raw = pg.get_connection_url()
            dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
            yield dsn
    finally:
        # Restore DOCKER_HOST to whatever the developer had (or unset it).
        if prior_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = prior_docker_host
        if prior_ryuk is None:
            os.environ.pop("TESTCONTAINERS_RYUK_DISABLED", None)
        else:
            os.environ["TESTCONTAINERS_RYUK_DISABLED"] = prior_ryuk


@_DOCKER_REQUIRED
def test_alembic_upgrade_head_creates_all_tables(postgres_dsn: str) -> None:
    """``alembic upgrade head`` creates plans, tasks, events, workers + indexes.

    Mirrors TASK-007's manual verification (`psql -c '\\dt'`) but inside
    pytest so CI catches a broken migration before it ships.
    """
    # asyncpg-driven asserts are wrapped in asyncio.run so the test stays
    # synchronous from pytest's POV (no pytest-asyncio coupling here).
    import asyncio

    import asyncpg

    cfg = _alembic_config(postgres_dsn)

    # env.py reads WHILLY_DATABASE_URL first; set it explicitly for this run.
    prior = os.environ.get("WHILLY_DATABASE_URL")
    os.environ["WHILLY_DATABASE_URL"] = postgres_dsn
    try:
        command.upgrade(cfg, "head")

        async def _inspect() -> dict[str, list[str]]:
            # asyncpg refuses the SQLAlchemy ``+asyncpg`` driver hint; strip
            # if env.py's coercion pushed it back into the env var.
            asyncpg_dsn = os.environ["WHILLY_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(asyncpg_dsn)
            try:
                tables = await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                )
                indexes = await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY indexname"
                )
                version_default = await conn.fetchval(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'tasks' AND column_name = 'version'"
                )
                return {
                    "tables": [r["tablename"] for r in tables],
                    "indexes": [r["indexname"] for r in indexes],
                    "version_default": [str(version_default)],
                }
            finally:
                await conn.close()

        result = asyncio.run(_inspect())

        # All four domain tables present + the alembic_version bookkeeping table.
        assert {"alembic_version", "events", "plans", "tasks", "workers"}.issubset(set(result["tables"])), (
            f"Expected domain tables not all present. Got: {result['tables']}"
        )

        # Indexes named in TASK-007's AC.
        expected_indexes = {
            "ix_tasks_plan_id_status",
            "ix_events_task_id_created_at",
            "ix_workers_last_heartbeat",
            "ix_tasks_claimed_at_active",
        }
        assert expected_indexes.issubset(set(result["indexes"])), (
            f"Missing indexes: {expected_indexes - set(result['indexes'])}; got {result['indexes']}"
        )

        # tasks.version DEFAULT 0 — the optimistic-locking column (FR-2.4).
        assert result["version_default"] == ["0"], (
            f"tasks.version must DEFAULT 0 for optimistic locking; got {result['version_default']}"
        )
    finally:
        if prior is None:
            os.environ.pop("WHILLY_DATABASE_URL", None)
        else:
            os.environ["WHILLY_DATABASE_URL"] = prior


@_DOCKER_REQUIRED
def test_alembic_round_trip_downgrade_then_upgrade(postgres_dsn: str) -> None:
    """``upgrade head`` → ``downgrade base`` → ``upgrade head`` is a clean cycle.

    Catches the class of bugs where a migration's ``downgrade()`` forgets to
    drop an object — the second upgrade then fails on a duplicate-name.
    """
    cfg = _alembic_config(postgres_dsn)
    prior = os.environ.get("WHILLY_DATABASE_URL")
    os.environ["WHILLY_DATABASE_URL"] = postgres_dsn
    try:
        # The previous test left us at head; downgrade then re-upgrade.
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    finally:
        if prior is None:
            os.environ.pop("WHILLY_DATABASE_URL", None)
        else:
            os.environ["WHILLY_DATABASE_URL"] = prior


def test_schema_sql_reference_file_present_and_non_empty() -> None:
    """``whilly/adapters/db/schema.sql`` is the human-readable reference DDL.

    TASK-007 ships this alongside the Alembic migration as documentation. The
    test only asserts the file exists, is non-empty, and mentions every domain
    table — it does NOT diff against the migration (manual review owns that
    contract; an automatic diff would just re-implement the migration).
    """
    text = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    assert text.strip(), f"{SCHEMA_SQL_PATH} is empty — TASK-007 reference DDL missing"
    for table in ("plans", "tasks", "events", "workers"):
        assert table in text, f"schema.sql missing reference to table {table!r}"
