"""Shared pytest fixtures for Whilly v4.0 integration tests (TASK-011 / SC-1).

Centralises the bits every integration test needs: Docker availability
detection, ``DOCKER_HOST`` resolution for macOS multi-context setups
(colima / Rancher Desktop / Docker Desktop), the testcontainers
Postgres bootstrap with Alembic migrations applied, and per-test
asyncpg pool + :class:`TaskRepository` fixtures with table truncation.

Why session-scope the container, function-scope the pool?
    Booting ``postgres:15-alpine`` and applying ``alembic upgrade head``
    costs several seconds even on a warm laptop. Per-test reboot would
    dominate the suite runtime once we have more than a couple of
    integration tests. The pool, on the other hand, is cheap to open
    and close, and using a fresh one per test side-steps the
    "leftover prepared statements / aborted transactions" surface
    that breaks reuse across tests using ``asyncpg``'s caching.

Why TRUNCATE at fixture *setup* and not teardown?
    Setup-side TRUNCATE means each test inherits whatever state the
    previous test left behind only for the brief window between
    teardown of the previous fixture and setup of the next — and the
    next test wipes that state before yielding. Doing it at teardown
    instead would mean a failing test corrupts the database for
    introspection (developers would have to re-run to inspect the
    final state), and it also wastes work for the very last test in
    the session. Setup-side is the canonical pytest pattern.

Skip plumbing
-------------
``DOCKER_REQUIRED`` is the public skipif marker; tests can apply it
either at module level (``pytestmark = DOCKER_REQUIRED``) or per-test.
The :func:`postgres_dsn` fixture also calls :func:`pytest.skip` on
demand so direct callers without the marker still get a clean skip
rather than an obscure testcontainers error.

Coexistence with TASK-008's smoke test
--------------------------------------
``tests/integration/test_phase1_smoke.py`` (TASK-008) defines its own
module-scoped ``postgres_dsn`` fixture — pytest fixture resolution
picks the closer scope, so that file keeps using its own container.
This conftest module is the source of truth for any *new*
integration test (TASK-011 onwards). Refactoring phase1_smoke to use
the shared fixture is intentionally deferred to keep TASK-011 within
its declared footprint.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from whilly.adapters.db import MIGRATIONS_DIR, TaskRepository, close_pool, create_pool

# ─── Fixture-file loader (M1 readiness baseline) ─────────────────────────
#
# ``tests/fixtures/`` ships frozen fixtures for the v5 mission's
# backwards-compat suite (v3-era + v4.0-era ``tasks.json`` snapshots, the
# v4.3.1 ``events.payload`` JSON-Schema baseline, and a representative
# ``.whilly_state.json`` round-trip snapshot). ``load_fixture`` is the
# single read-side entry point — both as an importable function and as a
# pytest fixture — so tests don't need to assemble paths from
# ``__file__`` themselves.
#
# Behaviour:
#   * ``.json`` files are parsed with :func:`json.loads`.
#   * Any other extension (including ``.md``) is returned as a UTF-8
#     :class:`str`.
#   * Names may include sub-paths (e.g. ``"baselines/events_payload_v4.3.1.json"``).
#
# Re-creation of these fixtures is owned by ``scripts/m1_baseline_fixtures.py``
# (idempotent re-run produces no diff).

FIXTURES_DIR: Path = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a fixture file from ``tests/fixtures/`` by relative name.

    JSON files are parsed; everything else is returned as text.
    """
    path = FIXTURES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return text


@pytest.fixture
def load_fixture_fn() -> Callable[[str], Any]:
    """Pytest fixture wrapper around :func:`load_fixture`.

    Tests that prefer fixture-injection over module imports can request
    ``load_fixture_fn`` and call it with a relative fixture name.
    """
    return load_fixture


# ─── Colima/testcontainers port-forwarding flake mitigation ───────────────
#
# Local Docker (colima/Rancher Desktop on macOS) intermittently wedges
# port-forwarding once a single pytest session has churned through ~5–10
# ephemeral testcontainer Postgres instances. Symptom: the next container's
# port probe — or the first ``asyncpg.create_pool`` against a freshly-booted
# container — fails with ``ConnectionRefusedError`` /
# ``OSError: [Errno 61] Connect call failed ('127.0.0.1', 32xxx)`` even though
# Postgres inside the container is healthy. Root cause is in the colima/lima
# vsock proxy, not in the test code.
#
# A tight 3-attempt retry with exponential backoff (0.5 s, 1.0 s, 2.0 s) is
# enough to ride out the transient wedge in nearly every observed case. On
# *full* failure we re-raise the underlying exception with a pytest-friendly
# wrapper that calls out the canonical remediation: ``colima restart``.
_TC_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0)
_TC_REMEDIATION_HINT: str = (
    "Hint: this is the documented colima/testcontainers port-forwarding flake "
    "(see AGENTS.md → 'Known pre-existing issues'). Run `colima restart` and "
    "retry. If running headless CI, increase Docker memory or pin the "
    "container to a long-lived session-scoped fixture."
)
_TC_LOG = logging.getLogger("whilly.tests.colima_retry")

_T = TypeVar("_T")


def _retry_colima_flake(
    fn: Callable[[], _T],
    *,
    op: str,
    backoffs: tuple[float, ...] = _TC_RETRY_BACKOFFS,
) -> _T:
    """Run ``fn`` with 3-attempt exponential backoff against colima flake.

    Sleeps from :data:`_TC_RETRY_BACKOFFS` (default 0.5 s, 1.0 s, 2.0 s)
    *between* attempts: 4 attempts total (1 initial + len(backoffs) retries).
    Re-raises the *last* exception wrapped in :class:`RuntimeError` whose
    message names the operation and the ``colima restart`` remediation, so
    the failure message in the pytest report points the operator straight
    at the fix instead of an opaque ``ConnectionRefusedError``.

    Designed for narrow use around two known-flaky surfaces:

    1. ``PostgresContainer`` start (`__enter__` / `start()`).
    2. ``asyncpg.create_pool`` (whilly's wrapper does a ``SELECT 1`` health
       check inside ``acquire()`` — that is where the flake actually
       surfaces, *not* during pool object construction).
    """
    last_exc: BaseException | None = None
    attempts = 1 + len(backoffs)
    for attempt_idx in range(attempts):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — we re-raise below
            last_exc = exc
            if attempt_idx == attempts - 1:
                break
            sleep_s = backoffs[attempt_idx]
            _TC_LOG.warning(
                "%s failed on attempt %d/%d (%s: %s); retrying after %.1fs",
                op,
                attempt_idx + 1,
                attempts,
                type(exc).__name__,
                exc,
                sleep_s,
            )
            time.sleep(sleep_s)
    assert last_exc is not None  # narrow the type for mypy
    raise RuntimeError(
        f"{op} failed after {attempts} attempts (backoffs={list(backoffs)}). "
        f"Last error: {type(last_exc).__name__}: {last_exc}. {_TC_REMEDIATION_HINT}"
    ) from last_exc


async def _retry_create_pool_async(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    op: str = "asyncpg.create_pool",
    backoffs: tuple[float, ...] = _TC_RETRY_BACKOFFS,
) -> asyncpg.Pool:
    """Async sibling of :func:`_retry_colima_flake` for ``create_pool``.

    Mirrors the same 3-attempt exponential-backoff policy. We can't use the
    sync helper here because ``await create_pool(...)`` must yield to the
    event loop, and ``time.sleep`` would block it. Uses ``asyncio.sleep``
    instead.
    """
    import asyncio

    last_exc: BaseException | None = None
    attempts = 1 + len(backoffs)
    for attempt_idx in range(attempts):
        try:
            return await create_pool(dsn, min_size=min_size, max_size=max_size)
        except BaseException as exc:  # noqa: BLE001 — we re-raise below
            last_exc = exc
            if attempt_idx == attempts - 1:
                break
            sleep_s = backoffs[attempt_idx]
            _TC_LOG.warning(
                "%s failed on attempt %d/%d (%s: %s); retrying after %.1fs",
                op,
                attempt_idx + 1,
                attempts,
                type(exc).__name__,
                exc,
                sleep_s,
            )
            await asyncio.sleep(sleep_s)
    assert last_exc is not None  # narrow the type for mypy
    raise RuntimeError(
        f"{op} failed after {attempts} attempts (backoffs={list(backoffs)}). "
        f"Last error: {type(last_exc).__name__}: {last_exc}. {_TC_REMEDIATION_HINT}"
    ) from last_exc


try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    HAS_TESTCONTAINERS: bool = True
except ImportError:  # pragma: no cover — testcontainers is in [dev]; defensive
    HAS_TESTCONTAINERS = False


def docker_available() -> bool:
    """Return True iff a Docker daemon is reachable.

    ``shutil.which`` checks the binary; ``docker info`` is the
    authoritative daemon-reachable check (cheap; ~30ms on a warm CLI).
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


def resolve_docker_host() -> str | None:
    """Resolve the active Docker context's socket for the Python SDK.

    The Python ``docker`` library defaults to ``/var/run/docker.sock``
    and ignores Docker CLI contexts. On macOS multi-context setups
    (colima / Rancher / Docker Desktop), ``/var/run/docker.sock`` is
    often a stale symlink to whichever flavour installed itself last,
    while the CLI routes via ``docker context use``. Returning the
    active context's endpoint lets us set ``DOCKER_HOST`` so
    testcontainers (which wraps the Python SDK) finds the same
    daemon the CLI does.

    Returns ``None`` if context detection fails — caller falls back
    to whatever ``docker.from_env`` picks.
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


# Public skipif marker — tests apply it as ``pytestmark = DOCKER_REQUIRED``
# at module level, or per-test, or rely on the fixture's internal
# pytest.skip() call. The session-scoped fixture also skips on demand,
# so module-level decoration is optional but makes the intent explicit.
DOCKER_REQUIRED = pytest.mark.skipif(
    not (HAS_TESTCONTAINERS and docker_available()),
    reason="Docker daemon not reachable; testcontainers cannot boot Postgres",
)


def _build_alembic_config(dsn: str) -> Config:
    """Build an Alembic :class:`Config` pointing at the project's migrations.

    Mirrors the pattern in TASK-008's smoke test: absolute
    ``script_location`` so the test is cwd-independent, DSN both via
    ``WHILLY_DATABASE_URL`` (which env.py reads first) and
    ``sqlalchemy.url`` as a belt-and-braces fallback.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("version_path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Boot ``postgres:15-alpine`` once per session and yield a migrated DSN.

    Steps:
      1. Detect Docker availability; ``pytest.skip`` if absent.
      2. Bridge ``DOCKER_HOST`` from CLI context to Python SDK if not
         already set (macOS multi-context fix).
      3. Disable testcontainers' ``ryuk`` reaper — it bind-mounts the
         host docker socket which colima rejects, and the
         ``with PostgresContainer(...)`` context manager already tears
         down deterministically.
      4. Boot the container, strip the ``+psycopg2`` driver suffix from
         the DSN so asyncpg + Alembic env.py both accept it.
      5. ``alembic upgrade head`` applies the schema once. Subsequent
         tests inherit the same migrated DB.
      6. Yield the DSN, restore env vars on teardown.

    The container survives the entire pytest session; per-test
    isolation is provided by :func:`db_pool` truncating tables.
    """
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")

    prior_docker_host = os.environ.get("DOCKER_HOST")
    if prior_docker_host is None:
        resolved = resolve_docker_host()
        if resolved is not None:
            os.environ["DOCKER_HOST"] = resolved

    prior_ryuk = os.environ.get("TESTCONTAINERS_RYUK_DISABLED")
    if prior_ryuk is None:
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"

    prior_db_url = os.environ.get("WHILLY_DATABASE_URL")

    # Wrap the testcontainers Postgres start in a 3-attempt exponential-backoff
    # retry loop (0.5 s, 1.0 s, 2.0 s) to ride out the colima/Rancher-Desktop
    # port-forwarding flake documented in AGENTS.md. The container is started
    # imperatively (not via ``with``) so we can retry; cleanup is in the outer
    # ``finally`` block.
    pg = PostgresContainer("postgres:15-alpine")
    started = False
    try:
        _retry_colima_flake(pg.start, op="PostgresContainer('postgres:15-alpine').start()")
        started = True
        raw = pg.get_connection_url()
        # testcontainers ships ``postgresql+psycopg2://`` by default; rip
        # back to plain ``postgresql://`` so env.py's own asyncpg coercion
        # path is exercised (and asyncpg itself doesn't choke on the
        # SQLAlchemy driver suffix).
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")

        os.environ["WHILLY_DATABASE_URL"] = dsn
        # Alembic's first SQL contact also rides through colima's port-forward
        # — same flake surface as the container start above. Retry with the
        # same 3-attempt exponential backoff.
        _retry_colima_flake(
            lambda: command.upgrade(_build_alembic_config(dsn), "head"),
            op="alembic.command.upgrade(head)",
        )

        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
                _TC_LOG.warning("PostgresContainer.stop() raised during teardown", exc_info=True)
        if prior_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = prior_docker_host
        if prior_ryuk is None:
            os.environ.pop("TESTCONTAINERS_RYUK_DISABLED", None)
        else:
            os.environ["TESTCONTAINERS_RYUK_DISABLED"] = prior_ryuk
        if prior_db_url is None:
            os.environ.pop("WHILLY_DATABASE_URL", None)
        else:
            os.environ["WHILLY_DATABASE_URL"] = prior_db_url


@pytest.fixture
async def db_pool(postgres_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    """Per-test asyncpg pool against the migrated session-scoped DB.

    ``max_size`` is bumped to 20 (vs the runtime default of 10) so
    SC-1's 100-concurrent-claims test gets meaningful in-flight
    parallelism on the wire — with 10 connections the test still
    passes (correctness is independent of pool size), but the SQL
    contention pattern is artificially serialised by the pool's own
    queue rather than by Postgres' SKIP LOCKED, defeating the point
    of the test.

    Truncation happens at *setup* (CASCADE so the events FK doesn't
    block, RESTART IDENTITY so the BIGSERIAL events.id sequence
    starts fresh) — see module docstring for the rationale.

    Pool creation is wrapped in a 3-attempt exponential-backoff retry
    (0.5 s, 1.0 s, 2.0 s) to ride out the colima/Rancher-Desktop
    port-forwarding flake documented in AGENTS.md — the symptom
    surfaces here as ``OSError: [Errno 61] Connect call failed`` from
    ``pool.acquire()``'s ``SELECT 1`` health check inside whilly's
    :func:`whilly.adapters.db.create_pool` wrapper. On full failure
    the helper raises a :class:`RuntimeError` mentioning
    ``colima restart`` as remediation.
    """
    pool = await _retry_create_pool_async(postgres_dsn, min_size=2, max_size=20)
    try:
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE events, tasks, plans, workers RESTART IDENTITY CASCADE")
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
async def task_repo(db_pool: asyncpg.Pool) -> TaskRepository:
    """Per-test :class:`TaskRepository` wrapping the per-test pool.

    Splitting pool from repo keeps the seeding helpers (which need
    raw SQL via the pool) decoupled from the system-under-test (the
    repository methods). Tests that only care about the repo can
    request just ``task_repo``; tests that also need to seed plans /
    tasks / workers request both.
    """
    return TaskRepository(db_pool)
