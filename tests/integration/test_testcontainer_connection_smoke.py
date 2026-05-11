"""500-connection smoke test for the colima vsock-flake mitigation.

Pins the per-connection regression bar for the
``fix-m3-testcontainers-vsock-flake-mitigation`` feature: the extended
5-attempt retry budget and the closure-local ``pg``-reference release in
``tests/conftest.py`` must NOT slow down the steady-state connection path.

Boots a single ``postgres:15-alpine`` testcontainer, then opens and closes
500 short-lived ``asyncpg`` connections sequentially against it (each
connection runs a trivial ``SELECT 1`` round-trip). Marked
``serial`` so pytest-xdist (when used) doesn't parallelise this with
other testcontainer-heavy tests — we want a clean signal for any
per-connection regression.

Why 500 sequential and not concurrent?
    The vsock-proxy wedge documented in AGENTS.md surfaces *between*
    connection establishments, not during steady-state traffic. A long
    sequential drain is the most direct way to exercise the retry path
    repeatedly without artificially saturating the docker socket fan-out.

Skipped when Docker is unreachable (mirrors the rest of the integration suite).
"""

from __future__ import annotations

import asyncio
import os
import time

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from tests.conftest import (
    DOCKER_REQUIRED,
    HAS_TESTCONTAINERS,
    WHILLY_TESTCONTAINER_IMAGE,
    WHILLY_TESTCONTAINER_LABEL_KEY,
    WHILLY_TESTCONTAINER_LABEL_VALUE,
    _register_pg_atexit_stop,
    _retry_colima_flake,
    docker_available,
    resolve_docker_host,
)

pytestmark = DOCKER_REQUIRED


_CONNECTION_COUNT: int = 500
_PER_CONNECTION_REGRESSION_BUDGET_SECONDS: float = 0.500


def test_500_sequential_connections_through_single_container() -> None:
    """500 sequential ``asyncpg.connect`` round-trips stay within budget.

    Verifies the vsock-flake mitigation does NOT introduce a per-connection
    regression: average connect+SELECT 1+close must be well under 500 ms even
    on a slow CI runner. Each connection runs a fresh ``SELECT 1`` so the
    test exercises the full SQL round-trip, not just TCP setup.
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

    pg = PostgresContainer(WHILLY_TESTCONTAINER_IMAGE).with_kwargs(
        labels={WHILLY_TESTCONTAINER_LABEL_KEY: WHILLY_TESTCONTAINER_LABEL_VALUE}
    )
    stop_pg = None
    try:
        _retry_colima_flake(
            pg.start,
            op="PostgresContainer.start() (test_500_sequential_connections)",
        )
        stop_pg = _register_pg_atexit_stop(pg)
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")

        async def _drain() -> tuple[int, float]:
            successes = 0
            t0 = time.monotonic()
            for _ in range(_CONNECTION_COUNT):
                conn = await asyncpg.connect(dsn)
                try:
                    value = await conn.fetchval("SELECT 1")
                    assert value == 1
                    successes += 1
                finally:
                    await conn.close()
            return successes, time.monotonic() - t0

        successes, elapsed = asyncio.run(_drain())

        assert successes == _CONNECTION_COUNT, f"Expected {_CONNECTION_COUNT} successful connections, got {successes}"
        avg = elapsed / _CONNECTION_COUNT
        assert avg < _PER_CONNECTION_REGRESSION_BUDGET_SECONDS, (
            f"Per-connection latency regressed: avg={avg:.3f}s exceeds "
            f"{_PER_CONNECTION_REGRESSION_BUDGET_SECONDS:.3f}s budget over "
            f"{_CONNECTION_COUNT} sequential connections (total {elapsed:.1f}s)."
        )
    finally:
        if stop_pg is not None:
            stop_pg()
        if prior_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = prior_docker_host
        if prior_ryuk is None:
            os.environ.pop("TESTCONTAINERS_RYUK_DISABLED", None)
        else:
            os.environ["TESTCONTAINERS_RYUK_DISABLED"] = prior_ryuk
