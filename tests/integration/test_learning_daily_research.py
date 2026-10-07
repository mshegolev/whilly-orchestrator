from __future__ import annotations

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401

pytestmark = pytest.mark.integration


async def test_learning_run_migration_is_disabled_by_default(db_pool) -> None:  # noqa: F811
    row = await db_pool.fetchrow(
        "SELECT COUNT(*) AS count, COALESCE(bool_or(enabled), FALSE) AS enabled "
        "FROM swarm_learning_schedules"
    )
    assert row["count"] == 0
    assert row["enabled"] is False
